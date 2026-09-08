"""airlock auth: run the OAuth dance, then dump the live tool map.

Usage: python -m airlock.auth
Prints the authorization URL (owner opens it through the SSH tunnel).
After consent, lists tools and writes docs/tools_map.md.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from .mcp_client import STATE_DIR, AirlockMcpClient


def _as_dict(obj) -> dict:
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "model_dump"):
        try:
            return obj.model_dump()
        except Exception:  # noqa: BLE001
            return {}
    return {}


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
        schema = _as_dict(t.get("inputSchema"))
        props = schema.get("properties") or {}
        required = schema.get("required") or []
        for k, v in props.items():
            v = _as_dict(v)
            star = "*" if k in required else ""
            lines.append(f"  - {k}{star}: {v.get('type', '?')}")
        lines.append("")
    docs = Path("docs")
    docs.mkdir(exist_ok=True)
    (docs / "tools_map.md").write_text("\n".join(lines))


async def main() -> int:
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
