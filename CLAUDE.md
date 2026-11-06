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
   diff, newest first. Baseline failures are not retried. `baseline_where` skips files untouched for
   `baseline_max_age_days` before install. Baseline and close share the `close` lock: a close that
   has to wait touches `locks/close.wanted`, the baseline yields between files, and a budgeted
   baseline (background job, morning) waits for the close and resumes.

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
- PowerPoint is single-instance: it is shared with the user and never quit or killed if the user owns it.
  Even when worklog started it, the user's presentations can land in it, so its settings
  (`_SETTINGS`) are applied per job and restored (`run_job`), and it is not quit while presentations are open
- files are opened read-only with macros, alerts, MRU and link updates off, and a dummy password so prompts fail fast
- Word loads a PDF its converter cannot read (DRM-protected) as plain text, even with the PDF converter
  forced (`Format=27`). `Extractor.extract` therefore skips Word for PDFs without a `%PDF-` header on
  disk (except when `doctor` forces `method="com"`), and `looks_like_pdf_source` rejects such output
- Word's PDF-conversion prompt is suppressed by temporarily setting the HKCU value `DisableConvertPdfWarning`, restored in `shutdown()`

Always use `Extractor` as a context manager so Office instances are closed.

**Storage.** SQLite in WAL mode (`index.db`). Cached document text and diffs are DPAPI-encrypted
(`storage.TextVault`); diffs and scan events are pruned after a retention period. Schema changes
bump `db.SCHEMA_VERSION` and add a step to `db._migrate` (existing installs keep their index).

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
A new command file must also be listed in `opencode_setup.COMMANDS`.
`setup-opencode --workspace [DIR]` registers per workspace instead (`DIR/opencode.jsonc`,
`DIR/.opencode/skills|commands`): DS Cowork's bundled opencode reads only that. Config backups go to
`%LOCALAPPDATA%\worklog\backups`, never next to the config, because a workspace is usually the work folder.

**MCP server.** Supports both mcp 2.x (`MCPServer`) and 1.x (`FastMCP`). `mcp_server.py`
deliberately has no `from __future__ import annotations`, because the SDK evaluates annotations at
runtime. Tools return JSON strings with `ensure_ascii=False`; `diff` and `read_file` return plain
text. The tool docstrings are the agent-facing documentation.

**Agent mode: every MCP call returns in time.** Agent apps time out tool calls (opencode's default
is 60 s), and a folder can have tens of thousands of files, so the server sets `agent_mode` and
`lock_wait` on its `Worklog`:
- tools run in worker threads (`_tools` in `mcp_server.py`). A call that finds another one running
  gets `WorklogBusy` after `AGENT_LOCK_WAIT` and answers `{"busy": true}` instead of queueing
- `close` gets the budget `[agent] call_seconds` (30). Office waits are capped by an `Extractor`
  deadline (budget + `HARD_GRACE_SECONDS`), and Office failures are remembered across calls
  (`com_memory`)
- `ensure_scan` reuses a scan whose walk *ended* recently (`taken_at + duration`). A slow scan
  continues in a background thread (`_ScanJob`, own SQLite connection) while the call answers busy
- nothing is written to the folder before the `init` tool (`_Folder` opens lazily), and `status`
  never starts the first inventory
- the `baseline` and `doctor` tools start `python -m worklog ...` processes (`_spawn`). `JobMarker`
  makes a running job visible across processes. An agent app may keep its servers in a kill-on-close
  Job Object (the MCP Python SDK does), and venv/console-script launchers nest a job of their own
  inside it, so `CREATE_BREAKAWAY_FROM_JOB` can "succeed" while the child stays in the app's job.
  `survives_session` therefore asks `IsProcessInJob` afterwards. Under libuv (Node) hosts the child
  is in no job and survives
- `status` reports missing install.ps1 tasks, found through the Task Scheduler COM API (`find_scheduled_tasks`)

**Automation.** `scripts/install.ps1` creates `%LOCALAPPDATA%\worklog\venv` (a regular install, not
editable), runs `init` and `setup-opencode`, and registers two Task Scheduler tasks with the
Interactive logon type, because COM and DRM clients need the user's session. The tasks are an
hourly `scan` and a morning `morning`, which closes out, continues the baseline, and runs
`report_command` (`opencode run --model M --command daily-report --auto auto`) when days need reports.
opencode exits 0 even when the model never called a tool, so `morning` judges the run by
`days_needing_report` afterwards: leftover days make it fail, are stored in `meta.last_report_run`
and surface as a `status` hint. ANSI escapes are stripped from `logs/report-*.log`. Tasks
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
  assertions must account for that. File mtimes also come from that coarse clock while
  `installed_at` uses `time.time()`, so a write right after `init` can be stamped before it:
  tests that edit right after `make_wl()` should `backdate_tracking` first.
- COM tests disable pytest's faulthandler (`tests/test_com.py`): COM raises SEH exceptions that
  the RPC runtime handles, which faulthandler would print as "Windows fatal exception".
- Agent-mode tests (`tests/test_agent.py`) set `wl.agent_mode = True` and a short `lock_wait`. To act
  like a second process, use a second `Worklog` on the same root (its own connection) in a thread.
  `conftest` stubs `find_scheduled_tasks` so the real tasks on the PC do not leak into tests, and
  tests that start background jobs wait on `wl._procs[name][0]`. A scan is reused for 60 s, so tests
  that change files after a scan backdate `scans.taken_at` first.
