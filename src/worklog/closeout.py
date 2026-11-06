"""Close-out and baseline.

close-out: for every file changed since its last extraction, hash it, extract
its text, diff against the previously stored text and write a change record.
Records are committed one file at a time, so an interrupted run never loses
a change and never blocks the hourly scan for long. All per-file work (hashing
included) happens under the time budget.

baseline: extract the text of files that already existed at install time so
their first later change can be shown as a diff. It steps aside whenever a
close-out is waiting for the shared lock; a run with a budget then resumes.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from .db import get_meta, set_meta
from .diffing import make_diff
from .extract import Extractor
from .scanner import MTIME_EPS
from .storage import FileLock, JobMarker, Workspace, sha256_file, sha256_text

if TYPE_CHECKING:  # pragma: no cover
    from .service import Worklog

log = logging.getLogger(__name__)

MAX_RETRIES = 3
SCAN_EVENT_RETENTION_DAYS = 180
# Agent calls: Office waits end this long after the budget, so a call returns in time.
HARD_GRACE_SECONDS = 15.0
# Agent calls: a scan that ended less than this ago is reused (e.g. the one status just made).
SCAN_REUSE_SECONDS = 60.0
# Agent calls continuing a close-out (the previous one left 'remaining' > 0 and ended less
# than CONTINUE_WINDOW ago) reuse a scan up to CONTINUE_SCAN_MAX_AGE old, so the
# "close until remaining is 0" loop walks the folder once, not once per call.
CONTINUE_WINDOW = 180.0
CONTINUE_SCAN_MAX_AGE = 1800.0
# CLI close-outs wait this long for a baseline run to pause and free the lock.
CLI_CLOSE_LOCK_WAIT = 120.0
_CLOSE_WANTED = "close.wanted"
_CLOSE_WANTED_STALE = 600.0


@dataclass
class Limits:
    """Time limits of one run. ``budget``: no new file starts after it (at least one
    always runs). ``hard``: Office waits are capped to end by then (agent calls)."""

    t0: float
    budget: float | None = None
    hard: float | None = None

    def over_budget(self) -> bool:
        return self.budget is not None and time.monotonic() - self.t0 >= self.budget

    def budget_left(self) -> float:
        return float("inf") if self.budget is None else max(0.0, self.t0 + self.budget - time.monotonic())

    @property
    def hard_deadline(self) -> float | None:
        return None if self.hard is None else self.t0 + self.hard


def baseline_where(wl: "Worklog") -> tuple[str, list]:
    """SQL condition (and parameters) for files still waiting for their baseline."""
    installed = float(get_meta(wl.con, "installed_at") or time.time())
    where = "present=1 AND ext_at IS NULL AND origin='init' AND mtime <= ?"
    params: list = [installed]
    if wl.cfg.baseline_max_age_days > 0:
        where += " AND mtime >= ?"
        params.append(installed - wl.cfg.baseline_max_age_days * 86400)
    return where, params


# --- close/baseline handshake -------------------------------------------------
# Close-out and baseline share one lock. A baseline can run for hours, so a close-out
# that finds the lock taken leaves a marker and waits; the baseline stops at the next
# file boundary when it sees the marker.
def _close_wanted_path(ws: Workspace) -> Path:
    return ws.locks_dir / _CLOSE_WANTED


def close_waiting(ws: Workspace) -> bool:
    try:
        return time.time() - _close_wanted_path(ws).stat().st_mtime < _CLOSE_WANTED_STALE
    except OSError:
        return False


def acquire_for_close(ws: Workspace, lock: FileLock, wait: float) -> bool:
    if lock.acquire():
        return True
    if wait <= 0:
        return False
    marker = _close_wanted_path(ws)
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.touch()
    except OSError:
        pass
    try:
        return lock.acquire_wait(wait)
    finally:
        try:
            marker.unlink()
        except OSError:
            pass

CANDIDATES_SQL = """
SELECT * FROM files WHERE
   present = 0
