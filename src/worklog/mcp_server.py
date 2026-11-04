"""MCP server (stdio) exposing worklog to opencode, the company agent and other MCP clients.

Annotations are evaluated at runtime by the MCP SDK, so this module avoids
``from __future__ import annotations`` and uses typing.Optional.
"""

import functools
import json
import logging
from typing import Optional

from .service import Worklog, WorklogError

log = logging.getLogger(__name__)

INSTRUCTIONS = """\
worklog tracks file changes in the user's work folder and stores Korean work reports.
Facts (what changed, when, diffs, document text, format checks) come from these tools;
you do the understanding and the writing. Load the `worklog` skill for the full
daily-report and weekly-report workflows and the Korean writing rules.
Start with `status`. Before writing reports, call `close` until `remaining` is 0.
Never modify or delete the user's files as part of this workflow."""


def _server_class():
    try:  # mcp >= 2
        from mcp.server.mcpserver import MCPServer

        return MCPServer
    except ImportError:  # mcp 1.x
        from mcp.server.fastmcp import FastMCP

        return FastMCP


def _json(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=1, default=str)


def _safe(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except (WorklogError, ValueError, KeyError, json.JSONDecodeError) as e:
            return _json({"ok": False, "errors": [str(e)]})
        except Exception as e:  # keep the server alive and tell the agent
            log.exception("tool %s failed", fn.__name__)
            return _json({"ok": False, "errors": [f"internal error: {type(e).__name__}: {e}"]})

    return wrapper


def build_server(root: str):
    wl = Worklog(root, create=True)
    mcp = _server_class()("worklog", instructions=INSTRUCTIONS)

    # ------------------------------------------------------------ tracking
    @mcp.tool()
    @_safe
    def status() -> str:
        """Tracking status. Call this first. Shows the last scan and close-out, files waiting
        for close-out, days that still need a daily report (missing or stale), the next
        weekly-report period, active topics and hints."""
        return _json(wl.status())

    @mcp.tool()
    @_safe
    def close(budget_seconds: int = 45) -> str:
        """Close out recent file changes: extract document text (Office is used for DRM files),
        diff it against the previous version and record dated change records. Works until the
        time budget is used; call again while 'remaining' > 0."""
        return _json(wl.close(budget_seconds=max(5, int(budget_seconds))))

    @mcp.tool()
    @_safe
    def baseline(budget_seconds: int = 45) -> str:
        """Continue the one-time baseline: capture the text of files that existed when tracking
        started, so their first change shows as a real diff. Call again while 'remaining' > 0."""
        return _json(wl.baseline(budget_seconds=max(5, int(budget_seconds))))

    @mcp.tool()
    @_safe
    def changes(day: str, limit: int = 200, offset: int = 0) -> str:
        """File changes recorded for one day (YYYY-MM-DD; work before the day boundary counts
        toward the previous day). kind: added | modified | deleted | renamed | resaved (bytes
        changed but text did not: usually noise). no_baseline: no earlier version known, so the
        diff is the full current content. touched_only: edited that day but the net change is
        recorded on another day. Use diff(change_id) to see content."""
        return _json(wl.changes(day, limit=limit, offset=offset))

    @mcp.tool()
    @_safe
    def diff(change_id: int, max_lines: int = 400, offset: int = 0) -> str:
        """Text diff of one change record (unified diff of the extracted text; Excel rows read
        'a | b | c', PowerPoint text is grouped under [슬라이드 N]). Page with offset."""
        d = wl.diff(change_id, max_lines=max_lines, offset=offset)
        if not d.get("ok"):
            return _json(d)
        head = f"# change {d['id']} · {d['kind']} · {d['path']} · {d['day']}"
        if d.get("old_path"):
            head += f" (from {d['old_path']})"
        lines = [head]
        if "total_lines" in d:
            lines.append(f"# lines {d['offset'] + 1}-{d['offset'] + d['returned']} of {d['total_lines']}"
                         + (" (more: use offset)" if d["truncated"] else ""))
        if d.get("note"):
            lines.append(f"# note: {d['note']}")
        return "\n".join(lines) + "\n" + d.get("diff", "")

    @mcp.tool()
    @_safe
    def read_file(path: str, max_chars: int = 20000, offset: int = 0) -> str:
        """Current text of a file in the work folder (Word, Excel, PowerPoint, PDF, text; DRM
        files are read through Office). Use it for context and purpose; use diff for what
        changed. Path may be relative to the work folder."""
        r = wl.read_file(path, max_chars=max_chars, offset=offset)
        if r.get("kind") != "text":
            return _json(r)
        head = (f"# {r['path']} · {r['source']} · chars {r['offset'] + 1}-{r['offset'] + r['returned']}"
                f" of {r['total_chars']}" + (" (more: use offset)" if r["truncated"] else ""))
        return head + "\n" + r["text"]

    # ------------------------------------------------------------ context
    @mcp.tool()
    @_safe
    def related(path: str) -> str:
        """Files related to a path: same folder, similar file names, files that usually change
        in the same hour, earlier daily-report mentions and topics covering the path."""
        return _json(wl.related(path))

    @mcp.tool()
    @_safe
    def search(query: str, limit: int = 20, path_prefix: Optional[str] = None) -> str:
        """Search file names and the stored text of documents in the work folder."""
        return _json(wl.search(query, limit=limit, path_prefix=path_prefix))

    @mcp.tool()
    @_safe
    def overview(path: str = "", depth: int = 2) -> str:
        """Folder tree summary (file counts, latest change, main file types) for orienting
        yourself or building the folder map."""
        return _json(wl.overview(path, depth))

    @mcp.tool()
    @_safe
    def folder_map_get() -> str:
        """Read the folder map: your own notes on what each folder of the work folder is about."""
        return _json(wl.folder_map_get())

    @mcp.tool()
    @_safe
    def folder_map_save(content: str) -> str:
        """Replace the folder map (markdown, one short line per folder, e.g.
        '- 프로젝트/A사/ : A사 공공 제안 건 (2026.10~)')."""
        return _json(wl.folder_map_save(content))

    @mcp.tool()
    @_safe
    def note_add(text: str, day: Optional[str] = None) -> str:
        """Save a note about work that leaves no file trace (meetings, calls, decisions) for a
        day (default: today). Notes are used when writing that day's report."""
        return _json(wl.note_add(text, day))

    @mcp.tool()
    @_safe
    def notes_get(day: str) -> str:
        """Notes saved for a day (YYYY-MM-DD)."""
        return _json(wl.notes_get(day))

    # ------------------------------------------------------------ topics
    @mcp.tool()
    @_safe
    def topics_list(status: Optional[str] = None) -> str:
        """Known work topics (stable IDs like T003) and the allowed statuses."""
        return _json(wl.topics_list(status))

    @mcp.tool()
    @_safe
    def topic_upsert(
        topic_id: Optional[str] = None,
        name: Optional[str] = None,
        description: Optional[str] = None,
        status: Optional[str] = None,
        paths: Optional[list[str]] = None,
        aliases: Optional[list[str]] = None,
    ) -> str:
        """Create a topic (omit topic_id; name required, a specific Korean noun phrase such as
        'A사 제안서 작성') or update one (pass topic_id). status: started | in_progress |
        reviewing | done | on_hold. paths: folder prefixes that belong to the topic."""
        return _json(wl.topic_upsert(topic_id, name, description, status, paths, aliases))

    # ------------------------------------------------------------ reports
    @mcp.tool()
    @_safe
    def daily_save(report: dict) -> str:
        """Save (or replace) the daily work log of one day. Errors are returned for fixing.
        Shape:
        {"date": "2026-11-04",
         "items": [{"topic_id": "T003",
                    "summary": "A사 제안서 3장 '추진 일정' 신규 작성 및 인건비 단가 수정",
                    "details": ["3장 추진 일정 약 2쪽 신규 작성", "인건비 단가 1,200만 원 → 1,350만 원"],
                    "files": ["프로젝트/A사/제안서_v3.docx"],
                    "context": "10/28 착수한 제안서 작업의 연속",
                    "status": "in_progress", "confidence": "high"}],
         "excluded": [{"summary": "다운로드 폴더 정리", "files": [], "reason": "보고 대상 아님"}],
         "no_work": false}
        One item per topic. topic_id must exist (topic_upsert). confidence: high|medium|low."""
        return _json(wl.daily_save(report))

    @mcp.tool()
    @_safe
    def weekly_save(report: dict) -> str:
        """Save (or replace) a weekly report. The tool builds each title as
        '{subject}을/를 {status phrase}.' and checks that every paragraph is a single paragraph
        in 존댓말 보고체 (each sentence ends with '~니다.'). Errors are returned for fixing.
        Shape:
        {"period_from": "2026-11-02", "period_to": "2026-11-13",
         "topics": [{"topic_ids": ["T003"], "subject": "A사 제안서 작성", "status": "in_progress",
                     "paragraph": "10월 28일 착수한 ... 조정하였습니다. ... 완료하였습니다."}]}
        subject = the object of the status verb, without 을/를 (for 'reviewing' use '예산안',
        not '예산안 검토'). Optional "particle": "을"|"를" when the subject ends in a non-Hangul
        character."""
        return _json(wl.weekly_save(report))

    @mcp.tool()
    @_safe
    def report_get(kind: str, key: str) -> str:
        """A saved report as JSON. kind 'daily' (key = date) or 'weekly' (key = 'FROM_TO')."""
        return _json(wl.report_get(kind, key))

    @mcp.tool()
    @_safe
    def reports_list(kind: str = "daily", date_from: Optional[str] = None, date_to: Optional[str] = None) -> str:
        """List saved reports. Daily entries include each item's topic and summary."""
        return _json(wl.reports_list(kind, date_from, date_to))

    @mcp.tool()
    @_safe
    def render(kind: str, key: str, format: str = "text") -> str:
        """Rendered report text ready to show or paste. weekly: 'text' or 'md'; daily: 'md'."""
        r = wl.render(kind, key, format)
        return r["content"] if r.get("ok") else _json(r)

    return mcp


def run(root: str) -> None:
    build_server(root).run("stdio")
