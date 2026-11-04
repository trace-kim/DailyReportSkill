"""Configuration (``.worklog/config.toml``) and day-boundary arithmetic."""

from __future__ import annotations

import datetime as dt
import json
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - exercised on Python 3.10 only
    import tomli as tomllib

# Status key -> phrase used to build weekly topic titles: "{subject}을/를 {phrase}."
DEFAULT_STATUS_PHRASES: dict[str, str] = {
    "started": "착수하였습니다",
    "in_progress": "진행중입니다",
    "reviewing": "검토중입니다",
    "done": "완료하였습니다",
    "on_hold": "보류중입니다",
}

DEFAULT_CONFIG_TOML = """\
# worklog configuration. Restart the MCP server (or the agent) after editing.

[general]
# Work saved before this time counts toward the previous day (late-night work).
day_boundary = "04:00"

[storage]
# Encrypt cached document text and diffs with Windows DPAPI (readable only by
# the same Windows user on this PC).
protect_text = true
# Stored diffs older than this many days are deleted. Reports are kept.
diff_retention_days = 60

[extract]
# Files larger than this are tracked by name/size only.
max_file_mb = 100
max_text_chars = 2000000
# Use Word/Excel/PowerPoint through COM when direct parsing fails (DRM files).
com_enabled = true
com_timeout_sec = 90
# Let Word open PDFs that cannot be parsed directly (slow: Word converts them).
# DRM-protected PDFs are not sent to Word: its converter cannot read them.
# While worklog's hidden Word runs, Word's PDF-conversion prompt is suppressed
# (HKCU ...\\Word\\Options\\DisableConvertPdfWarning) and restored afterwards.
com_pdf_via_word = true
# Extra extensions to read as plain text, e.g. [".sas", ".vba"].
extra_text_extensions = []
# Extensions to track by name/size only, e.g. [".zip", ".dwg"].
skip_extensions = []

[reports]
# Where reports are written. Empty = <root>/.worklog/reports.
# A folder inside the work folder is excluded from change tracking automatically.
dir = ""
weekly_title = "주간 업무 보고"
author = ""

[status_phrases]
# Weekly topic titles are built as "{subject}을/를 {phrase}."
started = "착수하였습니다"
in_progress = "진행중입니다"
reviewing = "검토중입니다"
done = "완료하였습니다"
on_hold = "보류중입니다"

[automation]
# Command started by `worklog morning` after the close-out, run in the work folder.
# Example: ["opencode", "run", "--model", "provider/model", "--command", "daily-report", "--auto", "auto"]
# Name the model: without --model, opencode uses the model last picked in its TUI.
# A run that leaves days without a report makes `morning` fail and shows up in `status`.
report_command = []
report_timeout_min = 30
# Minutes of baseline text extraction done by each `worklog morning` run.
baseline_minutes_per_run = 15
"""

_HHMM = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*$")


def parse_hhmm(value: str) -> dt.time:
    m = _HHMM.match(str(value))
    if not m:
        raise ValueError(f"expected HH:MM, got {value!r}")
    hour, minute = int(m.group(1)), int(m.group(2))
    if not (0 <= hour < 24 and 0 <= minute < 60):
        raise ValueError(f"invalid time {value!r}")
    return dt.time(hour, minute)


def _ext_list(values) -> list[str]:
    out = []
    for v in values or []:
        v = str(v).strip().lower()
        if v:
            out.append(v if v.startswith(".") else "." + v)
    return out


