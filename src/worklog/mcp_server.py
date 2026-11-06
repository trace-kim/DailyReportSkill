"""MCP server (stdio) exposing worklog to opencode, DS Cowork, the company agent and other MCP clients.

Annotations are evaluated at runtime by the MCP SDK, so this module avoids
``from __future__ import annotations`` and uses typing.Optional.

Every tool must answer within the agent's tool-call timeout (often 60 s). Tools run
in worker threads; the Worklog runs in agent mode (bounded calls, scans in a
background thread, ``busy`` answers instead of queueing behind a call the agent gave
up on); long Office work (baseline, doctor) runs in separate processes.
"""

import functools
import json
import logging
import threading
import time
from pathlib import Path
from typing import Optional

import anyio.to_thread

from .cli import add_file_log
from .service import AGENT_LOCK_WAIT, Worklog, WorklogBusy, WorklogError, format_diff
from .storage import Workspace

log = logging.getLogger(__name__)

INSTRUCTIONS = """\
worklog tracks file changes in the user's work folder and stores Korean work reports.
Facts (what changed, when, diffs, document text, format checks) come from these tools;
you do the understanding and the writing. Load the `worklog` skill for the full
daily-report and weekly-report workflows and the Korean writing rules.
Start with `status`. If it says tracking has not started, call `init`.
Before writing reports, call `close` until `remaining` is 0.
An answer with "busy": true means earlier work is still running: call the same tool again.
Never modify or delete the user's files as part of this workflow."""

NOT_SET_UP = "worklog is not set up for this work folder yet: call init first"


def _server_class():
    try:  # mcp >= 2
        from mcp.server.mcpserver import MCPServer

        return MCPServer
    except ImportError:  # mcp 1.x
        from mcp.server.fastmcp import FastMCP

        return FastMCP


def _json(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=1, default=str)


class _Folder:
    """The work folder of this server. The Worklog is opened on first use, and nothing
    is created in the folder before ``init`` (or ``doctor``) asks for it."""

    def __init__(self, root: str):
        self.root = Path(root).expanduser()
        self._wl: Optional[Worklog] = None
        self._guard = threading.Lock()

    def has_data(self) -> bool:
        return self._wl is not None or Workspace(self.root).dir.exists()

    def get(self, create: bool = False) -> Worklog:
        with self._guard:
            if self._wl is None:
                if not create and not Workspace(self.root).dir.exists():
                    raise WorklogError(NOT_SET_UP)
                wl = Worklog(self.root, create=create)
                wl.agent_mode = True
                wl.lock_wait = AGENT_LOCK_WAIT
                add_file_log(wl.ws.dir)
                self._wl = wl
            return self._wl


def _not_set_up_status(root: Path) -> dict:
    if not root.is_dir():
        raise WorklogError(f"work folder not found: {root}")
    return {"root": str(Workspace(root).root), "initialized": False,
            "hints": ["Tracking has not started for this work folder: call init. It lists the files in the "
                      "background (no file is opened); nothing can be reported until files change after that."]}


def _call(fn, args, kwargs) -> str:
    t0 = time.monotonic()
    outcome = "done"
    try:
        return fn(*args, **kwargs)
    except WorklogBusy as e:
        outcome = "busy"
        return _json({"ok": False, "busy": True, "errors": [str(e)]})
    except (WorklogError, ValueError, KeyError, json.JSONDecodeError) as e:
        outcome = "error"
        return _json({"ok": False, "errors": [str(e)]})
    except Exception as e:  # keep the server alive and tell the agent
        outcome = "internal error"
        log.exception("tool %s failed", fn.__name__)
        return _json({"ok": False, "errors": [f"internal error: {type(e).__name__}: {e}"]})
    finally:
        log.info("tool %s: %s in %.1fs", fn.__name__, outcome, time.monotonic() - t0)


