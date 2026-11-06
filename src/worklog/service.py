"""Worklog facade: the single API behind the CLI and the MCP server."""

from __future__ import annotations

import datetime as dt
import functools
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from collections import Counter, defaultdict
from pathlib import Path

from . import context
from .closeout import HARD_GRACE_SECONDS, baseline_where, candidate_counts, run_baseline, run_close
from .config import Config, set_report_command, write_default_config
from .db import connect, get_meta, set_meta
from .extract import Extractor
from .ignore import IgnoreRules
from .reports import Reports
from .scanner import MTIME_EPS
from .scanner import scan as scan_folder
from .storage import FileLock, JobMarker, TextVault, Workspace, atomic_write_text, hide_path
from .topics import TopicStore

log = logging.getLogger(__name__)

_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
_WEEKDAYS = "월화수목금토일"
MAX_LISTED_DAYS = 31
# Agent mode (MCP): how long a call waits for another call before answering busy.
AGENT_LOCK_WAIT = 10.0
# Agent mode: status reuses a scan up to this old, and waits this long for a new one.
STATUS_SCAN_MAX_AGE = 600.0
STATUS_SCAN_WAIT = 5.0
TASKS_CACHE_SECONDS = 600.0


class WorklogError(Exception):
    pass


class WorklogBusy(WorklogError):
    """Another call still holds the Worklog (typically one the agent stopped waiting for)."""


def _locked(fn):
    """Serialize calls. In agent mode a call that cannot get the lock within
    ``lock_wait`` raises WorklogBusy instead of queueing behind a long call."""

    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        if not self._lock.acquire(timeout=-1 if self.lock_wait is None else self.lock_wait):
            raise WorklogBusy(self._busy_message())
        self._active.append((fn.__name__, time.monotonic()))
        try:
            return fn(self, *args, **kwargs)
        finally:
            self._active.pop()
            self._lock.release()

    return wrapper


def _iso(ts: float | None) -> str | None:
    return dt.datetime.fromtimestamp(ts).isoformat(timespec="seconds") if ts else None


def _hhmm(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts).strftime("%H:%M")


def _check_day(day: str) -> str:
    try:
        return dt.date.fromisoformat(str(day)).isoformat()
    except ValueError:
        raise WorklogError(f"invalid date {day!r}; use YYYY-MM-DD") from None


# Terminal escape sequences: CSI (colors, cursor moves), OSC (titles, links), other two-byte escapes.
_ANSI = re.compile(rb"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[@-_])")


def strip_ansi(data: bytes) -> bytes:
    return _ANSI.sub(b"", data)


def log_tail(path: Path, lines: int = 12, max_chars: int = 2000) -> str:
    """Last non-empty lines of an agent log, for the failure message."""
    try:
        text = strip_ansi(path.read_bytes()).decode("utf-8", errors="replace")
    except OSError:
        return ""
    kept = [ln.rstrip() for ln in text.replace("\r\n", "\n").replace("\r", "\n").split("\n") if ln.strip()]
    return "\n".join(kept[-lines:])[-max_chars:]


def format_diff(d: dict) -> str:
    """Plain-text form of a ``Worklog.diff`` result: header lines, then the diff."""
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


# --------------------------------------------------------------------------
# scheduled tasks (registered by scripts/install.ps1)
# --------------------------------------------------------------------------
_TASKS_CACHE: dict[str, tuple[float, dict | None]] = {}
_ROOT_ARG = re.compile(r'--root\s+(?:"([^"]+)"|(\S+))')
_TASK_RESULTS = {0: "ok", 0x41301: "running", 0x41303: "not run yet"}


def task_root(arguments: str) -> str | None:
    """The --root folder in a task's command-line arguments."""
    m = _ROOT_ARG.search(arguments or "")
    return (m.group(1) or m.group(2)) if m else None


def same_folder(a: str | Path, b: str | Path) -> bool:
    def norm(p) -> str:
        try:
            p = Path(p).resolve()  # also maps a drive letter to the UNC path, like Workspace does
        except OSError:
            p = Path(p)
        return os.path.normcase(str(p).rstrip("\\/"))

    return norm(a) == norm(b)


