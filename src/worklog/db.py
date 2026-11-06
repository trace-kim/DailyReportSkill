"""SQLite index: files, hourly scans, close-outs and change records."""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_VERSION = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(
  key TEXT PRIMARY KEY,
  value TEXT
);

-- One row per tracked file. size/mtime/present come from the latest scan;
-- ext_* describe the state captured by the latest text extraction.
CREATE TABLE IF NOT EXISTS files(
  id INTEGER PRIMARY KEY,
  path TEXT NOT NULL UNIQUE,
  size INTEGER NOT NULL,
  mtime REAL NOT NULL,
  present INTEGER NOT NULL DEFAULT 1,
  origin TEXT NOT NULL,              -- 'init' (existed at install) | 'new'
  first_seen REAL NOT NULL,
  last_seen REAL NOT NULL,
  ext_size INTEGER,
  ext_mtime REAL,
  ext_hash TEXT,                     -- sha256 of raw bytes at extraction
  text_hash TEXT,                    -- sha256 of extracted text
  ext_kind TEXT,                     -- text | image | binary | failed
  ext_method TEXT,                   -- direct | com | metadata
  ext_error TEXT,
  ext_at REAL,
  ext_attempts INTEGER NOT NULL DEFAULT 0,
  has_text INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS scans(
  id INTEGER PRIMARY KEY,
  taken_at REAL NOT NULL,            -- when the walk started
  n_files INTEGER,
  n_events INTEGER,
  duration REAL                      -- seconds the walk took (schema 2)
);

CREATE TABLE IF NOT EXISTS scan_events(
  scan_id INTEGER NOT NULL,
  path TEXT NOT NULL,
  kind TEXT NOT NULL,                -- added | modified | deleted
  size INTEGER,
  mtime REAL,
  at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_scan_events_path ON scan_events(path);
CREATE INDEX IF NOT EXISTS ix_scan_events_at ON scan_events(at);
CREATE INDEX IF NOT EXISTS ix_scan_events_scan ON scan_events(scan_id);

CREATE TABLE IF NOT EXISTS closes(
  id INTEGER PRIMARY KEY,
  started_at REAL NOT NULL,
  closed_at REAL NOT NULL,
  n_changes INTEGER NOT NULL,
  remaining INTEGER NOT NULL,
  budget_hit INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS changes(
  id INTEGER PRIMARY KEY,
  close_id INTEGER NOT NULL,
  day TEXT NOT NULL,                 -- logical day (respects day_boundary)
  event_at REAL NOT NULL,
  path TEXT NOT NULL,
  old_path TEXT,
  kind TEXT NOT NULL,                -- added | modified | deleted | renamed | resaved
  size INTEGER,
  raw_hash TEXT,
  text_hash TEXT,
  lines_added INTEGER NOT NULL DEFAULT 0,
  lines_removed INTEGER NOT NULL DEFAULT 0,
  ext_kind TEXT,
  ext_method TEXT,
  ext_error TEXT,
  no_baseline INTEGER NOT NULL DEFAULT 0,
  diff_file TEXT,
  diff_pruned INTEGER NOT NULL DEFAULT 0,
  revised_at REAL                    -- set when a failed extraction was filled in later
);
CREATE INDEX IF NOT EXISTS ix_changes_day ON changes(day);
CREATE INDEX IF NOT EXISTS ix_changes_path ON changes(path);
"""


def connect(path: Path, setup: bool = True) -> sqlite3.Connection:
    """Open the index. ``setup=False`` skips schema creation and migration (for a second
    connection used by a background thread, after the main one has set things up)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path), timeout=30, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    if setup:
        con.executescript(SCHEMA)
        _migrate(con)
        con.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        con.commit()
    return con


def _migrate(con: sqlite3.Connection) -> None:
    """Bring indexes created by older versions up to the current schema."""
    cols = {r[1] for r in con.execute("PRAGMA table_info(scans)")}
    if "duration" not in cols:
        con.execute("ALTER TABLE scans ADD COLUMN duration REAL")


def get_meta(con: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def set_meta(con: sqlite3.Connection, key: str, value: str) -> None:
    con.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )
