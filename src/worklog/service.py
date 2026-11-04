"""Worklog facade: the single API behind the CLI and the MCP server."""

from __future__ import annotations

import datetime as dt
import functools
import json
import logging
import shutil
import subprocess
import sys
import threading
import time
from collections import Counter, defaultdict
from pathlib import Path

from . import context
from .closeout import candidate_counts, run_baseline, run_close
from .config import Config, set_report_command, write_default_config
from .db import connect, get_meta
from .extract import Extractor
from .ignore import IgnoreRules
from .reports import Reports
from .scanner import MTIME_EPS
from .scanner import scan as scan_folder
from .storage import FileLock, TextVault, Workspace, atomic_write_text, hide_path
from .topics import TopicStore

log = logging.getLogger(__name__)

_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
_WEEKDAYS = "월화수목금토일"
MAX_LISTED_DAYS = 31


class WorklogError(Exception):
    pass


def _locked(fn):
    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return fn(self, *args, **kwargs)

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

    def baseline_remaining(self) -> int:
        installed = get_meta(self.con, "installed_at")
        if installed is None:
            return 0
        return self.con.execute(
            "SELECT COUNT(*) FROM files WHERE present=1 AND ext_at IS NULL AND origin='init' AND mtime <= ?",
            (float(installed),),
        ).fetchone()[0]

    def _maybe_scan(self, max_age: float = 600) -> None:
        last = self.con.execute("SELECT MAX(taken_at) FROM scans").fetchone()[0]
        if last is None or time.time() - last > max_age:
            self.scan()

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
        return run_close(self, budget_seconds)

    @_locked
    def baseline(self, budget_seconds: float | None = None) -> dict:
        return run_baseline(self, budget_seconds)

    # ------------------------------------------------------------------
    # status
    # ------------------------------------------------------------------
    @_locked
    def status(self) -> dict:
        self._maybe_scan()
        con = self.con
        now = time.time()
        today = self.cfg.today(now)
        last_scan = con.execute("SELECT MAX(taken_at) FROM scans").fetchone()[0]
        last_close = con.execute("SELECT * FROM closes ORDER BY id DESC LIMIT 1").fetchone()
        pending = candidate_counts(self)
        baseline_left = self.baseline_remaining()

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
        if pending["changed"]:
            hints.append(f"{pending['changed']} file change(s) are not closed out yet: call close "
                         "(repeat while 'remaining' > 0) before writing reports.")
        if pending["retry_failed"]:
            hints.append(f"{pending['retry_failed']} file(s) failed text extraction earlier and will be retried "
                         "at the next close.")
        if baseline_left:
            hints.append(f"Baseline extraction pending for {baseline_left} file(s): their first change will show "
                         "full content instead of a diff. The 'baseline' tool or `worklog baseline` continues it.")
        if older:
            hints.append(f"{older} older day(s) without reports are not listed.")
        return {
            "root": str(self.ws.root),
            "today": today,
            "day_boundary": self.cfg.day_boundary.strftime("%H:%M"),
            "last_scan": _iso(last_scan),
            "last_close": _iso(last_close["closed_at"]) if last_close else None,
            "last_close_remaining": last_close["remaining"] if last_close else None,
            "tracked_files": con.execute("SELECT COUNT(*) FROM files WHERE present=1").fetchone()[0],
            "pending_close": pending,
            "baseline_remaining": baseline_left,
            "today_changes": per_day[today]["reportable"] if today in per_day else 0,
            "days_needing_report": needing,
            "daily_reports": len(self.reports.list_daily()),
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
                with Extractor(self.cfg) as ex:
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
        return self.topics.upsert(topic_id, name, description, status, paths, aliases, today=self.cfg.today())

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
    # automation
    # ------------------------------------------------------------------
    def morning(self) -> dict:
        """Scheduled routine: close out, continue the baseline, start the report agent."""
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
                out["report_command"] = self._run_report_command()
            return out
        finally:
            lock.release()

    def _run_report_command(self) -> dict:
        cmd = list(self.cfg.report_command)
        exe = shutil.which(cmd[0]) or cmd[0]
        cmd = (["cmd.exe", "/c", exe] if exe.lower().endswith((".cmd", ".bat")) else [exe]) + cmd[1:]
        self.ws.logs_dir.mkdir(parents=True, exist_ok=True)
        log_path = self.ws.logs_dir / f"report-{dt.datetime.now():%Y%m%d-%H%M%S}.log"
        try:
            with log_path.open("wb") as f:
                proc = subprocess.run(cmd, cwd=self.ws.root, stdout=f, stderr=subprocess.STDOUT,
                                      stdin=subprocess.DEVNULL, timeout=self.cfg.report_timeout_min * 60,
                                      creationflags=_NO_WINDOW)
            return {"command": cmd, "returncode": proc.returncode, "log": str(log_path)}
        except subprocess.TimeoutExpired:
            return {"command": cmd, "error": "timed out", "log": str(log_path)}
        except OSError as e:
            return {"command": cmd, "error": str(e), "log": str(log_path)}
