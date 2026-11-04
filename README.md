# worklog

Tracks what changes in a work folder every day and lets an AI agent (opencode, or a company
agent built on opencode) write **Korean daily work logs (일일 업무 일지)** and **weekly
reports for a senior manager (주간 업무 보고)** from those changes.

- No git required: worklog keeps its own change index.
- Works with DRM-protected Office files: when a file cannot be parsed directly, it is read
  through Word / Excel / PowerPoint (COM automation).
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
 ├─ commands /daily-report  /weekly-report
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
powershell -ExecutionPolicy Bypass -File scripts\install.ps1 -Root "D:\업무"
```

The installer:
- creates a virtual environment in `%LOCALAPPDATA%\worklog\venv` and installs worklog into it
- initializes the work folder (`.worklog\`, hidden)
- registers the MCP server, the skill and the two commands in `~/.config/opencode`
- registers two scheduled tasks that run only while the user is logged on (Office automation and
  DRM clients need the user's session)
- starts the baseline text extraction in the background

Options: `-Wheelhouse <dir>` for offline PCs (build it with `python -m pip wheel . -w wheelhouse`
on a PC with internet access and the same Python version), `-Report none` to skip the automatic agent
run, `-MorningTime 08:30`, `-NoSchedule`, `-NoOpencode`, `-Uninstall`.

**Then check DRM readability on the target PC:**

```powershell
& "$env:LOCALAPPDATA\worklog\venv\Scripts\worklog.exe" doctor --root "D:\업무" "D:\업무\샘플.docx" "D:\업무\샘플.xlsx" "D:\업무\샘플.pptx"
```

`doctor` reports, for each sample, whether it can be read directly or only through Office, how long
that takes, whether the session is interactive, and whether files that Python writes into the folder
stay readable (some DRM clients encrypt every new file). To test the scheduled-task context too,
run the same command from a one-off task with `--out C:\temp\doctor.json`.

### Other agents

The company agent wraps opencode, so the opencode registration covers it. For any other MCP client,
use the server command `<venv>\Scripts\python.exe -m worklog mcp --root "<folder>"`, and copy the
skill and command files with `worklog export-assets <dir>`.

## Using it

In opencode:
- `/daily-report`: write logs for every day that needs one (this also runs every morning).
  `/daily-report 오늘` writes today's log now; `/daily-report 2026-11-03` rewrites that day's log.
- `/weekly-report`: from the day after the last weekly report up to the latest daily log.
  `/weekly-report 2026-11-02 2026-11-13` uses an explicit period.
- Telling the agent about off-file work ("오늘 A사 미팅 다녀왔어") stores a note for that day's log.

CLI (`worklog <command> --root <folder>`): `init`, `scan`, `close`, `baseline`, `morning`, `status`,
`changes [DAY]`, `diff ID`, `read PATH`, `search TEXT`, `render daily|weekly KEY`, `doctor`,
`setup-opencode`, `export-assets`, `mcp`.

## Data and privacy

Everything lives in `<folder>\.worklog\`:

| Path | Content |
|---|---|
| `config.toml` | settings (day boundary, Office/COM, retention, report title, status phrases) |
| `index.db` | SQLite: tracked files, scans, change records |
| `text\`, `diffs\` | latest text of each document and daily diffs, **DPAPI-encrypted** (only this Windows user on this PC can decrypt them); diffs are deleted after `diff_retention_days` (60) |
| `topics.json` | work topics (safe to edit by hand) |
| `context\folder_map.md`, `notes\` | the agent's folder notes, the user's notes |
| `reports\daily\*.md/json`, `reports\weekly\*.txt/json` | reports (JSON is the source of truth) |
| `logs\` | tool logs and the output of scheduled agent runs |

worklog never modifies the user's files. Office files are opened read-only, with macros, link updates
and alerts disabled, and closed without saving. Word and Excel run as separate hidden instances.
PowerPoint is single-instance, so if the user already has it open it is shared and never closed or
killed. While worklog's own Word instance runs with PDF support enabled, Word's PDF-conversion prompt
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
