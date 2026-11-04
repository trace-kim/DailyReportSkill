# Testing worklog on a new PC

Step-by-step check for a PC where the repo has been cloned and a venv created, with nothing
installed yet. Everything is tried on a small **test folder** first; the real work folder is only
set up in the last step.

Assumptions (adjust the paths if yours differ):

- the repo is at `C:\tools\DailyReportSkill`
- the venv is `.venv` inside the repo
- the test folder is `D:\worklog-test`, and the real work folder is `D:\업무`

Run everything in one PowerShell window. If you open a new window, re-run the `$py`, `$wl` and
`$t` lines first (PowerShell variables do not carry over between windows).

## 1. Install into the venv

```powershell
cd C:\tools\DailyReportSkill
$py = (Resolve-Path .\.venv\Scripts\python.exe).Path
& $py --version                       # must be 3.10 or newer
& $py -m pip install -e ".[dev]"
$wl = (Resolve-Path .\.venv\Scripts\worklog.exe).Path
```

- `-e` (editable) means a later `git pull` takes effect without reinstalling.
- `[dev]` adds pytest and reportlab for the tests.
- If pip cannot reach PyPI (company proxy), add `--proxy http://<proxy>:<port>` or ask IT for the
  internal package mirror.
- You do not need to activate the venv; the commands call its `python.exe` directly, so a blocked
  `Activate.ps1` does not matter.

## 2. Run the test suite, including the Office tests

```powershell
$env:WORKLOG_TEST_COM = "1"; & $py -m pytest -q; Remove-Item Env:WORKLOG_TEST_COM
```

**Expected:** `44 passed`.

- Save open Office work first. The tests start hidden Word, Excel and PowerPoint instances.
  PowerPoint can only run once, so if it is already open the tests share it; they never close
  your presentations.
- If tests fail, keep the output. A failure here tells us something about this PC (for example,
  a DRM client encrypting new files in `%TEMP%`, or PowerPoint not being installed).

## 3. Check DRM readability (the most important step)

```powershell
$t = "D:\worklog-test"                # pick a folder where DRM applies
New-Item -ItemType Directory -Force $t | Out-Null
```

Copy a few real work files into `$t`: at least one `.docx`, `.xlsx`, `.pptx` and `.pdf`, plus an
old `.doc` / `.xls` / `.ppt` if you have one. Then run `doctor` twice, replacing the file names:

```powershell
# 1) with text previews, to see for yourself that the content is read correctly
& $wl doctor --root $t "$t\<file1>.docx" "$t\<file2>.xlsx" "$t\<file3>.pptx" "$t\<file4>.pdf"

# 2) without previews, saved to a file you can share
& $wl doctor --root $t --no-preview --out "$env:TEMP\doctor.json" "$t\<file1>.docx" "$t\<file2>.xlsx" "$t\<file3>.pptx" "$t\<file4>.pdf"
```

Read the `verdicts` list at the end of the output:

| Verdict | Meaning |
|---|---|
| `readable directly` | Python can read this file type as-is. |
| `readable through Office` | DRM blocks direct reading, but the hidden Office instance can read it. This works. |
| `NOT readable` | Neither method works. This file type needs the company agent's own DRM tool. |
| Write-test line | If it says files written by Python change after writing, the DRM client encrypts new files, and the reports must be stored outside the DRM area (`[reports] dir` in `.worklog\config.toml`). |

Only share the `--no-preview` file (`%TEMP%\doctor.json`). The first run's previews contain
company document text.

## 4. Try change tracking on the test folder

```powershell
& $wl init --root $t --report none
& $wl baseline --root $t
& $wl status --root $t
```

**Expected:** `tracked_files` equals the number of files you copied, and `baseline_remaining` is `0`.

Now make some normal edits in the test folder:

1. Add a sentence to the `.docx` in Word and save.
2. Change a number in the `.xlsx` and save.
3. Rename one file.
4. Delete another file.
5. Create a new file (any type).
6. Open one file and save it without changing anything.

Then close out the day and look at what was recorded:

```powershell
& $wl close --root $t
& $wl changes --root $t               # note the "id" of each change
& $wl diff <id> --root $t             # repeat for a few ids
```

**Expected:**

- Each edit appears with the right `kind`: `modified`, `renamed`, `deleted`, `added`.
- The diffs show the sentence and the number you changed.
- The file you only opened and saved shows as `resaved`, or does not appear at all.

## 5. Try the agent in opencode

```powershell
& $wl setup-opencode --root $t
```

This adds a `worklog` MCP server to `%USERPROFILE%\.config\opencode\opencode.json` (a backup of the
old file is kept next to it) and installs the skill and the `/daily-report` and `/weekly-report`
commands in the same folder. If the company agent reads its opencode config from a different
folder, first run `$env:OPENCODE_CONFIG_DIR = "<that folder>"` in the same window.

Restart opencode (or the company agent), then:

1. Type `/daily-report 오늘`.
   The agent should call the `worklog_*` tools and save
   `D:\worklog-test\.worklog\reports\daily\<today>.md`. Print it with:
   ```powershell
   & $wl render daily <YYYY-MM-DD> --root $t
   ```
2. Tell it about work that is not in a file, for example `오늘 A사 미팅 다녀왔어`.
   It should store this as a note for today's log.
3. Type `/weekly-report`.
   It writes `D:\worklog-test\.worklog\reports\weekly\*.txt`. With one day of data the report is
   short, but you can check the format: one titled paragraph per topic.

Then test the unattended run. This is what the morning scheduled task does:

```powershell
cd $t; opencode run --command daily-report --auto auto; cd C:\tools\DailyReportSkill
```

If this opencode version rejects `--auto`, note the version (`opencode --version`) so the command
can be changed.

## 6. Install for real

When steps 1-5 look right:

```powershell
cd C:\tools\DailyReportSkill
powershell -ExecutionPolicy Bypass -File scripts\install.ps1 -Root "D:\업무"
Get-ScheduledTask -TaskName "worklog-*" | Select-Object TaskName, State
```

The installer:

- creates its own environment in `%LOCALAPPDATA%\worklog\venv` (the repo's `.venv` stays for testing)
- initializes `D:\업무` and points the opencode registration at it (replacing the test folder entry)
- adds two scheduled tasks: an hourly scan and a morning run that writes the daily logs
- starts reading the existing files in the background

On a large DRM folder, the background reading keeps hidden Word and Excel busy for a while. That
is expected. The first run has no time limit; if it is cut short (logoff, reboot), the morning task
continues it for up to 15 minutes per run (`baseline_minutes_per_run` in `.worklog\config.toml`)
until every file has been read once.

When you are done with the test folder:

```powershell
Remove-Item -Recurse -Force D:\worklog-test
```

## Updating after a `git pull`

- The repo's `.venv` (editable install) picks up changes immediately.
- The installed copy used by the scheduled tasks does not. Reinstall it with:
  ```powershell
  & "$env:LOCALAPPDATA\worklog\venv\Scripts\python.exe" -m pip install --no-deps --force-reinstall C:\tools\DailyReportSkill
  ```
  `--force-reinstall` is needed while the version number stays the same.

## What to send back

1. `%TEMP%\doctor.json` (the `--no-preview` one)
2. The output of any failing tests from step 2
3. If step 4 or 5 looked wrong: the `changes` output and `D:\worklog-test\.worklog\logs\worklog.log`
4. If any file type was `NOT readable`: the names of the company agent's tools that read DRM files