@dataclass
class Config:
    day_boundary: dt.time = dt.time(4, 0)
    protect_text: bool = True
    diff_retention_days: int = 60
    max_file_mb: float = 100
    max_text_chars: int = 2_000_000
    com_enabled: bool = True
    com_timeout_sec: float = 90
    com_pdf_via_word: bool = True
    extra_text_extensions: list[str] = field(default_factory=list)
    skip_extensions: list[str] = field(default_factory=list)
    reports_dir: str = ""
    weekly_title: str = "주간 업무 보고"
    author: str = ""
    status_phrases: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_STATUS_PHRASES))
    report_command: list[str] = field(default_factory=list)
    report_timeout_min: float = 30
    baseline_minutes_per_run: float = 15

    @classmethod
    def load(cls, path: Path) -> "Config":
        cfg = cls()
        if not path.exists():
            return cfg
        with path.open("rb") as f:
            data = tomllib.load(f)
        g = data.get("general", {})
        s = data.get("storage", {})
        e = data.get("extract", {})
        r = data.get("reports", {})
        a = data.get("automation", {})
        if "day_boundary" in g:
            cfg.day_boundary = parse_hhmm(g["day_boundary"])
        cfg.protect_text = bool(s.get("protect_text", cfg.protect_text))
        cfg.diff_retention_days = int(s.get("diff_retention_days", cfg.diff_retention_days))
        cfg.max_file_mb = float(e.get("max_file_mb", cfg.max_file_mb))
        cfg.max_text_chars = int(e.get("max_text_chars", cfg.max_text_chars))
        cfg.com_enabled = bool(e.get("com_enabled", cfg.com_enabled))
        cfg.com_timeout_sec = float(e.get("com_timeout_sec", cfg.com_timeout_sec))
        cfg.com_pdf_via_word = bool(e.get("com_pdf_via_word", cfg.com_pdf_via_word))
        cfg.extra_text_extensions = _ext_list(e.get("extra_text_extensions"))
        cfg.skip_extensions = _ext_list(e.get("skip_extensions"))
        cfg.reports_dir = str(r.get("dir", "") or "")
        cfg.weekly_title = str(r.get("weekly_title", cfg.weekly_title))
        cfg.author = str(r.get("author", "") or "")
        phrases = data.get("status_phrases")
        if phrases:
            cleaned = {str(k).strip(): str(v).strip().rstrip(".") for k, v in phrases.items()}
            cfg.status_phrases = {k: v for k, v in cleaned.items() if k and v}
        cmd = a.get("report_command", [])
        cfg.report_command = [str(x) for x in cmd] if isinstance(cmd, list) else []
        cfg.report_timeout_min = float(a.get("report_timeout_min", cfg.report_timeout_min))
        cfg.baseline_minutes_per_run = float(a.get("baseline_minutes_per_run", cfg.baseline_minutes_per_run))
        return cfg

    # --- day arithmetic -------------------------------------------------
    def logical_day(self, ts: float) -> str:
        """Calendar day a timestamp belongs to, shifted by the day boundary."""
        shifted = dt.datetime.fromtimestamp(ts) - dt.timedelta(
            hours=self.day_boundary.hour, minutes=self.day_boundary.minute
        )
        return shifted.date().isoformat()

    def day_start(self, day: str) -> float:
        return dt.datetime.combine(dt.date.fromisoformat(day), self.day_boundary).timestamp()

    def day_end(self, day: str) -> float:
        nxt = dt.date.fromisoformat(day) + dt.timedelta(days=1)
        return dt.datetime.combine(nxt, self.day_boundary).timestamp()

    def today(self, now: float | None = None) -> str:
        return self.logical_day(time.time() if now is None else now)


def write_default_config(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(DEFAULT_CONFIG_TOML, encoding="utf-8")


def set_report_command(path: Path, command: list[str]) -> None:
    """Replace the ``report_command`` line in config.toml, keeping comments."""
    text = path.read_text(encoding="utf-8")
    # A JSON array of strings is also a valid TOML array of basic strings.
    line = "report_command = " + json.dumps([str(c) for c in command], ensure_ascii=False)
    new, n = re.subn(r"^report_command\s*=.*$", lambda _m: line, text, flags=re.M)
    if n == 0:
        if "[automation]" not in new:
            new = new.rstrip("\n") + "\n\n[automation]\n"
        new = new.replace("[automation]\n", "[automation]\n" + line + "\n", 1)
    path.write_text(new, encoding="utf-8")
