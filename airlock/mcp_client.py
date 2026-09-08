"""AIRLOCK MCP client: Streamable HTTP + OAuth 2.1 to the official Binance MCP Server.

The OAuth consent happens once in a desktop browser. On a headless VPS the
owner forwards the callback port over SSH (ssh -L 8765:localhost:8765) and
opens the printed URL locally. Tokens persist to ~/.airlock with mode 600.
No tool names are invented here: discovery is whatever tools/list returns.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any

MCP_URL = os.environ.get("AIRLOCK_MCP_URL", "https://agent.binance.com/mcp/agentic")
STATE_DIR = Path(os.environ.get("AIRLOCK_HOME", str(Path.home() / ".airlock")))
CALLBACK_PORT = int(os.environ.get("AIRLOCK_CALLBACK_PORT", "8765"))
TOKEN_PATH = STATE_DIR / "binance_tokens.json"
AUTH_URL_PATH = STATE_DIR / "auth_url.txt"
MAX_CALLS_PER_MIN = 60


class McpToolError(RuntimeError):
    pass


class FileTokenStorage:
    """Implements mcp.client.auth.TokenStorage backed by a 0600 JSON file.

    Falls back to an in-memory store on any filesystem error so the OAuth
    dance never hard-fails on a read-only FS.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._memory: dict[str, Any] = {}

    def _read(self) -> dict[str, Any]:
        try:
            if self.path.exists():
                data = json.loads(self.path.read_text())
                if isinstance(data, dict):
                    return data
        except Exception:  # noqa: BLE001
            pass
        return dict(self._memory)

    def _write(self, data: dict[str, Any]) -> None:
        self._memory = dict(data)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(data, indent=2, default=str))
            os.chmod(self.path, 0o600)
        except Exception:  # noqa: BLE001
            pass

    async def get_tokens(self):
        return self._read().get("tokens")

    async def set_tokens(self, tokens) -> None:
        data = self._read()
        data["tokens"] = tokens.model_dump(mode="json") if hasattr(tokens, "model_dump") else tokens
        self._write(data)

    async def get_client_info(self):
        raw = self._read().get("client_info")
        if not raw:
            return None
        from mcp.shared.auth import OAuthClientMetadata

        try:
            return OAuthClientMetadata.model_validate(raw)
        except Exception:  # noqa: BLE001
            return None

    async def set_client_info(self, info) -> None:
        data = self._read()
        data["client_info"] = info.model_dump(mode="json")
        self._write(data)


def _announce_auth_url(auth_url: str) -> None:
    print(f"\nAIRLOCK OAUTH: open this URL in your browser:\n{auth_url}\n", flush=True)
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        AUTH_URL_PATH.write_text(auth_url)
    except Exception:  # noqa: BLE001
        pass


def build_oauth_provider():
    """Construct the mcp SDK OAuthClientProvider.

    The SDK handles PKCE, dynamic client registration, and token refresh.
    """
    from mcp.client.auth import OAuthClientProvider
    from mcp.shared.auth import OAuthClientMetadata

    redirect_uri = f"http://localhost:{CALLBACK_PORT}/callback"
    provider = OAuthClientProvider(
        server_url=MCP_URL,
        client_metadata=OAuthClientMetadata(
            client_name="airlock",
            redirect_uris=[redirect_uri],
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            token_endpoint_auth_method="none",
            scope="trade",
        ),
        storage=FileTokenStorage(STATE_DIR / "oauth_state.json"),
        redirect_handler=_announce_auth_url,
        callback_handler=_local_callback_handler(CALLBACK_PORT),
    )
    try:
        provider.on_redirect = _announce_auth_url  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass
    return provider