def survives_session(pid: int) -> bool | None:
    """Whether a process this server started outlives the app that started the server.

    An agent app can keep its servers in a Job Object that it ends together with them (the
    MCP Python SDK does, with KILL_ON_JOB_CLOSE and no breakaway). Jobs nest: the venv and
    console-script launchers put Python in a job of their own inside the app's. The child
    was started with CREATE_BREAKAWAY_FROM_JOB, which leaves every job that allows it and
    still "succeeds" when an outer one does not, so any job it is still in forbids leaving
    and can be ended with the session. libuv (Node) jobs let grandchildren leave silently,
    so under such apps the child is in no job. None: it cannot be told.
    """
    if sys.platform != "win32":
        return None
    try:
        import win32api
        import win32con
        import win32job

        h = win32api.OpenProcess(win32con.PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        try:
            return not win32job.IsProcessInJob(h, None)
        finally:
            win32api.CloseHandle(h)
    except Exception as e:  # noqa: BLE001 - only a hint for the agent
        log.debug("job check for pid %s: %s", pid, e)
        return None


def find_scheduled_tasks(root: Path) -> dict | None:
    """worklog's Task Scheduler tasks for this folder: ``{"scan": {...}, "morning": {...}}``
    with only the tasks found, or None when this cannot be checked. Cached for a while."""
    if sys.platform != "win32":
        return None
    key = os.path.normcase(str(root))
    hit = _TASKS_CACHE.get(key)
    if hit and time.monotonic() - hit[0] < TASKS_CACHE_SECONDS:
        return hit[1]
    result = _query_tasks(Path(root))
    _TASKS_CACHE[key] = (time.monotonic(), result)
    return result


def _query_tasks(root: Path) -> dict | None:
    try:
        import pythoncom
        import win32com.client
    except ImportError:
        return None
    initialized = False
    try:
        pythoncom.CoInitialize()
        initialized = True
        svc = win32com.client.Dispatch("Schedule.Service")
        svc.Connect()
        found: dict = {}
        for t in svc.GetFolder("\\").GetTasks(1):  # 1 = TASK_ENUM_HIDDEN
            name = str(t.Name)
            low = name.lower()
            kind = "scan" if low.startswith("worklog-scan-") else "morning" if low.startswith("worklog-morning-") else None
            if kind is None:
                continue
            args = " ".join(str(a.Arguments or "") for a in t.Definition.Actions if getattr(a, "Type", None) == 0)
            folder = task_root(args)
            if not folder or not same_folder(folder, root):
                continue
            code = int(t.LastTaskResult) & 0xFFFFFFFF
            try:
                last = t.LastRunTime
                last_run = last.strftime("%Y-%m-%dT%H:%M") if last.year >= 2000 else None
            except Exception:
                last_run = None
            found[kind] = {"name": name, "enabled": bool(t.Enabled), "last_run": last_run,
                           "last_result": _TASK_RESULTS.get(code, f"0x{code:X}")}
        return found
    except Exception as e:
        log.debug("cannot query scheduled tasks: %s", e)
        return None
    finally:
        if initialized:
            pythoncom.CoUninitialize()


class _ScanJob:
    """A scan running in a background thread with its own database connection."""

    def __init__(self) -> None:
        self.requested = time.time()
        self.started = time.monotonic()
        self.progress = {"files": 0}
        self.done = threading.Event()
        self.result: dict | None = None
        self.error: str | None = None

    def state(self) -> dict:
        return {"files_seen": self.progress["files"], "running_seconds": round(time.monotonic() - self.started)}


class Worklog:
    def __init__(self, root: str | Path, create: bool = False):
        root = Path(root).expanduser()
        if not root.is_dir():
            raise WorklogError(f"work folder not found: {root}")
        ws = Workspace(root)
        if not ws.dir.exists():
            if not create:
                raise WorklogError(f'worklog is not initialized for {ws.root}; run: worklog init --root "{ws.root}"')
            ws.dir.mkdir(parents=True, exist_ok=True)
            hide_path(ws.dir)
        write_default_config(ws.config_path)
        self.cfg = Config.load(ws.config_path)
        ws.set_reports_dir(self.cfg.reports_dir)
        self.ws = ws
        self.con = connect(ws.db_path)
        self.vault = TextVault(self.cfg.protect_text)
        self.topics = TopicStore(ws.topics_path, self.cfg.status_phrases.keys())
        self.reports = Reports(self)
        self._lock = threading.RLock()
        self._active: list[tuple[str, float]] = []
        # Agent mode is set by the MCP server: every call must return within the agent's
        # tool-call timeout, so long work is bounded, moved to the background, or answered
        # with busy. The CLI and the scheduled tasks keep the unbounded behaviour.
        self.agent_mode = False
        self.lock_wait: float | None = None
        self.com_memory: dict = {}  # Office health shared by the calls of this process
        self._scan_job: _ScanJob | None = None
        self._procs: dict[str, tuple[subprocess.Popen, float]] = {}

    @classmethod
    def init(cls, root: str | Path, report_command: list[str] | None = None) -> tuple["Worklog", dict]:
        wl = cls(root, create=True)
        if report_command is not None:
            set_report_command(wl.ws.config_path, report_command)
            wl.cfg = Config.load(wl.ws.config_path)
        first = wl.scan()
        return wl, {
            "ok": True,
            "root": str(wl.ws.root),
            "data_dir": str(wl.ws.dir),
            "config": str(wl.ws.config_path),
            "reports_dir": str(wl.ws.reports_dir),
            "text_protection": "dpapi" if wl.vault.protect else "none",
            "scan": first,
            "baseline_remaining": wl.baseline_remaining(),
            "report_command": wl.cfg.report_command,
        }

    def close_db(self) -> None:
        self.con.close()

    # ------------------------------------------------------------------
    # internals shared with closeout/context
    # ------------------------------------------------------------------
    def rules(self) -> IgnoreRules:
        rel = self.ws.reports_dir_rel()
        return IgnoreRules(self.ws.root, [rel.rstrip("/") + "/"] if rel else [])

    def write_diff(self, day: str, change_id: int, text: str) -> str:
        rel = f"diffs/{day}/{change_id}.bin"
        self.vault.write(self.ws.dir / rel, text)
        return rel

    def read_diff(self, rel: str) -> str | None:
        return self.vault.read(self.ws.dir / rel)

    def media_record(self, raw_hash: str, path: str, meta: dict) -> None:
        p = self.ws.media_dir / f"{raw_hash}.json"
        data: dict = {"hash": raw_hash, "description": None, "paths": []}
        if p.exists():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                pass
        data["meta"] = meta
        paths = [x for x in data.get("paths", []) if x != path] + [path]
        data["paths"] = paths[-10:]
        atomic_write_text(p, json.dumps(data, ensure_ascii=False, indent=2))

    def file_row(self, rel: str):
        row = self.con.execute("SELECT * FROM files WHERE path=?", (rel,)).fetchone()
        if row is None:
            row = self.con.execute("SELECT * FROM files WHERE path=? COLLATE NOCASE", (rel,)).fetchone()
        return row

    @property
    def initialized(self) -> bool:
        """Tracking has started: the first inventory is recorded."""
        return get_meta(self.con, "installed_at") is not None

    def baseline_remaining(self) -> int:
        if not self.initialized:
            return 0
        where, params = baseline_where(self)
        return self.con.execute(f"SELECT COUNT(*) FROM files WHERE {where}", params).fetchone()[0]

    def baseline_skipped_old(self) -> int:
        """Files left out of the baseline because they were not modified for a long time."""
        installed = get_meta(self.con, "installed_at")
        if installed is None or self.cfg.baseline_max_age_days <= 0:
            return 0
        return self.con.execute(
            "SELECT COUNT(*) FROM files WHERE present=1 AND ext_at IS NULL AND origin='init' AND mtime < ?",
            (float(installed) - self.cfg.baseline_max_age_days * 86400,),
        ).fetchone()[0]

    def _busy_message(self) -> str:
        try:
            name, since = self._active[0]
        except IndexError:
            name, since = "another", time.monotonic()
        return (f"worklog is still working on an earlier '{name}' call ({time.monotonic() - since:.0f}s so far), "
                "probably one whose answer the agent stopped waiting for. Call again in about a minute.")

    def _maybe_scan(self, max_age: float = 600) -> None:
        last = self.con.execute("SELECT MAX(taken_at) FROM scans").fetchone()[0]
        if last is None or time.time() - last > max_age:
            self.scan()

    @_locked
    def ensure_scan(self, max_age: float, wait: float | None) -> dict:
        """Make the index reflect the folder as of at most ``max_age`` seconds ago.

        With ``wait=None`` a needed scan runs in this thread (CLI). Otherwise it runs in
        a background thread with its own connection and this waits up to ``wait``
        seconds, so a slow folder (network or document-management drive) never holds a
        tool call past its time limit: the scan goes on and a later call gets its result.
        """
        job = self._scan_job
        if job is None:
            # Age counts from the end of the walk: a slow walk is not stale the moment it ends.
            last = self.con.execute("SELECT MAX(taken_at + COALESCE(duration, 0)) FROM scans").fetchone()[0]
            if last is not None and time.time() - last <= max_age:
                return {"ok": True, "reused": True, "age_seconds": round(time.time() - last)}
            if wait is None:
                return self.scan()
            job = self._scan_job = _ScanJob()
            threading.Thread(target=self._scan_worker, args=(job,), name="worklog-scan", daemon=True).start()
        if not job.done.wait(wait):
            st = job.state()
            return {"ok": False, "busy": True, "scanning": True, **st,
                    "message": f"Scanning the work folder takes longer than one call ({st['files_seen']:,} files "
                               f"so far, {st['running_seconds']}s). It continues in the background: call again "
                               "in about a minute."}
        self._scan_job = None
        if job.error:
            return {"ok": False, "errors": [f"scan failed: {job.error}"]}
        return job.result or {"ok": True}

    def _scan_worker(self, job: _ScanJob) -> None:
        con = None
        try:
            con = connect(self.ws.db_path, setup=False)
            lock = FileLock(self.ws.lock_path("scan"))
            if not lock.acquire_wait(900):
                job.error = "another process has held the scan lock for 15 minutes"
                return
            try:
                last = con.execute("SELECT MAX(taken_at) FROM scans").fetchone()[0]
                if last is not None and last >= job.requested:  # another process scanned meanwhile
                    job.result = {"ok": True, "reused": True, "age_seconds": round(time.time() - last)}
                else:
                    job.result = {"ok": True, **scan_folder(con, self.ws.root, self.rules(), progress=job.progress)}
            finally:
                lock.release()
        except Exception as e:
            log.exception("background scan failed")
            job.error = f"{type(e).__name__}: {e}"
        finally:
            if con is not None:
                con.close()
            job.done.set()

    # ------------------------------------------------------------------
    # tracking
    # ------------------------------------------------------------------
    @_locked
    def scan(self) -> dict:
        lock = FileLock(self.ws.lock_path("scan"))
        if not lock.acquire_wait(60):
            return {"ok": False, "busy": True, "message": "another scan is running"}
        try:
            return {"ok": True, **scan_folder(self.con, self.ws.root, self.rules())}
        finally:
            lock.release()

    @_locked
    def close(self, budget_seconds: float | None = None) -> dict:
        if self.agent_mode and not self.initialized:
            if self._scan_job is None:
                return {"ok": False, "errors": ["tracking has not started for this work folder: call init first"]}
            started = self.ensure_scan(float("inf"), wait=budget_seconds)  # the first inventory is running
            if not started.get("ok"):
                return started
            budget_seconds = 5.0  # the inventory used the time; nothing has changed since it anyway
        return run_close(self, budget_seconds)

    @_locked
    def start_tracking(self, wait: float) -> dict:
        """``init`` for agents: start tracking without blocking the call.

        The first inventory (names, sizes and dates; no file is opened) runs in the
        background; this waits up to ``wait`` seconds for it. Calling it again on a
        folder that is already tracked changes nothing.
        """
        already = self.initialized
        if not already:
            r = self.ensure_scan(float("inf"), wait=wait)
            if r.get("scanning"):
                return {"ok": True, "initialized": False, "root": str(self.ws.root),
                        "inventory": {"running": True, "files_seen": r["files_seen"],
                                      "running_seconds": r["running_seconds"]},
                        "message": "The first inventory of the work folder (names, sizes and dates only; no file is "
                                   "opened) continues in the background. Call status in about a minute; init does "
                                   "not need to be called again."}
            if not r.get("ok"):
                return r
        installed = float(get_meta(self.con, "installed_at"))
        return {
            "ok": True,
            "initialized": True,
            "already_initialized": already,
            "root": str(self.ws.root),
            "data_dir": str(self.ws.dir),
            "tracking_since": _iso(installed),
            "tracked_files": self.con.execute("SELECT COUNT(*) FROM files WHERE present=1").fetchone()[0],
            "baseline_remaining": self.baseline_remaining(),
            "baseline_skipped_old": self.baseline_skipped_old(),
            "scheduled_tasks": find_scheduled_tasks(self.ws.root),
            "next": ("Tracking was already set up; nothing was changed." if already else
                     "Tracking started: changes from now on are recorded. Nothing can be reported before files "
                     "change, so the first daily log is usually written the next day."),
        }

    @_locked
    def baseline(self, budget_seconds: float | None = None) -> dict:
        return run_baseline(self, budget_seconds)

    # ------------------------------------------------------------------
    # status
    # ------------------------------------------------------------------
    @_locked
    def status(self) -> dict:
        scanning = None
        if self.agent_mode:
            # Never start the first inventory from here (that is init's job), and never
            # block on a slow scan: report it and go on with the previous scan's data.
            if self.initialized or self._scan_job is not None:
                r = self.ensure_scan(STATUS_SCAN_MAX_AGE, wait=STATUS_SCAN_WAIT)
                if r.get("scanning"):
                    scanning = {"files_seen": r["files_seen"], "running_seconds": r["running_seconds"]}
        else:
            self._maybe_scan()
        initialized = self.initialized
        con = self.con
        now = time.time()
        today = self.cfg.today(now)
        last_scan_row = con.execute("SELECT taken_at, duration FROM scans ORDER BY id DESC LIMIT 1").fetchone()
        last_scan = last_scan_row["taken_at"] if last_scan_row else None
        last_close = con.execute("SELECT * FROM closes ORDER BY id DESC LIMIT 1").fetchone()
        pending = candidate_counts(self)
        baseline_left = self.baseline_remaining()
        tasks = find_scheduled_tasks(self.ws.root)

        per_day: dict[str, dict] = defaultdict(lambda: {"ids": set(), "reportable": 0, "revised": 0.0})
        for r in con.execute("SELECT id, day, kind, revised_at FROM changes"):
            info = per_day[r["day"]]
            info["ids"].add(r["id"])
            if r["kind"] != "resaved":
                info["reportable"] += 1
            if r["revised_at"]:
                info["revised"] = max(info["revised"], r["revised_at"])
        needing = []
        for day in sorted(per_day):
            info = per_day[day]
            if day >= today or info["reportable"] == 0:
                continue
            rep = self.reports.get_daily(day)
            if rep is None:
                needing.append({"day": day, "changes": info["reportable"], "reason": "missing"})
                continue
            new = info["ids"] - set(rep.get("covered_change_ids", []))
            if new:
                needing.append({"day": day, "changes": info["reportable"],
                                "reason": f"stale: {len(new)} change(s) recorded after the report was written"})
            elif info["revised"] > rep.get("generated_ts", 0):
                needing.append({"day": day, "changes": info["reportable"],
                                "reason": "stale: a previously unreadable file was extracted after the report"})
        older = max(0, len(needing) - MAX_LISTED_DAYS)
        needing = needing[-MAX_LISTED_DAYS:]

        hints = []
        if not initialized:
            if scanning:
                hints.append(f"Tracking is starting: the first inventory of the work folder is running "
                             f"({scanning['files_seen']:,} files so far). Call status again in a minute. Nothing "
                             "can be reported until files change after it.")
            else:
                hints.append("Tracking has not started for this work folder: call init "
                             "(in a terminal: worklog init).")
        elif scanning:
            hints.append(f"A scan of the work folder is running in the background ({scanning['files_seen']:,} "
                         "files so far); the counts here are from the previous scan. close waits for it.")
        if pending["changed"]:
            hints.append(f"{pending['changed']} file change(s) are not closed out yet: call close "
                         "(repeat while 'remaining' > 0) before writing reports.")
        if pending["retry_failed"]:
            hints.append(f"{pending['retry_failed']} file(s) failed text extraction earlier and will be retried "
                         "at the next close.")
        if baseline_left:
            how = ("the morning task continues it, or the 'baseline' tool runs it in the background"
                   if self.agent_mode else "`worklog baseline` or the morning task continues it")
            hints.append(f"Baseline text is not captured yet for {baseline_left} file(s): their first change will "
                         f"show the full content instead of a diff. Reports do not need it and nothing has to wait "
                         f"for it; {how}.")
        if initialized and tasks is not None and "scan" not in tasks:
            hints.append("No scheduled tasks are installed for this folder: changes are recorded only when an agent "
                         "calls close, and the baseline advances only when it is started. Tell the user that the "
                         "hourly scan and the morning close-out are installed with the worklog installer: "
                         f"powershell -ExecutionPolicy Bypass -File scripts\\install.ps1 -Root \"{self.ws.root}\" "
                         "-Workspace -Report none (run from the worklog folder).")
        if older:
            hints.append(f"{older} older day(s) without reports are not listed.")
        last_run = self._last_report_run()
        if last_run and not last_run.get("ok"):
            still = sorted(set(last_run.get("missing") or []) & {n["day"] for n in needing})
            if still:
                why = last_run.get("error") or (f"exit code {last_run.get('returncode')}"
                                                if last_run.get("returncode") else "it saved nothing")
                hints.append(f"The scheduled report run at {_iso(last_run.get('at'))} did not write the log for "
                             f"{', '.join(still)} ({why}); its output is in {last_run.get('log')}. Tell the user: "
                             "the usual cause is a report_command in config.toml without --model.")
        return {
            "root": str(self.ws.root),
            "initialized": initialized,
            "today": today,
            "day_boundary": self.cfg.day_boundary.strftime("%H:%M"),
            "last_scan": _iso(last_scan),
            "last_scan_seconds": last_scan_row["duration"] if last_scan_row else None,
            "scan_running": scanning,
            "last_close": _iso(last_close["closed_at"]) if last_close else None,
            "last_close_remaining": last_close["remaining"] if last_close else None,
            "tracked_files": con.execute("SELECT COUNT(*) FROM files WHERE present=1").fetchone()[0],
            "pending_close": pending,
            "baseline_remaining": baseline_left,
            "baseline_skipped_old": self.baseline_skipped_old(),
            "background": self.jobs_state(),
            "scheduled_tasks": tasks,
            "today_changes": per_day[today]["reportable"] if today in per_day else 0,
            "days_needing_report": needing,
            "daily_reports": len(self.reports.list_daily()),
            "last_report_run": ({"at": _iso(last_run.get("at")), "ok": bool(last_run.get("ok")),
                                 "missing": last_run.get("missing") or []} if last_run else None),
            "weekly": self.reports.weekly_state(today),
            "topics_active": [
                {"id": t["id"], "name": t.get("name"), "status": t.get("status")}
                for t in self.topics.list() if t.get("status") != "done"
            ],
            "hints": hints,
        }

    # ------------------------------------------------------------------
    # changes / diffs / file text
    # ------------------------------------------------------------------
    @_locked
    def changes(self, day: str | None = None, limit: int = 200, offset: int = 0) -> dict:
        day = _check_day(day or self.cfg.today())
        rows = self.con.execute("SELECT * FROM changes WHERE day=? ORDER BY event_at, id", (day,)).fetchall()
        summary = Counter(r["kind"] for r in rows)
        failures = sum(1 for r in rows if r["ext_error"])
        folders = Counter((r["path"].rsplit("/", 1)[0] if "/" in r["path"] else ".") for r in rows)
        items = []
        for r in rows[offset: offset + limit]:
            item = {"id": r["id"], "time": _hhmm(r["event_at"]), "kind": r["kind"], "path": r["path"]}
            if r["old_path"]:
                item["old_path"] = r["old_path"]
            if r["lines_added"] or r["lines_removed"]:
                item["lines"] = f"+{r['lines_added']} -{r['lines_removed']}"
            item["reader"] = f"{r['ext_kind']}/{r['ext_method']}" if r["ext_kind"] else None
            if r["ext_error"]:
                item["error"] = r["ext_error"]
            if r["no_baseline"]:
                item["no_baseline"] = True
            item["has_diff"] = bool(r["diff_file"])
            items.append({k: v for k, v in item.items() if v is not None})

        recorded = {r["path"] for r in rows} | {r["old_path"] for r in rows if r["old_path"]}
        touched = []
        for e in self.con.execute(
            "SELECT path, MIN(at) AS first, MAX(at) AS last, COUNT(*) AS n FROM scan_events "
            "WHERE at >= ? AND at < ? AND kind IN ('added', 'modified') GROUP BY path ORDER BY first",
            (self.cfg.day_start(day), self.cfg.day_end(day)),
        ):
            if e["path"] in recorded:
                continue
            later = self.con.execute(
                "SELECT day FROM changes WHERE path=? AND day > ? ORDER BY day LIMIT 1", (e["path"], day)
            ).fetchone()
            touched.append({
                "path": e["path"], "seen": f"{_hhmm(e['first'])}-{_hhmm(e['last'])}",
                "note": f"net change recorded on {later['day']}" if later else "not closed out yet, or changed back",
            })
            if len(touched) >= 50:
                break
        d = dt.date.fromisoformat(day)
        return {
            "day": day,
            "weekday": _WEEKDAYS[d.weekday()],
            "total": len(rows),
            "summary": {**dict(summary), "extraction_failed": failures},
            "folders": dict(folders.most_common(15)),
            "changes": items,
            "truncated": offset + limit < len(rows),
            "touched_only": touched,
            "has_notes": (self.ws.notes_dir / f"{day}.md").exists(),
        }

    @_locked
    def diff(self, change_id: int, max_lines: int = 400, offset: int = 0) -> dict:
        r = self.con.execute("SELECT * FROM changes WHERE id=?", (int(change_id),)).fetchone()
        if r is None:
            return {"ok": False, "errors": [f"no change with id {change_id}"]}
        base = {"ok": True, "id": r["id"], "day": r["day"], "path": r["path"], "kind": r["kind"]}
        if r["old_path"]:
            base["old_path"] = r["old_path"]
        if not r["diff_file"]:
            if r["diff_pruned"]:
                note = "the diff was deleted after the retention period"
            elif r["kind"] == "resaved":
                note = "re-saved without content change"
            elif r["kind"] == "renamed":
                note = "moved/renamed; content unchanged"
            elif r["ext_error"]:
                note = f"text could not be extracted: {r['ext_error']}"
            elif r["ext_kind"] == "image":
                note = "image file: no text diff (use read_file for metadata)"
            elif r["ext_kind"] == "binary":
                note = "binary file: tracked by name and size only"
            else:
                note = "no text difference recorded"
            return {**base, "diff": "", "note": note}
        text = self.read_diff(r["diff_file"]) or ""
        lines = [ln for ln in text.splitlines() if not ln.startswith(("--- a/", "+++ b/"))]
        page = lines[offset: offset + max_lines]
        note = None
        if r["kind"] == "added":
            note = "new file: its content is shown as added lines"
        elif r["kind"] == "deleted":
            note = "deleted file: its last known content is shown as removed lines"
        elif r["no_baseline"]:
            note = ("no earlier version was captured: every line shows as added; "
                    "treat it as the file's current content, not as new work")
        out = {**base, "total_lines": len(lines), "offset": offset, "returned": len(page),
               "truncated": offset + len(page) < len(lines), "diff": "\n".join(page)}
        if note:
            out["note"] = note
        return out

    @_locked
    def read_file(self, path: str, max_chars: int = 20000, offset: int = 0) -> dict:
        try:
            rel = self.ws.to_rel(path)
        except ValueError as e:
            return {"ok": False, "errors": [str(e)]}
        row = self.file_row(rel)
        if row is not None:
            rel = row["path"]
        p = self.ws.abs(rel)
        blob = self.ws.text_blob(rel)
        if not p.exists():
            text = self.vault.read(blob) if row is not None and row["has_text"] else None
            if text is None:
                return {"ok": False, "errors": [f"file not found: {rel}"]}
            source = "cache (the file no longer exists)"
        else:
            st = p.stat()
            cached = (row is not None and row["has_text"] and row["ext_size"] == st.st_size
                      and abs((row["ext_mtime"] or 0) - st.st_mtime) <= MTIME_EPS)
            text = self.vault.read(blob) if cached else None
            if text is not None:
                source = "cache"
            else:
                # Fresh extraction is NOT written to the cache: the cached text is
                # the "before" state the next close-out diffs against.
                deadline = (time.monotonic() + self.cfg.agent_call_seconds + HARD_GRACE_SECONDS
                            if self.agent_mode else None)
                with Extractor(self.cfg, deadline=deadline, memory=self.com_memory) as ex:
                    res = ex.extract(p)
                if res.kind == "image":
                    return {"ok": True, "path": rel, "kind": "image", "meta": res.meta,
                            "description": context.media_description(self, row["ext_hash"] if row else None),
                            "note": "image content analysis is not enabled yet; only metadata is available"}
                if res.kind != "text":
                    out = {"ok": res.kind == "binary", "path": rel, "kind": res.kind, "meta": res.meta}
                    if res.error:
                        out["error"] = res.error
                        out["hint"] = ("If another available tool can read DRM-protected documents, try it; "
                                       "otherwise describe the file only by its name and folder.")
                    return out
                text, source = res.text, f"fresh ({res.method})"
        chunk = text[offset: offset + max_chars]
        return {"ok": True, "path": rel, "kind": "text", "source": source, "total_chars": len(text),
                "offset": offset, "returned": len(chunk), "truncated": offset + len(chunk) < len(text),
                "text": chunk}

    # ------------------------------------------------------------------
    # context
    # ------------------------------------------------------------------
    @_locked
    def overview(self, path: str = "", depth: int = 2) -> dict:
        return context.overview(self, path, depth)

    @_locked
    def related(self, path: str) -> dict:
        return context.related(self, path)

    @_locked
    def search(self, query: str, limit: int = 20, path_prefix: str | None = None) -> dict:
        return context.search(self, query, limit, path_prefix)

    @_locked
    def note_add(self, text: str, day: str | None = None) -> dict:
        return context.note_add(self, text, day)

    @_locked
    def notes_get(self, day: str) -> dict:
        return context.notes_get(self, _check_day(day))

    @_locked
    def folder_map_get(self) -> dict:
        return context.folder_map_get(self)

    @_locked
    def folder_map_save(self, content: str) -> dict:
        return context.folder_map_save(self, content)

    # ------------------------------------------------------------------
    # topics & reports
    # ------------------------------------------------------------------
    @_locked
    def topics_list(self, status: str | None = None) -> dict:
        return {"statuses": self.cfg.status_phrases, "topics": self.topics.list(status)}

    @_locked
    def topic_upsert(self, topic_id=None, name=None, description=None, status=None, paths=None, aliases=None) -> dict:
        warnings: list[str] = []
        if paths is not None:
            norm = []
            for p in [paths] if isinstance(paths, str) else paths:
                if not str(p or "").strip():
                    continue
                try:  # "." / "./" / the absolute root all become "."; "A사\\" becomes "A사"
                    norm.append(self.ws.to_rel(str(p), allow_root=True))
                except ValueError as e:
                    return {"ok": False, "errors": [f"paths: {e}"]}
            if "." in norm:
                warnings.append("paths: '.' is the whole work folder, so every file matches this topic; "
                                "list the subfolders or files that belong to it if the topic is narrower")
            paths = norm
        res = self.topics.upsert(topic_id, name, description, status, paths, aliases, today=self.cfg.today())
        if warnings and res.get("ok"):
            res["warnings"] = warnings
        return res

    @_locked
    def daily_save(self, report) -> dict:
        return self.reports.save_daily(report)

    @_locked
    def weekly_save(self, report) -> dict:
        return self.reports.save_weekly(report)

    @_locked
    def report_get(self, kind: str, key: str) -> dict:
        rep = self.reports.get_daily(key) if kind == "daily" else self.reports.get_weekly(key) if kind == "weekly" else None
        if rep is None:
            return {"ok": False, "errors": [f"no {kind} report {key!r}"]}
        return {"ok": True, "report": rep}

    @_locked
    def reports_list(self, kind: str = "daily", date_from: str | None = None, date_to: str | None = None) -> dict:
        if kind == "weekly":
            return {"kind": "weekly", "reports": self.reports.list_weekly()}
        return {"kind": "daily", "reports": self.reports.list_daily(date_from, date_to)}

    @_locked
    def render(self, kind: str, key: str, fmt: str = "text") -> dict:
        return self.reports.render(kind, key, fmt)

    # ------------------------------------------------------------------
    # background jobs (agent mode): long Office work runs in its own process
    # ------------------------------------------------------------------
    def _spawn(self, args: list[str]) -> tuple[subprocess.Popen, bool | None]:
        """Start ``python -m worklog <args> --root <root>`` in its own process, without a window.

        Its own process because Office work can take long and must not share Office with
        a tool call of this one. It gets no handle to this process's stdin/stdout, which
        carry the MCP channel. Returns the process and whether it outlives the agent app's
        session (``survives_session``).
        """
        cmd = [sys.executable, "-m", "worklog", *args, "--root", str(self.ws.root)]
        base = _NO_WINDOW | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        breakaway = getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0)
        for flags in ([base | breakaway, base] if breakaway else [base]):
            try:
                proc = subprocess.Popen(cmd, cwd=str(self.ws.root), stdin=subprocess.DEVNULL,
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                        creationflags=flags, close_fds=True)
                break
            except OSError:  # breaking away is refused inside a job that forbids it
                if flags == base:
                    raise
        return proc, survives_session(proc.pid)

    def _baseline_running(self) -> dict | None:
        """A running baseline: any process's marker, or one this server started that has not set it yet."""
        info = JobMarker.probe(self.ws, "baseline")
        if info is not None:
            return {"since": _iso(info.get("started")), "pid": info.get("pid")}
        own = self._procs.get("baseline")
        if own is not None and own[0].poll() is None:
            return {"since": _iso(own[1]), "pid": own[0].pid, "starting": True}
        return None

    def jobs_state(self) -> dict:
        out: dict = {}
        b = self._baseline_running()
        if b is not None:
            out["baseline"] = {"running": True, **b}
        d = self._procs.get("doctor")
        if d is not None and d[0].poll() is None:
            out["doctor"] = {"running": True, "since": _iso(d[1])}
        return out

    @_locked
    def start_baseline(self, minutes: float = 15) -> dict:
        """Run the baseline in a background process for up to ``minutes``; returns at once."""
        if not self.initialized:
            return {"ok": False, "errors": ["tracking has not started for this work folder: call init first"]}
        left = self.baseline_remaining()
        running = self._baseline_running()
        if running is not None:
            return {"ok": True, "running": True, "since": running["since"], "remaining": left,
                    "message": "A baseline run is already going in the background. Nothing to wait for."}
        if not left:
            return {"ok": True, "remaining": 0, "message": "Every file that needs a baseline has one."}
        proc, survives = self._spawn(["baseline", "--budget", str(int(minutes * 60))])
        self._procs["baseline"] = (proc, time.time())
        msg = (f"The baseline runs in the background for up to {minutes:g} minutes, reading files through "
               "Office where needed. It steps aside whenever a close-out needs the index and then goes on, so "
               "keep working. Do not wait for it or call baseline again for it: status shows the progress "
               "(baseline_remaining).")
        if survives is not True:
            later = ("the morning task continues it" if (find_scheduled_tasks(self.ws.root) or {}).get("morning")
                     else "call baseline again in a later session to continue")
            msg += (f" This agent app {'ends' if survives is False else 'may end'} background runs together with "
                    f"the session, so it can stop early; the progress made is kept and {later}.")
        return {"ok": True, "started": True, "pid": proc.pid, "minutes": minutes, "remaining": left,
                "survives_session": survives, "message": msg}

    @_locked
    def doctor_job(self, rerun: bool = False, wait: float = 0) -> dict:
        """The readability check (``worklog doctor``) as a background process.

        Starts it when there is no result yet (or ``rerun``), waits up to ``wait``
        seconds, and returns the result once there is one.
        """
        out_path = self.ws.logs_dir / "doctor-latest.json"
        info = self._procs.get("doctor")
        running = info is not None and info[0].poll() is None
        if not running and (rerun or not out_path.exists()):
            self.ws.logs_dir.mkdir(parents=True, exist_ok=True)
            out_path.unlink(missing_ok=True)
            info = self._procs["doctor"] = (self._spawn(["doctor", "--no-preview", "--out", str(out_path)])[0],
                                            time.time())
            running = True
        if running:
            try:
                info[0].wait(timeout=max(0.0, wait))
            except subprocess.TimeoutExpired:
                return {"ok": True, "running": True, "since": _iso(info[1]),
                        "message": "The check reads up to 3 recent files of each document type directly and "
                                   "through Word, Excel and PowerPoint; it usually takes 1-3 minutes. Call doctor "
                                   "again in a minute for the result."}
        if not out_path.exists():
            code = info[0].returncode if info is not None else None
            return {"ok": False, "errors": [f"the check ended without a result (exit code {code}); its log is "
                                            f"{self.ws.logs_dir / 'worklog.log'}"]}
        report = json.loads(out_path.read_text(encoding="utf-8"))
        return {"ok": True, "checked_at": report.get("time"), "verdicts": report.get("verdicts", []),
                "office": report.get("office"), "session": report.get("session"),
                "files_tested": len(report.get("files", [])), "report": str(out_path),
                "note": "This is the latest result; doctor(rerun=true) checks again."}

    # ------------------------------------------------------------------
    # automation
    # ------------------------------------------------------------------
    def morning(self) -> dict:
        """Scheduled routine: close out, continue the baseline, start the report agent.

        The agent's exit code is not trusted: opencode exits 0 even when the model
        never called a tool. Success means every day that needed a report has one
        afterwards; anything else makes the result ``ok: false`` and is shown by
        ``status`` until those days are written.
        """
        lock = FileLock(self.ws.lock_path("morning"))
        if not lock.acquire():
            return {"ok": False, "busy": True, "message": "a morning run is already in progress"}
        try:
            out: dict = {"ok": True, "close": self.close(None)}
            if self.baseline_remaining() and self.cfg.baseline_minutes_per_run > 0:
                out["baseline"] = self.baseline(self.cfg.baseline_minutes_per_run * 60)
            days = [d["day"] for d in self.status()["days_needing_report"]]
            out["days_needing_report"] = days
            if self.cfg.report_command and days:
                run = self._run_report_command()
                left = {d["day"] for d in self.status()["days_needing_report"]}
                run["written"] = [d for d in days if d not in left]
                run["missing"] = [d for d in days if d in left]
                run["ok"] = not run.get("error") and run.get("returncode") == 0 and not run["missing"]
                if not run["ok"]:
                    run["log_tail"] = log_tail(Path(run["log"]))
                    out["ok"] = False
                    log.warning("morning: report command %s; %d of %d day(s) still without a report (%s); "
                                "output: %s", run.get("error") or f"exited with {run.get('returncode')}",
                                len(run["missing"]), len(days), ", ".join(run["missing"]) or "-", run["log"])
                else:
                    log.info("morning: reports written for %s", ", ".join(run["written"]))
                out["report_command"] = run
                with self._lock:
                    set_meta(self.con, "last_report_run", json.dumps(
                        {"at": time.time(), **{k: run.get(k) for k in
                                               ("ok", "returncode", "error", "written", "missing", "log")}},
                        ensure_ascii=False))
                    self.con.commit()
            return out
        finally:
            lock.release()

    def _last_report_run(self) -> dict | None:
        raw = get_meta(self.con, "last_report_run")
        try:
            return json.loads(raw) if raw else None
        except json.JSONDecodeError:
            return None

    def _run_report_command(self) -> dict:
        cmd = list(self.cfg.report_command)
        exe = shutil.which(cmd[0]) or cmd[0]
        cmd = (["cmd.exe", "/c", exe] if exe.lower().endswith((".cmd", ".bat")) else [exe]) + cmd[1:]
        self.ws.logs_dir.mkdir(parents=True, exist_ok=True)
        log_path = self.ws.logs_dir / f"report-{dt.datetime.now():%Y%m%d-%H%M%S}.log"
        out: dict = {"command": cmd, "log": str(log_path)}
        # Ask for plain output; escapes that are printed anyway are stripped below.
        env = {**os.environ, "NO_COLOR": "1", "FORCE_COLOR": "0"}
        try:
            with log_path.open("wb") as f:
                proc = subprocess.run(cmd, cwd=self.ws.root, stdout=f, stderr=subprocess.STDOUT,
                                      stdin=subprocess.DEVNULL, timeout=self.cfg.report_timeout_min * 60,
                                      creationflags=_NO_WINDOW, env=env)
            out["returncode"] = proc.returncode
        except subprocess.TimeoutExpired:
            out["error"] = f"timed out after {self.cfg.report_timeout_min:g} min"
        except OSError as e:
            out["error"] = str(e)
        try:
            data = log_path.read_bytes()
            clean = strip_ansi(data)
            if clean != data:
                log_path.write_bytes(clean)
        except OSError:
            pass
        return out
