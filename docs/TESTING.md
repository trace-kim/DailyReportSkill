# Testing worklog on a new PC

Step-by-step check for a PC where the repo has been cloned and a venv created, with nothing
installed yet. Everything is tried on a small **test folder** first; the real work folder is only
set up in the last step. Every command below can be pasted as-is.

Assumptions (change them only in the "Every new window" block below):

- the repo is at `C:\tools\DailyReportSkill`, and the venv is `.venv` inside it
- the test folder is `D:\worklog-test` (it must be somewhere DRM applies)

## Every new window: run this first

Open PowerShell and paste:

```powershell
cd C:\tools\DailyReportSkill
$env:Path = "$PWD\.venv\Scripts;$env:Path"
$env:WORKLOG_ROOT = "D:\worklog-test"
(Get-Command python).Source
```

The last line must print a path ending in `\.venv\Scripts\python.exe`. If it does not, the `cd`
path is wrong; fix it and paste the block again.

What this does: the venv's `python` and `worklog` now run without a path in front of them, and
every `worklog` command uses the test folder without needing `--root`. Both settings last only
for this window, which is why the block is pasted again in every new window.

## 1. Install into the venv

```powershell
python --version
python -m pip install -e ".[dev]"
worklog --version
```

**Expected:** Python 3.10 or newer, and `worklog 0.1.0` at the end.

- `-e` (editable) means a later `git pull` takes effect in this venv without reinstalling.
- If pip cannot reach PyPI (company proxy), run
  `python -m pip install -e ".[dev]" --proxy http://<proxy>:<port>`, or ask IT for the internal
  package mirror.

## 2. Run the tests, including the Office tests

Save open Office work first: the tests start hidden Word, Excel and PowerPoint instances.

```powershell
$env:WORKLOG_TEST_COM = "1"; python -m pytest -q; Remove-Item Env:WORKLOG_TEST_COM
```

**Expected:** `48 passed`.

PowerPoint can only run once, so if it is already open the tests share it; they never close your
presentations. If tests fail, keep the output: a failure here tells us something about this PC
(for example, a DRM client encrypting new files in `%TEMP%`, or PowerPoint not being installed).

## 3. Check DRM readability (the most important step)

Create the test folder and open it in Explorer:

```powershell
New-Item -ItemType Directory -Force $env:WORKLOG_ROOT | Out-Null
explorer $env:WORKLOG_ROOT
```

Copy a few real work files into that Explorer window: some `.docx`, `.xlsx`, `.pptx` and `.pdf`,
plus old `.doc` / `.xls` / `.ppt` files if you have them. Subfolders are fine. Then:

```powershell
worklog doctor
```

`doctor` finds the files itself: it tests the 3 most recently modified files of each type in the
test folder, reading each one directly and through Office. It takes a minute or two. Read the
`verdicts` list at the end of the output:

| Verdict | Meaning |
|---|---|
| `readable directly` | Python can read this file as-is. |
| `readable through Office` | DRM blocks direct reading, but the hidden Office instance can read it. This works. |
| `NOT readable` | Neither method works. This file type needs the company agent's own DRM tool. |
| Write-test line | If it says files written by Python change after writing, the DRM client encrypts new files, and reports must be stored outside the DRM area. |

The `preview` fields let you check that the text was read correctly. Then save a copy without
document text, which is the one to share:

```powershell
worklog doctor --no-preview --out "$env:TEMP\doctor.json"
```

## 4. Try change tracking on the test folder

```powershell
worklog init --report none
worklog baseline
worklog status
```

**Expected:** `tracked_files` equals the number of files you copied, and `baseline_remaining` is `0`.

Now make some normal edits in the Explorer window from step 3:

1. Open a `.docx` in Word, add a sentence, save.
2. Open an `.xlsx` in Excel, change a number, save.
3. Rename one file.
4. Delete another file.
5. Create a new file (right-click > New > any type).
6. Open one more file and save it without changing anything.

Then record the day and print every change with its diff:

