# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`worklog` tracks daily file changes in a work folder (without git) and gives an AI agent (opencode,
or a company agent that wraps opencode) the facts it needs to write **Korean daily work logs** and
**weekly reports for a senior manager**. The tool records and validates; the agent understands and
writes. It ships as one Python package with a CLI, an MCP server, an opencode skill and two slash
commands, plus a Windows installer.

Target environment (company PCs): Windows, Python usually present, git possibly absent, Office
installed, and most files DRM-protected so they can only be read through Office COM. HWP is out of
scope. Reports are plain files for now; email/Confluence delivery and multimodal image analysis are
planned.

## Commands

```powershell
python -m venv .venv; .venv\Scripts\python -m pip install -e ".[dev]"   # tests import the installed package
.venv\Scripts\python -m pytest -q                                      # COM tests are skipped
$env:WORKLOG_TEST_COM = "1"; .venv\Scripts\python -m pytest -q          # include Office COM tests (needs Office)
.venv\Scripts\python -m pytest -q tests\test_tracking.py::test_rename_and_delete
.venv\Scripts\python -m pytest -q -k "budget or unreadable"
```

There is no linter or formatter configured.

Manual run against a scratch folder (`--root` defaults to `$env:WORKLOG_ROOT`, then the cwd):

```powershell
worklog init --root <folder> --report none; worklog baseline --root <folder>
# edit files, then
worklog close --root <folder>; worklog changes --root <folder> --diffs
worklog doctor --root <folder>          # samples Office/PDF files itself; checks direct vs COM reading
python -m worklog mcp --root <folder>   # MCP server on stdio
```

When the number of tests changes, update the expected count in `docs/TESTING.md` (step 2).

## Architecture

**`service.Worklog` is the single API.** `cli.py` and `mcp_server.py` are thin wrappers over it;
add features there and expose them in both. All state lives in `<root>/.worklog/` (hidden), laid
out by `storage.Workspace`.

**Two-phase tracking.** This is the core idea and spans `scanner.py`, `closeout.py` and `db.py`:

1. `scanner.scan` (hourly, scheduled): size/mtime only, never opens files. It updates `files` and
   appends `scan_events`. The first scan records the inventory as `origin='init'` with no events.
   Folders that cannot be listed are excluded from deletion detection.
2. `closeout.run_close` (daily, or on demand by the agent): `CANDIDATES_SQL` picks files whose
   size/mtime differ from the last extraction, deletions, new files, and failed extractions
   (retried up to `MAX_RETRIES`). Each file is hashed, its text extracted and diffed against the
   cached previous text, and a `changes` row is committed **per file** (atomic, resumable). It runs
   under a time budget (oldest first, at least one file per call) and returns `remaining`; the
   agent calls `close` until it is 0.
3. `closeout.run_baseline` extracts text for `init` files once, so their first change has a real
   diff. Baseline failures are not retried.

Change kinds: `added`, `modified`, `deleted`, `renamed` (hash match, or size+mtime for files never
extracted), `resaved` (bytes changed, text did not, e.g. DRM re-encryption). Flags: `no_baseline`,
`touched_only`. A "day" is a logical day with a configurable boundary (default 04:00, see
`Config.logical_day`). Event time is the file mtime if plausible, otherwise the first scan event
(copied files keep old mtimes). A retry that succeeds fills in the earlier change record and sets
`revised_at`, which marks that day's report as stale.

**Extraction chain** (`extract/__init__.py`): text files, then direct parsers (`DIRECT`:
python-docx/openpyxl/python-pptx/pypdf), then Office COM (`COM_APPS`). Adding a format means adding
one reader there. `extract/com.py` is delicate:
- each Office app runs in its own worker thread with timeouts and a circuit breaker
- Word and Excel use `DispatchEx` (private hidden instances)
- PowerPoint is single-instance: it is shared with the user and never quit or killed if the user owns it
- files are opened read-only with macros, alerts, MRU and link updates off, and a dummy password so prompts fail fast
- Word's PDF-conversion prompt is suppressed by temporarily setting the HKCU value `DisableConvertPdfWarning`, restored in `shutdown()`

Always use `Extractor` as a context manager so Office instances are closed.

**Storage.** SQLite in WAL mode (`index.db`). Cached document text and diffs are DPAPI-encrypted
(`storage.TextVault`); diffs and scan events are pruned after a retention period.

**Reports.** JSON is the source of truth (`reports/daily/<date>.json`, `reports/weekly/<from>_<to>.json`).
The `.md` / `.txt` files are renders. Topics live in `topics.json` with stable IDs (`T001`...).
Daily reports store `covered_change_ids`, which is how `status` detects missing or stale days.
`korean.py` enforces the weekly format, and the agent cannot bypass it:
- one paragraph per topic, no bullets or markdown, every sentence ending in `니다.`
- the title is built by `build_title` as `{subject}을/를 {status phrase}.`, with 을/를 chosen from the final consonant
- titles that repeat the status in the subject are rejected

**Agent side.** The workflow and Korean writing rules for agents live in
`src/worklog/assets/skill/worklog/SKILL.md` and `assets/commands/*.md`. They are packaged, and
`setup-opencode` installs them into the opencode config dir (`~/.config/opencode` or
`$OPENCODE_CONFIG_DIR`) along with the `mcp.worklog` entry. Changing how the agent writes reports
usually means editing those markdown files, not Python. opencode exposes the tools as `worklog_<name>`.

**MCP server.** Supports both mcp 2.x (`MCPServer`) and 1.x (`FastMCP`). `mcp_server.py`
deliberately has no `from __future__ import annotations`, because the SDK evaluates annotations at
runtime. Tools return JSON strings with `ensure_ascii=False`; `diff` and `read_file` return plain
text. The tool docstrings are the agent-facing documentation.

**Automation.** `scripts/install.ps1` creates `%LOCALAPPDATA%\worklog\venv` (a regular install, not
editable), runs `init` and `setup-opencode`, and registers two Task Scheduler tasks with the
Interactive logon type, because COM and DRM clients need the user's session. The tasks are an
hourly `scan` and a morning `morning`, which closes out, continues the baseline, and runs
`report_command` (`opencode run --command daily-report --auto auto`) when days need reports. Tasks
use `worklogw` (gui-script, no console window). `FileLock` (msvcrt) keeps scan/close/morning runs
from overlapping.

## Conventions and pitfalls

- Never modify, move or delete the user's files. Office files are opened read-only and closed
  without saving. The skill tells the agent never to send reports anywhere unless the user asks.
- `scripts/install.ps1` must stay ASCII-only: Windows PowerShell 5.1 reads BOM-less scripts in the
  ANSI codepage.
- PowerShell 5.1 breaks native-command arguments that contain double quotes. For git commit
  messages use `git commit -F <file>`; for JSON CLI arguments prefer the `--report` choices over
  `--report-command`.
- Test fixtures (`tests/conftest.py`) use a root folder named `업무 폴더` (Korean with a space) on
  purpose, and disable COM unless `make_wl(com=True)`. Use `wl.cfg.today()`, not the calendar date,
  in date-dependent tests (the day boundary is 04:00).
- `time.monotonic()` on Windows has about 15.6 ms resolution; zero-length budgets and timing
  assertions must account for that.
