"""End-to-end: start `python -m worklog mcp` over stdio and call tools like opencode would."""

import asyncio
import json
import sys
from pathlib import Path

from conftest import write_config


async def _session(root: Path):
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    params = StdioServerParameters(command=sys.executable, args=["-m", "worklog", "mcp", "--root", str(root)])
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            (root / "보고서.txt").write_text("첫 줄\n", encoding="utf-8")
            await session.call_tool("status", {})  # first call initializes and scans
            close = await session.call_tool("close", {"budget_seconds": 30})
            status = await session.call_tool("status", {})
            return tools, close, status


def _text(result) -> str:
    return "".join(getattr(c, "text", "") for c in result.content)


def test_mcp_stdio_roundtrip(root: Path):
    write_config(root, com=False)
    tools, close, status = asyncio.run(_session(root))
    names = {t.name for t in tools.tools}
    assert {"status", "close", "changes", "diff", "read_file", "daily_save", "weekly_save", "render"} <= names
    assert json.loads(_text(close))["ok"] is True
    st = json.loads(_text(status))
    assert st["root"] == str(root.resolve())
    assert "업무 폴더" in _text(status)  # Korean is sent as-is, not \uXXXX-escaped