```powershell
worklog close
worklog changes --diffs
```

**Expected:**

- Each edit has a `# change` header with the right kind: `modified`, `renamed`, `deleted`, `added`.
- The diffs show your added sentence (`+...`) and the changed number (`-old` / `+new`).
- The file you only opened and saved shows as `resaved`, or does not appear at all.

## 5. Try the agent in opencode

```powershell
worklog setup-opencode
```

This adds a `worklog` MCP server for the test folder to
`%USERPROFILE%\.config\opencode\opencode.json` (a backup of the old file is kept next to it) and
installs the skill and the `/daily-report` and `/weekly-report` commands in the same folder.
If the company agent reads its opencode config from a different folder, run
`$env:OPENCODE_CONFIG_DIR = "<that folder>"` first, then `worklog setup-opencode` again.

Restart opencode (or the company agent) and type, one at a time:

1. `/daily-report 오늘`: the agent reads the changes and saves today's daily log.
2. `오늘 A사 미팅 다녀왔어`: the agent stores this as a note for today.
3. `/weekly-report`: the agent writes a weekly report from the daily logs.

Back in PowerShell, print what was saved:

```powershell
worklog render daily
worklog render weekly
```

**Expected:** a daily log with one section per topic, and a weekly report with one titled
paragraph per topic, every sentence ending in `~니다.` With one day of data the weekly report is
short; the format is what matters here.

Last, test the unattended run, which is what the morning scheduled task does. Edit one more file
in the test folder first, so there is something new to report, then:

```powershell
Push-Location $env:WORKLOG_ROOT; opencode run --command daily-report --auto auto; Pop-Location
worklog render daily
```

**Expected:** the run finishes on its own without asking anything, and today's log now includes
the last edit. If opencode rejects `--auto`, run `opencode --version` and keep the output.

## 6. Install for real

Close the PowerShell window and **open a new one**, so the test settings from the "Every new
window" block are gone. Do not paste that block this time. Then, with your real work folder:

```powershell
cd C:\tools\DailyReportSkill
powershell -ExecutionPolicy Bypass -File .\scripts\install.ps1 -Root "D:\업무"
Get-ScheduledTask -TaskName "worklog-*" | Select-Object TaskName, State
```

**Expected:** two tasks, `worklog-scan-...` and `worklog-morning-...`, both `Ready`.

The installer:

- creates its own environment in `%LOCALAPPDATA%\worklog\venv` (the repo's `.venv` stays for testing)
- initializes the real folder and points the opencode registration at it (replacing the test folder)
- adds an hourly scan and a morning run that writes the daily logs
- starts reading the existing files in the background

On a large DRM folder the background reading keeps hidden Word and Excel busy for a while. That
is expected. The first run has no time limit; if it is cut short (logoff, reboot), the morning task
continues it for up to 15 minutes per run until every file has been read once.

The next morning, the daily log for the previous workday should be in `.worklog\reports\daily\`
inside the work folder, and the agent's output in `.worklog\logs\report-*.log`.

Remove the test folder when you no longer need it:

```powershell
Remove-Item -Recurse -Force D:\worklog-test
```

## Updating after a `git pull`

```powershell
cd C:\tools\DailyReportSkill
git pull
& "$env:LOCALAPPDATA\worklog\venv\Scripts\python.exe" -m pip install --force-reinstall --no-deps .
& "$env:LOCALAPPDATA\worklog\venv\Scripts\python.exe" -m pip install .
```

The repo's `.venv` (editable) picks up changes by itself. The installed copy used by the scheduled
tasks does not: the first `pip` line replaces it (needed while the version number stays the same),
and the second installs any new dependencies. The `&` at the start of those lines is required.

## What to send back

1. `%TEMP%\doctor.json` (it has no document text)
2. The output of any failing tests from step 2
3. If step 4 looked wrong: the output of `worklog changes --diffs`, and
   `D:\worklog-test\.worklog\logs\worklog.log`
4. If any file type was `NOT readable`: the names of the company agent's tools that read DRM files