def _local_callback_handler(port: int):
    """Serve one GET /callback?code=... on localhost:port.

    The owner SSH-forwards this port (ssh -L port:localhost:port) so the
    browser consent on their laptop lands here. Returns (code, state).
    """

    async def _handler() -> tuple[str, str | None]:
        from urllib.parse import parse_qs

        result: dict[str, str] = {}

        async def _app(scope, receive, send):
            if scope["type"] == "http":
                result.update(
                    {k: v[0] for k, v in parse_qs(scope["query_string"].decode()).items()}
                )
                body = b"<html><body><h3>Airlock OAuth OK - close this tab.</h3></body></html>"
                await send(
                    {
                        "type": "http.response.start",
                        "status": 200,
                        "headers": [(b"content-type", b"text/html")],
                    }
                )
                await send({"type": "http.response.body", "body": body})
            else:
                while True:
                    msg = await receive()
                    if msg["type"] == "lifespan.startup":
                        await send({"type": "lifespan.startup.complete"})
                    elif msg["type"] == "lifespan.shutdown":
                        await send({"type": "lifespan.shutdown.complete"})
                        return

        import uvicorn

        server = uvicorn.Server(
            uvicorn.Config(_app, host="127.0.0.1", port=port, log_level="error")
        )
        task = asyncio.create_task(server.serve())
        for _ in range(3000):  # about 5 minutes at 0.1s
            await asyncio.sleep(0.1)
            if "code" in result:
                break
        server.should_exit = True
        try:
            await asyncio.wait_for(task, timeout=3)
        except Exception:  # noqa: BLE001
            task.cancel()
        if "code" not in result:
            raise RuntimeError("oauth callback timeout: no ?code= received")
        return result["code"], result.get("state")

    return _handler


class AirlockMcpClient:
    """Thin async wrapper around the official mcp SDK session."""

    def __init__(self) -> None:
        self._session = None
        self._call_times: list[float] = []

    async def connect(self) -> None:
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        try:
            auth = build_oauth_provider()
        except Exception:  # noqa: BLE001
            auth = None
        self._cm = streamablehttp_client(MCP_URL, auth=auth)
        read, write, _ = await self._cm.__aenter__()
        self._session_cm = ClientSession(read, write)
        self._session = await self._session_cm.__aenter__()
        await self._session.initialize()

    async def call(self, tool: str, args: dict[str, Any], timeout: float = 20.0) -> dict[str, Any]:
        if self._session is None:
            raise McpToolError("client not connected")
        await self._rate_limit()
        for attempt in range(3):
            try:
                result = await asyncio.wait_for(
                    self._session.call_tool(tool, args), timeout=timeout
                )
                if getattr(result, "isError", False):
                    raise McpToolError(str(result.content))
                data = getattr(result, "structuredContent", None) or {
                    "content": [getattr(c, "text", str(c)) for c in (result.content or [])]
                }
                return {"ok": True, "result": data}
            except McpToolError:
                raise
            except Exception as e:  # noqa: BLE001 - transient retry
                if attempt == 2:
                    return {"ok": False, "error": str(e)}
                await asyncio.sleep(0.5 * (2**attempt))
        return {"ok": False, "error": "unreachable"}

    async def list_tools(self) -> list[dict[str, Any]]:
        if self._session is None:
            raise McpToolError("client not connected")
        res = await self._session.list_tools()
        out = []
        for t in res.tools:
            out.append(
                {
                    "name": t.name,
                    "description": t.description,
                    "inputSchema": getattr(t, "inputSchema", None),
                    "annotations": {
                        "readOnlyHint": getattr(t.annotations, "readOnlyHint", None),
                        "destructiveHint": getattr(t.annotations, "destructiveHint", None),
                    }
                    if t.annotations
                    else {},
                }
            )
        return out

    async def _rate_limit(self) -> None:
        now = time.monotonic()
        self._call_times = [t for t in self._call_times if now - t < 60.0]
        if len(self._call_times) >= MAX_CALLS_PER_MIN:
            wait = 60.0 - (now - self._call_times[0]) + 0.05
            await asyncio.sleep(wait)
        self._call_times.append(time.monotonic())

    async def close(self) -> None:
        for name in ("_session_cm", "_cm"):
            cm = getattr(self, name, None)
            if cm is not None:
                try:
                    await cm.__aexit__(None, None, None)
                except Exception:  # noqa: BLE001
                    pass
                setattr(self, name, None)
        self._session = None
