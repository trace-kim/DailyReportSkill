"""End-to-end: start `python -m worklog mcp` over stdio and call tools like opencode would."""

import asyncio
import json
import sqlite3
import sys
from pathlib import Path


def _text(result) -> str:
    return "".join(getattr(c, "text", "") for c in result.content)


def _backdate_scans(root: Path, seconds: float) -> None:
    """Make the last scan look older, so the next close scans again (it reuses fresh scans)."""
    con = sqlite3.connect(root / ".worklog" / "index.db")
    con.execute("UPDATE scans SET taken_at = taken_at - ?", (seconds,))
    con.commit()
    con.close()


async def _session(root: Path):
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    params = StdioServerParameters(command=sys.executable, args=["-m", "worklog", "mcp", "--root", str(root)])
    out = {}
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            out["tools"] = await session.list_tools()
            out["before"] = json.loads(_text(await session.call_tool("status", {})))
            out["created_before_init"] = (root / ".worklog").exists()
            out["refused"] = json.loads(_text(await session.call_tool("changes", {"day": "2026-11-05"})))
            out["init"] = json.loads(_text(await session.call_tool("init", {})))
            out["init_again"] = json.loads(_text(await session.call_tool("init", {})))
            (root / "보고서.txt").write_text("첫 줄\n", encoding="utf-8")
            _backdate_scans(root, 120)
            out["close"] = json.loads(_text(await session.call_tool("close", {"budget_seconds": 30})))
            out["status"] = _text(await session.call_tool("status", {}))
    return out


def test_mcp_stdio_roundtrip(root: Path):
    (root / "기존 문서.txt").write_text("이미 있던 파일\n", encoding="utf-8")
    out = asyncio.run(_session(root))
    names = {t.name for t in out["tools"].tools}
    assert {"status", "init", "close", "baseline", "doctor", "changes", "diff", "read_file", "daily_save",
            "weekly_save", "render"} <= names

    # Nothing is created in the work folder before init, and tools say what to do.
    assert out["before"]["initialized"] is False and "call init" in out["before"]["hints"][0]
    assert out["created_before_init"] is False
    assert out["refused"]["ok"] is False and "call init" in out["refused"]["errors"][0]

    assert out["init"]["ok"] and out["init"]["initialized"] and out["init"]["tracked_files"] == 1
    assert out["init_again"]["already_initialized"] is True

    assert out["close"]["ok"] is True and out["close"]["by_kind"] == {"added": 1}, out["close"]
    st = json.loads(out["status"])
    assert st["root"] == str(root.resolve()) and st["initialized"] is True
    assert "업무 폴더" in out["status"]  # Korean is sent as-is, not \uXXXX-escaped
    assert (root / ".worklog" / "logs" / "worklog.log").exists()  # the first server process logs too