def _tools(mcp):
    """Decorator registering a sync tool that runs in a worker thread.

    Explicit, so mcp 1.x (which would run sync tools on its event loop) behaves like
    2.x. If the client gives up on a call, the thread finishes on its own; later calls
    then get a busy answer instead of waiting (see ``Worklog.lock_wait``).
    """

    def register(fn):
        @functools.wraps(fn)
        async def run_in_thread(*args, **kwargs):
            return await anyio.to_thread.run_sync(functools.partial(_call, fn, args, kwargs),
                                                  abandon_on_cancel=True)

        return mcp.tool()(run_in_thread)

    return register


def build_server(root: str):
    folder = _Folder(root)
    mcp = _server_class()("worklog", instructions=INSTRUCTIONS)
    tool = _tools(mcp)

    def wl() -> Worklog:
        return folder.get()

    def call_wait(w: Worklog) -> float:
        """Seconds a tool may wait for background work within one call."""
        return max(5.0, w.cfg.agent_call_seconds - 5)

    # ------------------------------------------------------------ setup & tracking
    @tool
    def status() -> str:
        """Tracking status. Call this first. Shows whether tracking has started (initialized),
        the last scan and close-out, files waiting for close-out, days that still need a daily
        report (missing or stale), the next weekly-report period, active topics, background
        work, scheduled tasks and hints."""
        if not folder.has_data():
            return _json(_not_set_up_status(folder.root))
        return _json(wl().status())

    @tool
    def init() -> str:
        """Start tracking this work folder (first use; status shows initialized=false). Lists the
        files (names, sizes and dates; no file is opened) in the background as the starting point;
        changes after that are recorded. Returns at once or within about half a minute; if the
        inventory is still running, call status later. Safe to call again: a folder that is
        already tracked is left as it is. Never modifies the user's files."""
        w = folder.get(create=True)
        return _json(w.start_tracking(wait=call_wait(w)))

    @tool
    def close(budget_seconds: int = 30) -> str:
        """Close out recent file changes: extract document text (Office is used for DRM files),
        diff it against the previous version and record dated change records. Works until the
        time budget is used; call again while 'remaining' > 0. A busy answer (a folder scan or
        another run is still going) also means: call again."""
        w = wl()
        return _json(w.close(budget_seconds=max(5.0, min(float(budget_seconds), w.cfg.agent_call_seconds))))

    @tool
    def baseline(minutes: int = 15) -> str:
        """Start the one-time baseline in the background: capture the text of files that existed
        when tracking started, so their first change shows as a real diff. Returns at once; there
        is nothing to wait for and reports do not need it. It reads files through Office for up to
        'minutes', stepping aside whenever a close-out needs it, so keep working meanwhile.
        survives_session false: this agent app ends it with the session (progress is kept).
        Progress: status.baseline_remaining."""
        return _json(wl().start_baseline(minutes=max(1, min(120, int(minutes)))))

    @tool
    def doctor(rerun: bool = False) -> str:
        """Check how this folder's documents can be read: directly, only through Word/Excel/
        PowerPoint (DRM-protected files), or not at all, and whether Office automation works in
        this session. Runs in the background for 1-3 minutes; call again until it returns the
        verdicts. Returns the latest result if there is one; rerun=true checks again. Opens files
        read-only and changes nothing in the work folder."""
        w = folder.get(create=True)
        return _json(w.doctor_job(rerun=bool(rerun), wait=call_wait(w)))

    @tool
    def changes(day: str, limit: int = 200, offset: int = 0) -> str:
        """File changes recorded for one day (YYYY-MM-DD; work before the day boundary counts
        toward the previous day). kind: added | modified | deleted | renamed | resaved (bytes
        changed but text did not: usually noise). no_baseline: no earlier version known, so the
        diff is the full current content. touched_only: edited that day but the net change is
        recorded on another day. Use diff(change_id) to see content."""
        return _json(wl().changes(day, limit=limit, offset=offset))

    @tool
    def diff(change_id: int, max_lines: int = 400, offset: int = 0) -> str:
        """Text diff of one change record (unified diff of the extracted text; Excel rows read
        'a | b | c', PowerPoint text is grouped under [슬라이드 N]). Page with offset."""
        d = wl().diff(change_id, max_lines=max_lines, offset=offset)
        return format_diff(d) if d.get("ok") else _json(d)

    @tool
    def read_file(path: str, max_chars: int = 20000, offset: int = 0) -> str:
        """Current text of a file in the work folder (Word, Excel, PowerPoint, PDF, text; DRM
        files are read through Office). Use it for context and purpose; use diff for what
        changed. Path may be relative to the work folder."""
        r = wl().read_file(path, max_chars=max_chars, offset=offset)
        if r.get("kind") != "text":
            return _json(r)
        head = (f"# {r['path']} · {r['source']} · chars {r['offset'] + 1}-{r['offset'] + r['returned']}"
                f" of {r['total_chars']}" + (" (more: use offset)" if r["truncated"] else ""))
        return head + "\n" + r["text"]

    # ------------------------------------------------------------ context
    @tool
    def related(path: str) -> str:
        """Files related to a path: same folder, similar file names, files that usually change
        in the same hour, earlier daily-report mentions and topics covering the path."""
        return _json(wl().related(path))

    @tool
    def search(query: str, limit: int = 20, path_prefix: Optional[str] = None) -> str:
        """Search file names and the stored text of documents in the work folder."""
        return _json(wl().search(query, limit=limit, path_prefix=path_prefix))

    @tool
    def overview(path: str = "", depth: int = 2) -> str:
        """Folder tree summary (file counts, latest change, main file types) for orienting
        yourself or building the folder map."""
        return _json(wl().overview(path, depth))

    @tool
    def folder_map_get() -> str:
        """Read the folder map: your own notes on what each folder of the work folder is about."""
        return _json(wl().folder_map_get())

    @tool
    def folder_map_save(content: str) -> str:
        """Replace the folder map (markdown, one short line per folder, e.g.
        '- 프로젝트/A사/ : A사 공공 제안 건 (2026.10~)')."""
        return _json(wl().folder_map_save(content))

    @tool
    def note_add(text: str, day: Optional[str] = None) -> str:
        """Save a note about work that leaves no file trace (meetings, calls, decisions) for a
        day (default: today). Notes are used when writing that day's report."""
        return _json(wl().note_add(text, day))

    @tool
    def notes_get(day: str) -> str:
        """Notes saved for a day (YYYY-MM-DD)."""
        return _json(wl().notes_get(day))

    # ------------------------------------------------------------ topics
    @tool
    def topics_list(status: Optional[str] = None) -> str:
        """Known work topics (stable IDs like T003) and the allowed statuses."""
        return _json(wl().topics_list(status))

    @tool
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
        reviewing | done | on_hold. paths: subfolders or files (relative to the work folder) that
        belong to the topic, e.g. ['프로젝트/A사']; for files directly in the work folder list the
        files ('.' would match every file)."""
        return _json(wl().topic_upsert(topic_id, name, description, status, paths, aliases))

    # ------------------------------------------------------------ reports
    @tool
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
        return _json(wl().daily_save(report))

    @tool
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
        return _json(wl().weekly_save(report))

    @tool
    def report_get(kind: str, key: str) -> str:
        """A saved report as JSON. kind 'daily' (key = date) or 'weekly' (key = 'FROM_TO')."""
        return _json(wl().report_get(kind, key))

    @tool
    def reports_list(kind: str = "daily", date_from: Optional[str] = None, date_to: Optional[str] = None) -> str:
        """List saved reports. Daily entries include each item's topic and summary."""
        return _json(wl().reports_list(kind, date_from, date_to))

    @tool
    def render(kind: str, key: str, format: str = "text") -> str:
        """Rendered report text ready to show or paste. weekly: 'text' or 'md'; daily: 'md'."""
        r = wl().render(kind, key, format)
        return r["content"] if r.get("ok") else _json(r)

    return mcp


def run(root: str) -> None:
    build_server(root).run("stdio")
