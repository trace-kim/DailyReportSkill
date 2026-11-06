# worklog

Tracks what changes in a work folder every day and lets an AI agent (opencode, or a company
agent built on opencode) write **Korean daily work logs (일일 업무 일지)** and **weekly
reports for a senior manager (주간 업무 보고)** from those changes.

- No git required: worklog keeps its own change index.
- Works with DRM-protected Office files: when a file cannot be parsed directly, it is read
  through Word / Excel / PowerPoint (COM automation). DRM-protected PDFs are the exception: Word
  cannot convert them, so they are tracked by name only.
- Changes made by the user and by agents are tracked the same way.
- The tool records facts (what changed, when, the diff, the document text) and checks report
  formats. The agent does the understanding and the writing.

## How it works

```
Task Scheduler ─ hourly + at logon ─▶ worklogw scan       size/mtime only, never opens files
               └ daily 09:00 + logon ─▶ worklogw morning   close-out, baseline, then the agent:
                                                           opencode run --command daily-report

opencode / company agent
 ├─ skill    worklog/SKILL.md          workflow + Korean writing rules
 ├─ commands /daily-report  /weekly-report  /worklog-init  /worklog-status  /worklog-doctor
 └─ MCP      python -m worklog mcp --root <folder>
```

1. **Scan** (hourly): records which files were added, modified or deleted. Cheap, reads no content.
2. **Close-out** (daily, or on demand by the agent): for each changed file it computes a hash, extracts
   the text (`python-docx` / `openpyxl` / `python-pptx` / `pypdf`, falling back to Office COM for DRM
   or legacy formats), diffs it against the previous text and stores a dated change record.
   A save that changes the bytes but not the text is marked `resaved`, so DRM re-encryption and
   "open and save" do not show up as work. A file moved to a new path is marked `renamed`.
3. **Daily log**: the agent reads the change list and the diffs, links them to earlier work
   (related files, earlier logs, topics) and saves one item per *topic* (stable IDs like `T003`).
4. **Weekly report** (every 1-2 weeks): the agent groups the daily items by topic and writes one
   titled paragraph per topic. The tool builds each title as `{subject}을/를 {status}.`
   (for example "A사 제안서 작성을 진행중입니다.") and rejects paragraphs that are not a single
   paragraph in 존댓말 보고체.

Example weekly output (`.worklog/reports/weekly/2026-11-02_2026-11-13.txt`):

```
주간 업무 보고 (2026.11.02 ~ 2026.11.13)

1. A사 제안서 작성을 진행중입니다.
10월 28일 착수한 A사 제안서 초안 작성을 이어서 진행하였습니다. 10월 30일 회의에서 제기된 요구사항을
반영하여 추진 일정(3장)을 신규로 작성하였고, 인건비 단가를 1,200만 원에서 1,350만 원으로 조정하였습니다. ...

2. 예산안을 검토중입니다.
...
```

## Install (Windows)

