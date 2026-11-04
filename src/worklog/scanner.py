"""Hourly metadata scan: size + mtime only. Never opens file contents."""

from __future__ import annotations

import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import Iterator

from .db import get_meta, set_meta
from .ignore import IgnoreRules

log = logging.getLogger(__name__)

MTIME_EPS = 0.001


def walk(root: Path, rules: IgnoreRules, failed_dirs: list[str] | None = None) -> Iterator[tuple[str, int, float]]:
    """Yield (relative posix path, size, mtime) for every tracked file.

    Folders that cannot be listed are appended to ``failed_dirs`` (relative
    paths) so the caller does not mistake their files for deletions.
    """
    root_str = str(root)
    cut = len(root_str) + (0 if root_str.endswith(("\\", "/")) else 1)
    stack = [root_str]
    while stack:
        current = stack.pop()
        try:
            it = os.scandir(current)
        except OSError as e:
            if current == root_str:
                raise
            log.warning("cannot list %s: %s", current, e)
            if failed_dirs is not None:
                failed_dirs.append(current[cut:].replace("\\", "/"))
            continue
        with it:
            for entry in it:
                try:
                    rel = entry.path[cut:].replace("\\", "/")
                    if entry.is_symlink() or (hasattr(entry, "is_junction") and entry.is_junction()):
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        if not rules.ignored(rel, True):
                            stack.append(entry.path)
                    elif entry.is_file(follow_symlinks=False):
                        if rules.ignored(rel, False):
                            continue
                        st = entry.stat(follow_symlinks=False)
                        yield rel, st.st_size, st.st_mtime
                except OSError as e:
                    log.debug("skip %s: %s", entry.path, e)


def scan(con: sqlite3.Connection, root: Path, rules: IgnoreRules, now: float | None = None) -> dict:
    """Compare the folder with the index and record added/modified/deleted events.

    The very first scan only records the baseline inventory (origin='init')
    and no events.
    """
    now = time.time() if now is None else now
    if not root.is_dir():
        raise FileNotFoundError(f"work folder not found: {root}")
    initial = get_meta(con, "installed_at") is None

    existing: dict[str, tuple[int, int, float, int]] = {
        r["path"]: (r["id"], r["size"], r["mtime"], r["present"])
        for r in con.execute("SELECT id, path, size, mtime, present FROM files")
    }
    seen: set[str] = set()
    inserts: list[tuple] = []
    updates: list[tuple] = []
    events: list[tuple[str, str, int, float]] = []
    n_files = 0
    failed_dirs: list[str] = []

    for rel, size, mtime in walk(root, rules, failed_dirs):
        n_files += 1
        seen.add(rel)
        cur = existing.get(rel)
        if cur is None:
            inserts.append((rel, size, mtime, "init" if initial else "new", now, now))
            if not initial:
                events.append((rel, "added", size, mtime))
            continue
        fid, osize, omtime, present = cur
        if not present:
            updates.append((size, mtime, now, fid))
            events.append((rel, "added", size, mtime))
        elif osize != size or abs(omtime - mtime) > MTIME_EPS:
            updates.append((size, mtime, now, fid))
            events.append((rel, "modified", size, mtime))

    unreadable = tuple(d.rstrip("/") + "/" for d in failed_dirs)
    deletions = [
        (fid, rel, size, mtime)
        for rel, (fid, size, mtime, present) in existing.items()
        if present and rel not in seen and not (unreadable and rel.startswith(unreadable))
    ]
    for _fid, rel, size, mtime in deletions:
        events.append((rel, "deleted", size, mtime))

    with con:
        con.executemany(
            "INSERT INTO files(path, size, mtime, present, origin, first_seen, last_seen) "
            "VALUES(?, ?, ?, 1, ?, ?, ?)",
            inserts,
        )
        con.executemany(
            "UPDATE files SET size=?, mtime=?, present=1, last_seen=? WHERE id=?", updates
        )
        con.executemany("UPDATE files SET present=0 WHERE id=?", [(d[0],) for d in deletions])
        cur = con.execute(
            "INSERT INTO scans(taken_at, n_files, n_events) VALUES(?, ?, ?)",
            (now, n_files, len(events)),
        )
        scan_id = cur.lastrowid
        con.executemany(
            "INSERT INTO scan_events(scan_id, path, kind, size, mtime, at) VALUES(?, ?, ?, ?, ?, ?)",
            [(scan_id, p, k, s, m, now) for p, k, s, m in events],
        )
        if initial:
            set_meta(con, "installed_at", repr(now))

    counts = {"added": 0, "modified": 0, "deleted": 0}
    for _p, k, _s, _m in events:
        counts[k] += 1
    out = {"scan_id": scan_id, "initial": initial, "files": n_files, **counts}
    if failed_dirs:
        out["unreadable_folders"] = failed_dirs[:20]
    return out
