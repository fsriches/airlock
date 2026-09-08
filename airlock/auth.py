"""airlock auth: manual OAuth 2.1 + PKCE dance against Binance's AS.

Binance's authorization server advertises client_id_metadata_document_supported
and NO registration_endpoint, so dynamic client registration is not available.
Instead the client_id is a URL hosting our client metadata document
(client_metadata.json, served from the public GitHub repo via CDN).
Flow: PKCE S256, loopback redirect on 8765 (owner ssh -L forwards it),
token exchange, tokens saved 0600, then tool discovery.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import secrets
import sys
import urllib.parse
from pathlib import Path

import httpx

from .mcp_client import (
    CALLBACK_PORT,
    STATE_DIR,
    TOKEN_PATH,
    AirlockMcpClient,
    _local_callback_handler,
)

AUTHZ_URL = "https://accounts.binance.com/agentic-oauth/authorize"
TOKEN_URL = "https://accounts.binance.com/oauth-agentic/token"
DEFAULT_CLIENT_ID = (
    "https://cdn.jsdelivr.net/gh/fsriches/airlock@main/client_metadata.json"
)


def write_tools_map(tools: list[dict]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    (STATE_DIR / "tools_list.json").write_text(json.dumps(tools, indent=2, default=str))
    lines = ["# Binance MCP tool map (generated from tools/list)", ""]
    for t in tools:
        ann = t.get("annotations") or {}
        lines.append(f"## {t['name']}")
        lines.append(
            f"- readOnly: {ann.get('readOnlyHint')} | destructive: {ann.get('destructiveHint')}"
        )
        desc = (t.get("description") or "").strip().splitlines()
        if desc:
            lines.append(f"- desc: {desc[0]}")
        schema = t.get("inputSchema") or {}
        if not isinstance(schema, dict):
            schema = schema.model_dump() if hasattr(schema, "model_dump") else {}
        props = schema.get("properties") or {}
        required = schema.get("required") or []
        for k, v in props.items():
            if not isinstance(v, dict):
                v = {}
            star = "*" if k in required else ""
            lines.append(f"  - {k}{star}: {v.get('type', '?')}")
        lines.append("")
    docs = Path("docs")
    docs.mkdir(exist_ok=True)
    (docs / "tools_map.md").write_text("\n".join(lines))


async def run_dance() -> dict:
    client_id = os.environ.get("AIRLOCK_CLIENT_ID", DEFAULT_CLIENT_ID)
    redirect_uri = f"http://localhost:{CALLBACK_PORT}/callback"
    verifier = secrets.token_urlsafe(48)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    state = secrets.token_urlsafe(16)
    qs = urllib.parse.urlencode(
        {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": state,
        }
    )
    auth_url = f"{AUTHZ_URL}?{qs}"
    print(f"\nAIRLOCK OAUTH: open this URL in your browser:\n{auth_url}\n", flush=True)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    (STATE_DIR / "auth_url.txt").write_text(auth_url)

    code, cb_state = await _local_callback_handler(CALLBACK_PORT)()
    if cb_state and state and cb_state != state:
        raise RuntimeError("oauth state mismatch")

    async with httpx.AsyncClient(timeout=30) as hc:
        resp = await hc.post(
            TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "client_id": client_id,
                "code_verifier": verifier,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if resp.status_code != 200:
            raise RuntimeError(f"token exchange failed: {resp.status_code} {resp.text[:300]}")
        tokens = resp.json()

    TOKEN_PATH.write_text(json.dumps(tokens, indent=2))
    os.chmod(TOKEN_PATH, 0o600)
    print(f"TOKENS SAVED: {TOKEN_PATH} (0600). access_token present:", bool(tokens.get("access_token")), flush=True)
    return tokens


async def main() -> int:
    await run_dance()
    client = AirlockMcpClient()
    try:
        await client.connect()
        tools = await client.list_tools()
        write_tools_map(tools)
        print(f"CONNECTED. {len(tools)} tools discovered. Map written to docs/tools_map.md")
        for t in tools:
            print(" -", t["name"])
        return 0
    finally:
        await client.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