Requirements: Python 3.10+; Microsoft Office for DRM-protected files; opencode for automatic daily logs.

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install.ps1 -Root "D:\업무" -Model "provider/model"
```

`-Model` is the model of the unattended daily-report run (`opencode models` lists them). Without
it, `opencode run` uses the model last picked in the opencode TUI.

The installer:
- creates a virtual environment in `%LOCALAPPDATA%\worklog\venv` and installs worklog into it
- initializes the work folder (`.worklog\`, hidden)
- registers the MCP server, the skill and the commands in `~/.config/opencode` (with
  `-Workspace`: in the work folder, for DS Cowork; see below)
- registers two scheduled tasks that run only while the user is logged on (Office automation and
  DRM clients need the user's session)
- starts the baseline text extraction in the background

Options: `-Wheelhouse <dir>` for offline PCs (build it with `python -m pip wheel . -w wheelhouse`
on a PC with internet access and the same Python version), `-Report none` to skip the automatic agent
run, `-Workspace`, `-MorningTime 08:30`, `-NoSchedule`, `-NoOpencode`, `-Uninstall`.

`worklog morning` checks the agent's work instead of trusting its exit code (opencode exits 0 even
when the model called no tool). If days are still without a log afterwards, the run fails (task
result 1), `.worklog\logs\worklog.log` gets a warning with the end of the agent's output, and
`status` shows a hint until those days are written.

**Then check DRM readability on the target PC:**

```powershell
& "$env:LOCALAPPDATA\worklog\venv\Scripts\worklog.exe" doctor --root "D:\업무"
```

With no file arguments, `doctor` tests the 3 most recently modified files of each Office/PDF type
in the folder (`--per-type N` to change that). It also accepts files, folders and wildcards.

For a full step-by-step check on a new PC (tests, DRM, tracking, opencode, then the real install),
see [docs/TESTING.md](docs/TESTING.md).

`doctor` reports, for each sample, whether it can be read directly or only through Office, how long
that takes, whether the session is interactive, and whether files that Python writes into the folder
stay readable (some DRM clients encrypt every new file). To test the scheduled-task context too,
run the same command from a one-off task with `--out C:\temp\doctor.json`.

### DS Cowork

DS Cowork bundles its own opencode, which does not read `~/.config/opencode` and rewrites its own
config dir on every start. It reads the folder it opens as its workspace, so register worklog there:

```powershell
worklog setup-opencode --root "D:\업무" --workspace   # or install.ps1 -Workspace
```

This writes `opencode.jsonc` (`mcp` entry), `.opencode\skills\worklog\SKILL.md` and
`.opencode\commands\*.md` into the work folder (none of them is tracked as work). Reload the
workspace in DS Cowork afterwards. `--workspace <DIR>` registers a different workspace folder.
Run it again after updating worklog, so the workspace gets the current commands.

Tracking can then be started from DS Cowork itself: `/worklog-init` (or asking the agent to start
tracking) calls the `init` tool, checks Office/DRM readability with `doctor` and offers to start the
baseline. Nothing is written into the work folder before that. The scheduled tasks cannot be created
from the agent: `status` says when they are missing, together with the `install.ps1` command to run.

The bundled opencode is not on PATH and needs the app's login, so on a PC with only DS Cowork the
morning task cannot start it: install with `-Workspace -Report none` and schedule the daily report
in DS Cowork's own scheduler with a plain-text prompt (see [docs/TESTING.md](docs/TESTING.md),
step 6). The morning task still closes out the changes; days the in-app scheduler misses while the
app is closed are written by its next run.

### Other agents

For any other MCP client, use the server command
`<venv>\Scripts\python.exe -m worklog mcp --root "<folder>"`, and copy the skill and command files
with `worklog export-assets <dir>`.

## Using it

In opencode:
- `/daily-report`: write logs for every day that needs one (this also runs every morning).
  `/daily-report 오늘` writes today's log now; `/daily-report 2026-11-03` rewrites that day's log.
- `/weekly-report`: from the day after the last weekly report up to the latest daily log.
  `/weekly-report 2026-11-02 2026-11-13` uses an explicit period.
- Telling the agent about off-file work ("오늘 A사 미팅 다녀왔어") stores a note for that day's log.
- `/worklog-init`: start tracking this folder (DS Cowork, or any agent without the installer).
- `/worklog-status`: what is tracked, what is pending, which days need a log, background work.
- `/worklog-doctor`: can the agent read the Office/PDF (DRM) files here; `/worklog-doctor 다시` reruns it.

Agent tool calls are kept short, whatever the folder size: each returns within about 30 seconds
(`[agent] call_seconds`), and work that takes longer moves to the background. The first inventory
of a large folder and slow rescans continue after the call returns. `close` handles what fits and
reports `remaining`. The baseline runs as a separate process for up to `minutes`. A tool that is
still working on something else answers `busy` instead of queueing the call. The baseline steps aside
whenever a close-out needs the index. It skips files untouched for more than
`[automation] baseline_max_age_days` (180) before tracking started. Agent apps that end their
servers' child processes with the session (the MCP Python SDK does) also end a background baseline:
the `baseline` answer says so (`survives_session`), the progress is kept, and the next run continues.

CLI (`worklog <command> --root <folder>`): `init`, `scan`, `close`, `baseline`, `morning`, `status`,
`changes [DAY] [--diffs]`, `diff ID`, `read PATH`, `search TEXT`, `render daily|weekly [KEY]`, `doctor [PATHS]`,
`setup-opencode`, `export-assets`, `mcp`.

## Data and privacy

Everything lives in `<folder>\.worklog\`:

| Path | Content |
|---|---|
| `config.toml` | settings (day boundary, Office/COM, retention, agent call time, report title, status phrases) |
| `index.db` | SQLite: tracked files, scans, change records |
| `text\`, `diffs\` | latest text of each document and daily diffs, **DPAPI-encrypted** (only this Windows user on this PC can decrypt them); diffs are deleted after `diff_retention_days` (60) |
| `topics.json` | work topics (safe to edit by hand) |
| `context\folder_map.md`, `notes\` | the agent's folder notes, the user's notes |
| `reports\daily\*.md/json`, `reports\weekly\*.txt/json` | reports (JSON is the source of truth) |
| `logs\` | tool logs and the output of scheduled agent runs |

worklog never modifies the user's files. Office files are opened read-only, with macros, link updates
and alerts disabled, and closed without saving. Word and Excel run as separate hidden instances.
PowerPoint is single-instance, so if the user already has it open it is shared and never closed or
killed; its alert and macro settings are changed only while a file is read and restored right after.
While worklog's own Word instance runs with PDF support enabled, Word's PDF-conversion prompt
is suppressed through a per-user registry value; the previous value is restored afterwards.

Document content goes to whatever LLM provider the agent is configured with.

Exclude folders or files with `<folder>\.worklogignore` (gitignore syntax). Office lock files, temp
files, `.git`, `node_modules` and agent folders are excluded by default.

## Development

```powershell
python -m venv .venv; .venv\Scripts\pip install -e ".[dev]"
.venv\Scripts\python -m pytest              # unit + MCP end-to-end tests
$env:WORKLOG_TEST_COM = "1"; .venv\Scripts\python -m pytest tests\test_com.py   # needs Office
```

Layout: `src/worklog/` with `scanner.py` (hourly scan), `closeout.py` (close-out and baseline),
`extract/` (direct parsers and Office COM), `reports.py` and `korean.py` (validation and rendering),
`service.py` (API used by both `cli.py` and `mcp_server.py`), and `assets/` (skill and commands).

Adding a file type means adding one reader to `extract/__init__.py` (`DIRECT` or `COM_APPS`).
HWP is not supported yet.

Planned: image understanding with a multimodal model (images are already recorded with their
metadata, and descriptions will be cached by content hash in `.worklog\media\`), and delivery of
weekly reports by email or Confluence through the company agent's tools (renderers will produce
those formats from the stored JSON).