OR (present = 1 AND ext_at IS NOT NULL AND (size != ext_size OR abs(mtime - ext_mtime) > :eps))
OR (present = 1 AND ext_at IS NULL AND (origin = 'new' OR mtime > :installed))
OR (present = 1 AND ext_kind = 'failed' AND ext_attempts < :retries)
"""

_UPDATE_EXT = (
    "UPDATE files SET ext_size=?, ext_mtime=?, ext_hash=?, text_hash=?, ext_kind=?, ext_method=?, "
    "ext_error=?, ext_at=?, ext_attempts=?, has_text=? WHERE id=?"
)


def candidate_counts(wl: "Worklog") -> dict:
    installed = float(get_meta(wl.con, "installed_at") or time.time())
    params = {"eps": MTIME_EPS, "installed": installed, "retries": MAX_RETRIES}
    rows = wl.con.execute(CANDIDATES_SQL, params).fetchall()
    retry = sum(1 for r in rows if r["present"] and r["ext_kind"] == "failed"
                and r["ext_size"] == r["size"] and abs((r["ext_mtime"] or 0) - r["mtime"]) <= MTIME_EPS)
    return {"changed": len(rows) - retry, "retry_failed": retry}


@dataclass
class Rec:
    path: str
    kind: str
    event_at: float
    old_path: str | None = None
    size: int | None = None
    raw_hash: str | None = None
    text_hash: str | None = None
    added: int = 0
    removed: int = 0
    ext_kind: str | None = None
    ext_method: str | None = None
    ext_error: str | None = None
    no_baseline: bool = False
    diff: str | None = None


class _Close:
    def __init__(self, wl: "Worklog", now: float):
        self.wl = wl
        self.con = wl.con
        self.ws = wl.ws
        self.cfg = wl.cfg
        self.vault = wl.vault
        self.now = now
        self.installed = float(get_meta(self.con, "installed_at") or now)
        self.close_id: int | None = None
        self.kinds: Counter = Counter()
        self.days: Counter = Counter()
        self.errors: list[dict] = []
        self.failures: list[dict] = []
        self.revised = 0

    # --- time attribution ------------------------------------------------
    def _prev_scan_time(self, at: float) -> float:
        row = self.con.execute("SELECT MAX(taken_at) FROM scans WHERE taken_at < ?", (at,)).fetchone()
        return row[0] if row and row[0] is not None else self.installed

    def _first_event(self, path: str, lower: float, kinds: tuple[str, ...]) -> float | None:
        marks = ",".join("?" * len(kinds))
        row = self.con.execute(
            f"SELECT MIN(at) FROM scan_events WHERE path=? AND at>=? AND kind IN ({marks})",
            (path, lower, *kinds),
        ).fetchone()
        return row[0] if row and row[0] is not None else None

    def _event_at(self, row, mtime: float) -> float:
        """When did this change happen? The file's mtime, unless it is implausible.

        A copied-in file keeps its original (possibly years old) mtime; then the
        time the hourly scan first noticed it is used instead.
        """
        if row["ext_at"] is not None:
            lower = row["ext_at"]
        elif row["origin"] == "init":
            lower = self.installed
        else:
            lower = self._prev_scan_time(row["first_seen"])
        if lower - 1 <= mtime <= self.now + 120:
            return mtime
        ev = self._first_event(row["path"], lower, ("added", "modified"))
        return ev if ev is not None else self.now

    # --- persistence -----------------------------------------------------
    def _begin(self, started_at: float) -> None:
        cur = self.con.execute(
            "INSERT INTO closes(started_at, closed_at, n_changes, remaining, budget_hit) VALUES(?, ?, 0, 0, 0)",
            (started_at, started_at),
        )
        self.close_id = cur.lastrowid
        self.con.commit()

    def _store(self, rec: Rec, sql: str | None = None, params: tuple = ()) -> None:
        """Insert one change record (and its files-row update) atomically."""
        day = self.cfg.logical_day(rec.event_at)
        cur = self.con.execute(
            "INSERT INTO changes(close_id, day, event_at, path, old_path, kind, size, raw_hash, text_hash, "
            "lines_added, lines_removed, ext_kind, ext_method, ext_error, no_baseline) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (self.close_id, day, rec.event_at, rec.path, rec.old_path, rec.kind, rec.size, rec.raw_hash,
             rec.text_hash, rec.added, rec.removed, rec.ext_kind, rec.ext_method, rec.ext_error,
             int(rec.no_baseline)),
        )
        if rec.diff:
            diff_file = self.wl.write_diff(day, cur.lastrowid, rec.diff)
            self.con.execute("UPDATE changes SET diff_file=? WHERE id=?", (diff_file, cur.lastrowid))
        if sql:
            self.con.execute(sql, params)
        self.con.commit()
        self.kinds[rec.kind] += 1
        self.days[day] += 1
        if rec.ext_error:
            self.failures.append({"path": rec.path, "error": rec.ext_error})

    def _finish(self, remaining: int, budget_hit: bool) -> None:
        self.con.execute(
            "UPDATE closes SET closed_at=?, n_changes=?, remaining=?, budget_hit=? WHERE id=?",
            (time.time(), sum(self.kinds.values()), remaining, int(budget_hit), self.close_id),
        )
        self.con.commit()

    # --- main --------------------------------------------------------------
    def _stat_hash(self, r):
        p = self.ws.abs(r["path"])
        try:
            return p.stat(), sha256_file(p)
        except OSError as e:
            self.errors.append({"path": r["path"], "error": f"cannot read file: {e}"})
            return None

    def run(self, limits: Limits) -> dict:
        self._begin(self.now)
        params = {"eps": MTIME_EPS, "installed": self.installed, "retries": MAX_RETRIES}
        rows = self.con.execute(CANDIDATES_SQL, params).fetchall()
        deleted = [r for r in rows if not r["present"]]
        present = [r for r in rows if r["present"]]
        finished = 0  # candidates fully handled in this run; the rest is 'remaining'
        units = 0  # files hashed or recorded: at least one per run, so repeated calls progress
        budget_hit = False

        def out_of_time() -> bool:
            nonlocal budget_hit
            if units and limits.over_budget():
                budget_hit = True
            return budget_hit

        # Renames/moves: a vanished file whose content (or size+mtime for never-extracted
        # files) reappears at a new path. A move keeps the size, so only new files with
        # the size of a vanished file need hashing here.
        vanished_sizes = {d["size"] for d in deleted}
        renames = [r for r in present
                   if r["ext_at"] is None and r["origin"] == "new" and r["size"] in vanished_sizes]
        rename_ids = {r["id"] for r in renames}
        by_hash: dict[str, list] = {}
        by_sig: dict[tuple, list] = {}
        for d in deleted:
            if d["ext_hash"]:
                by_hash.setdefault(d["ext_hash"], []).append(d)
            elif d["origin"] == "init":
                by_sig.setdefault((d["size"], round(d["mtime"], 3)), []).append(d)
        used: set[int] = set()
        unmatched: list[tuple] = []  # rename candidates that are plain additions: (row, stat, hash)
        for r in renames:
            if out_of_time():
                break
            units += 1
            got = self._stat_hash(r)
            if got is None:
                finished += 1
                continue
            st, h = got
            match = next((d for d in by_hash.get(h, []) if d["id"] not in used), None)
            if match is None:
                sig = (st.st_size, round(st.st_mtime, 3))
                match = next((d for d in by_sig.get(sig, []) if d["id"] not in used), None)
            if match is not None:
                used.add(match["id"])
                self._rename(match, r, st, h)
                finished += 2  # the new path and the vanished one
            else:
                unmatched.append((r, st, h))

        # Deletions, once every possible rename has been looked at.
        if not budget_hit:
            for d in deleted:
                if d["id"] in used:
                    continue
                if out_of_time():
                    break
                units += 1
                self._delete(d)
                finished += 1

        # Changes, additions and retries (the expensive part), oldest first so earlier
        # days are complete when the budget runs out. Hashing happens here, per file.
        work = [(r, None, None) for r in present if r["id"] not in rename_ids] + unmatched
        work.sort(key=lambda it: it[0]["mtime"])
        if work and not budget_hit:
            with Extractor(self.cfg, deadline=limits.hard_deadline, memory=self.wl.com_memory) as ex:
                for r, st, h in work:
                    if out_of_time():
                        break
                    units += 1
                    try:
                        if h is None:
                            got = self._stat_hash(r)
                            if got is None:
                                finished += 1
                                continue
                            st, h = got
                        if r["ext_hash"] and h == r["ext_hash"]:
                            if r["ext_kind"] == "failed":
                                self._retry(ex, r, st, h)
                            else:  # only the timestamp changed
                                self.con.execute("UPDATE files SET ext_size=?, ext_mtime=? WHERE id=?",
                                                 (st.st_size, st.st_mtime, r["id"]))
                                self.con.commit()
                        else:
                            self._change(ex, r, st, h)
                    except Exception as e:  # never let one file abort the run
                        log.exception("close-out failed for %s", r["path"])
                        self.errors.append({"path": r["path"], "error": f"{type(e).__name__}: {e}"})
                        self.con.rollback()
                    finished += 1
        remaining = len(rows) - finished
        self._finish(remaining, budget_hit)
        return {
            "ok": True,
            "close_id": self.close_id,
            "changes": sum(self.kinds.values()),
            "by_kind": dict(self.kinds),
            "days": dict(sorted(self.days.items())),
            "revised_earlier_changes": self.revised,
            "remaining": remaining,
            "budget_hit": budget_hit,
            "extraction_failures": self.failures[:20],
            "errors": self.errors[:20],
        }

    # --- record builders -------------------------------------------------
    def _change(self, ex: Extractor, r, st, h: str) -> None:
        path = r["path"]
        res = ex.extract(self.ws.abs(path))
        blob = self.ws.text_blob(path)
        had_text = bool(r["has_text"])
        kind = "added" if (r["origin"] == "new" and r["ext_at"] is None) else "modified"
        rec = Rec(path=path, kind=kind, event_at=self._event_at(r, st.st_mtime), size=st.st_size,
                  raw_hash=h, ext_kind=res.kind, ext_method=res.method)
        text_hash, has_text = r["text_hash"], had_text
        if res.kind == "text":
            th = sha256_text(res.text)
            if had_text and th == r["text_hash"]:
                rec.kind = "resaved"  # bytes changed, content did not (re-save / DRM re-encryption)
            else:
                old = self.vault.read(blob) if had_text else None
                rec.no_baseline = kind == "modified" and old is None
                rec.diff, rec.added, rec.removed = make_diff(old or "", res.text, path)
            self.vault.write(blob, res.text)
            rec.text_hash = text_hash = th
            has_text = True
        elif res.kind == "image":
            self.wl.media_record(h, path, res.meta)
        elif res.kind == "failed":
            rec.ext_error = res.error
        attempts = 1 if res.kind == "failed" else 0
        self._store(rec, _UPDATE_EXT, (st.st_size, st.st_mtime, h, text_hash, res.kind, res.method, res.error,
                                       self.now, attempts, int(has_text), r["id"]))

    def _retry(self, ex: Extractor, r, st, h: str) -> None:
        """Re-attempt a failed extraction of unchanged bytes; fill in the earlier record."""
        path = r["path"]
        res = ex.extract(self.ws.abs(path))
        if res.kind != "text":
            self.con.execute(
                "UPDATE files SET ext_attempts = ext_attempts + 1, ext_error=? WHERE id=?", (res.error, r["id"])
            )
            self.con.commit()
            return
        blob = self.ws.text_blob(path)
        old = self.vault.read(blob) if r["has_text"] else None
        th = sha256_text(res.text)
        prev = self.con.execute(
            "SELECT * FROM changes WHERE path=? AND ext_error IS NOT NULL ORDER BY id DESC LIMIT 1", (path,)
        ).fetchone()
        if prev is not None:
            if old is not None and th == r["text_hash"]:
                kind, diff, added, removed = "resaved", None, 0, 0
            else:
                kind = prev["kind"]
                diff, added, removed = make_diff(old or "", res.text, path)
            diff_file = self.wl.write_diff(prev["day"], prev["id"], diff) if diff else None
            self.con.execute(
                "UPDATE changes SET kind=?, text_hash=?, lines_added=?, lines_removed=?, ext_kind='text', "
                "ext_method=?, ext_error=NULL, no_baseline=?, diff_file=?, revised_at=? WHERE id=?",
                (kind, th, added, removed, res.method, int(old is None and prev["kind"] == "modified"),
                 diff_file, self.now, prev["id"]),
            )
            self.revised += 1
        self.vault.write(blob, res.text)
        self.con.execute(
            "UPDATE files SET text_hash=?, ext_kind='text', ext_method=?, ext_error=NULL, ext_attempts=0, "
            "has_text=1, ext_at=? WHERE id=?",
            (th, res.method, self.now, r["id"]),
        )
        self.con.commit()

    def _delete(self, d) -> None:
        path = d["path"]
        blob = self.ws.text_blob(path)
        if d["origin"] == "new" and d["ext_at"] is None:
            # Appeared and disappeared between two close-outs: transient file.
            self.con.execute("DELETE FROM files WHERE id=?", (d["id"],))
            self.con.commit()
            return
        row = self.con.execute(
            "SELECT MAX(at) FROM scan_events WHERE path=? AND kind='deleted'", (path,)
        ).fetchone()
        rec = Rec(path=path, kind="deleted", event_at=(row[0] if row and row[0] else self.now),
                  size=d["size"], raw_hash=d["ext_hash"], ext_kind=d["ext_kind"], ext_method=d["ext_method"],
                  no_baseline=d["ext_at"] is None)
        if d["has_text"]:
            old = self.vault.read(blob)
            if old:
                rec.diff, rec.added, rec.removed = make_diff(old, "", path)
        self._store(rec, "DELETE FROM files WHERE id=?", (d["id"],))
        try:
            blob.unlink()
        except FileNotFoundError:
            pass

    def _rename(self, d, r, st, h: str) -> None:
        ev = self._first_event(r["path"], self._prev_scan_time(r["first_seen"]), ("added",))
        rec = Rec(path=r["path"], kind="renamed", old_path=d["path"], event_at=ev or self.now, size=st.st_size,
                  raw_hash=h, text_hash=d["text_hash"], ext_kind=d["ext_kind"], ext_method=d["ext_method"])
        old_blob, new_blob = self.ws.text_blob(d["path"]), self.ws.text_blob(r["path"])
        if d["has_text"] and old_blob.exists():
            new_blob.parent.mkdir(parents=True, exist_ok=True)
            os.replace(old_blob, new_blob)
        if d["ext_at"] is not None:
            sql = (
                "UPDATE files SET ext_size=?, ext_mtime=?, ext_hash=?, text_hash=?, ext_kind=?, ext_method=?, "
                "ext_error=?, ext_at=?, ext_attempts=?, has_text=?, origin=? WHERE id=?"
            )
            params = (st.st_size, st.st_mtime, h, d["text_hash"], d["ext_kind"], d["ext_method"], d["ext_error"],
                      self.now, d["ext_attempts"], d["has_text"], d["origin"], r["id"])
        else:  # never extracted: inherit 'init' so the baseline job picks it up
            sql, params = "UPDATE files SET origin=? WHERE id=?", (d["origin"], r["id"])
        self.con.execute("DELETE FROM files WHERE id=?", (d["id"],))
        self._store(rec, sql, params)


def run_close(wl: "Worklog", budget_seconds: float | None = None) -> dict:
    """Close out pending changes.

    In agent mode (MCP) the run is shaped to fit one tool call: a scan younger than
    ``SCAN_REUSE_SECONDS`` is reused, a slow scan continues in the background (the call
    answers ``busy``), and Office waits end ``HARD_GRACE_SECONDS`` after the budget.
    """
    t0 = time.monotonic()
    agent = wl.agent_mode and budget_seconds is not None
    limits = Limits(t0, budget_seconds, budget_seconds + HARD_GRACE_SECONDS if agent else None)
    lock = FileLock(wl.ws.lock_path("close"))
    if not acquire_for_close(wl.ws, lock, limits.budget_left() if agent else CLI_CLOSE_LOCK_WAIT):
        return {"ok": False, "busy": True,
                "message": "another close-out, or a baseline run that has not paused yet, holds the lock "
                           "(the morning task or a background run); call close again in a minute"}
    try:
        if agent:
            prev = wl.con.execute("SELECT closed_at, remaining FROM closes ORDER BY id DESC LIMIT 1").fetchone()
            continuing = prev is not None and prev["remaining"] > 0 and time.time() - prev["closed_at"] < CONTINUE_WINDOW
            max_age = CONTINUE_SCAN_MAX_AGE if continuing else SCAN_REUSE_SECONDS
            scan_info = wl.ensure_scan(max_age, wait=max(1.0, limits.budget_left()))
            if not scan_info.get("ok"):
                return scan_info
        else:
            scan_info = wl.scan()
        now = time.time()
        result = _Close(wl, now).run(limits)
        result["scan"] = scan_info
        prune(wl, now)
        return result
    finally:
        lock.release()


def _wait_out_close(ws: Workspace, lock: FileLock, deadline: float) -> bool:
    """After yielding: let the waiting close-out take the lock, then take it back once it is done."""
    while close_waiting(ws) and time.monotonic() < deadline:
        time.sleep(0.25)  # the close-out removes its marker only after it holds the lock
    return lock.acquire_wait(max(0.0, deadline - time.monotonic()))


def _baseline_pass(wl: "Worklog", t0: float, budget_seconds: float | None, first: bool) -> dict:
    con, ws = wl.con, wl.ws
    now = time.time()
    installed = float(get_meta(con, "installed_at") or now)
    where, params = baseline_where(wl)
    rows = con.execute(f"SELECT * FROM files WHERE {where} ORDER BY mtime DESC", params).fetchall()
    n = {"processed": 0, "failed": 0, "skipped_changed": 0, "budget_hit": False, "yielded": False}
    if not rows:
        return n
    # A fresh Extractor per pass: Office instances are not kept open while a close-out runs its own.
    with Extractor(wl.cfg, memory=wl.com_memory) as ex:
        for r in rows:
            started = n["processed"] or not first  # every run does at least one file
            if started and budget_seconds is not None and time.monotonic() - t0 >= budget_seconds:
                n["budget_hit"] = True
                break
            if n["processed"] and close_waiting(ws):
                n["yielded"] = True  # a close-out is waiting for the lock: let it run
                break
            p = ws.abs(r["path"])
            try:
                st = p.stat()
                if st.st_mtime > installed or st.st_size != r["size"]:
                    n["skipped_changed"] += 1  # changed after install: the close-out reports it
                    continue
                h = sha256_file(p)
            except OSError:
                n["skipped_changed"] += 1
                continue
            res = ex.extract(p)
            th, has_text = None, 0
            if res.kind == "text":
                th, has_text = sha256_text(res.text), 1
                wl.vault.write(ws.text_blob(r["path"]), res.text)
            elif res.kind == "image":
                wl.media_record(h, r["path"], res.meta)
            elif res.kind == "failed":
                n["failed"] += 1
            # Baseline failures are not retried; the file gets text when it next changes.
            attempts = MAX_RETRIES if res.kind == "failed" else 0
            con.execute(_UPDATE_EXT, (st.st_size, st.st_mtime, h, th, res.kind, res.method, res.error,
                                      now, attempts, has_text, r["id"]))
            con.commit()
            n["processed"] += 1
    return n


def run_baseline(wl: "Worklog", budget_seconds: float | None = None) -> dict:
    """Extract text for files present at install, newest first, until the budget is used.

    It yields to a waiting close-out between files. A run with a budget (a background job or
    the morning task) then waits for that close-out and goes on; a run without one returns.
    """
    lock = FileLock(wl.ws.lock_path("close"))
    if not lock.acquire():
        return {"ok": False, "busy": True, "message": "a close-out or baseline run is already in progress"}
    held = True
    marker = JobMarker(wl.ws, "baseline")
    marker.begin(budget_seconds=budget_seconds)
    try:
        t0 = time.monotonic()
        total = {"processed": 0, "failed": 0, "skipped_changed": 0}
        resumed = 0
        while True:
            n = _baseline_pass(wl, t0, budget_seconds, first=resumed == 0)
            for k in total:
                total[k] += n[k]
            if not n["yielded"] or budget_seconds is None:
                break
            lock.release()
            held = False
            if not _wait_out_close(wl.ws, lock, t0 + budget_seconds):
                break  # the budget ran out while close-outs held the lock
            held = True
            resumed += 1
        where, params = baseline_where(wl)
        remaining = wl.con.execute(f"SELECT COUNT(*) FROM files WHERE {where}", params).fetchone()[0]
        out = {"ok": True, **total, "remaining": remaining, "budget_hit": n["budget_hit"]}
        if n["yielded"]:
            out["paused_for_close"] = True
        if resumed:
            out["resumed_after_close"] = resumed
        return out
    finally:
        marker.end()
        if held:
            lock.release()


def prune(wl: "Worklog", now: float) -> None:
    """Delete diffs past the retention period (once per day)."""
    con = wl.con
    today = wl.cfg.today(now)
    if get_meta(con, "last_prune") == today:
        return
    cutoff = (dt.date.fromisoformat(today) - dt.timedelta(days=wl.cfg.diff_retention_days)).isoformat()
    for r in con.execute("SELECT diff_file FROM changes WHERE day < ? AND diff_file IS NOT NULL", (cutoff,)):
        try:
            (wl.ws.dir / r["diff_file"]).unlink()
        except FileNotFoundError:
            pass
    con.execute("UPDATE changes SET diff_file=NULL, diff_pruned=1 WHERE day < ? AND diff_file IS NOT NULL",
                (cutoff,))
    con.execute("DELETE FROM scan_events WHERE at < ?", (now - SCAN_EVENT_RETENTION_DAYS * 86400,))
    if wl.ws.diff_dir.exists():
        for sub in wl.ws.diff_dir.iterdir():
            if sub.is_dir() and not any(sub.iterdir()):
                sub.rmdir()
    set_meta(con, "last_prune", today)
    con.commit()
