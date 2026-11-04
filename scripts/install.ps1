<#
.SYNOPSIS
    Installs worklog for one work folder: Python venv, opencode registration and
    scheduled tasks (hourly scan + daily close-out/report).

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\install.ps1 -Root "D:\Work"

.EXAMPLE
    # Name the model of the unattended daily-report run (see: opencode models).
    powershell -ExecutionPolicy Bypass -File scripts\install.ps1 -Root "D:\Work" -Model "provider/model"

.EXAMPLE
    # DS Cowork only: register in the work folder as its workspace; its bundled opencode cannot be
    # started by the morning task, so the daily report is scheduled inside the app instead.
    powershell -ExecutionPolicy Bypass -File scripts\install.ps1 -Root "D:\Work" -Workspace -Report none

.EXAMPLE
    # Offline PC: build the wheelhouse elsewhere with
    #   python -m pip wheel . -w wheelhouse
    powershell -ExecutionPolicy Bypass -File scripts\install.ps1 -Root "D:\Work" -Wheelhouse .\wheelhouse

.EXAMPLE
    # Remove scheduled tasks and the opencode registration (tracking data is kept).
    powershell -ExecutionPolicy Bypass -File scripts\install.ps1 -Root "D:\Work" -Uninstall
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Root,
    [string]$InstallDir = (Join-Path $env:LOCALAPPDATA 'worklog'),
    [string]$Python = '',
    [string]$Package = '',
    [string]$Wheelhouse = '',
    [ValidateSet('auto', 'opencode', 'none')][string]$Report = 'auto',
    [string]$Model = '',
    [string]$MorningTime = '09:00',
    [switch]$Workspace,
    [switch]$NoOpencode,
    [switch]$NoSchedule,
    [switch]$NoBaseline,
    [switch]$Uninstall
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2

function Write-Step([string]$Message) { Write-Host "==> $Message" -ForegroundColor Cyan }

$Root = (Resolve-Path -LiteralPath $Root).Path.TrimEnd('\')
$venv = Join-Path $InstallDir 'venv'
$py = Join-Path $venv 'Scripts\python.exe'
$wl = Join-Path $venv 'Scripts\worklog.exe'
$wlw = Join-Path $venv 'Scripts\worklogw.exe'

# Task names are unique per work folder.
$sha1 = [System.Security.Cryptography.SHA1]::Create()
$bytes = $sha1.ComputeHash([System.Text.Encoding]::UTF8.GetBytes($Root.ToLowerInvariant()))
$suffix = ([System.BitConverter]::ToString($bytes)).Replace('-', '').Substring(0, 8).ToLowerInvariant()
$scanTask = "worklog-scan-$suffix"
$morningTask = "worklog-morning-$suffix"

if ($Uninstall) {
    foreach ($t in @($scanTask, $morningTask)) {
        if (Get-ScheduledTask -TaskName $t -ErrorAction SilentlyContinue) {
            Unregister-ScheduledTask -TaskName $t -Confirm:$false
            Write-Host "removed scheduled task $t"
        }
    }
    if (Test-Path -LiteralPath $wl) {
        & $wl setup-opencode --root $Root --remove | Out-Null
        & $wl setup-opencode --root $Root --remove --workspace | Out-Null
        Write-Host 'removed the opencode registrations (global and workspace: MCP entry, skill, commands)'
    }
    Write-Host "Tracking data in $Root\.worklog was kept. Delete it manually if it is no longer needed."
    return
}

# ---------------------------------------------------------------- Python
Write-Step 'Locating Python 3.10+'
$pyCmd = @()
if ($Python) { $pyCmd = @($Python) }
elseif (Get-Command py -ErrorAction SilentlyContinue) { $pyCmd = @('py', '-3') }
elseif (Get-Command python -ErrorAction SilentlyContinue) { $pyCmd = @('python') }
else { throw 'Python 3.10+ was not found. Install it or pass -Python C:\path\to\python.exe' }
$pyExe = $pyCmd[0]
$pyArgs = @()
if ($pyCmd.Count -gt 1) { $pyArgs = $pyCmd[1..($pyCmd.Count - 1)] }
$ver = (& $pyExe @pyArgs -c "import sys; print('%d.%d' % sys.version_info[:2])" | Select-Object -First 1)
if (-not $ver -or ([version]$ver -lt [version]'3.10')) { throw "Python 3.10+ is required (found: '$ver')" }
Write-Host "using Python $ver ($($pyCmd -join ' '))"

if (-not (Test-Path -LiteralPath $py)) {
    Write-Step "Creating the virtual environment in $venv"
    New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
    & $pyExe @pyArgs -m venv $venv
    if ($LASTEXITCODE -ne 0) { throw 'creating the virtual environment failed' }
}

# ---------------------------------------------------------------- package
if (-not $Package) {
    if ($Wheelhouse) { $Package = 'worklog' } else { $Package = Split-Path -Parent $PSScriptRoot }
}
Write-Step "Installing worklog ($Package)"
$pipArgs = @('-m', 'pip', 'install', '--upgrade', '--disable-pip-version-check')
if ($Wheelhouse) { $pipArgs += @('--no-index', '--find-links', (Resolve-Path -LiteralPath $Wheelhouse).Path) }
& $py @pipArgs $Package
if ($LASTEXITCODE -ne 0) { throw 'pip install failed' }

# ---------------------------------------------------------------- init
Write-Step "Initializing tracking for $Root"
$initArgs = @('init', '--root', $Root, '--report', $Report)
if ($Model) { $initArgs += @('--model', $Model) }
& $wl @initArgs
if ($LASTEXITCODE -ne 0) { throw 'worklog init failed' }

if (-not $NoOpencode) {
    $setupArgs = @('setup-opencode', '--root', $Root, '--python', $py)
    if ($Workspace) {
        Write-Step 'Registering the MCP server, skill and commands in the work folder (workspace)'
        $setupArgs += '--workspace'
    }
    else {
        Write-Step 'Registering the MCP server, skill and commands with opencode'
    }
    & $wl @setupArgs
    if ($LASTEXITCODE -ne 0) { throw 'setup-opencode failed' }
}

# ---------------------------------------------------------------- tasks
if (-not $NoSchedule) {
    Write-Step 'Registering scheduled tasks (they run only while you are logged on)'
    $user = "$env:USERDOMAIN\$env:USERNAME"
    # Interactive logon: Office automation and DRM clients need the user's desktop session.
    $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited

    function Register-WorklogTask([string]$Name, [string]$Arguments, $Triggers, [string]$Delay,
                                  [TimeSpan]$Limit, [string]$Description) {
        $action = New-ScheduledTaskAction -Execute $wlw -Argument $Arguments -WorkingDirectory $Root
        $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries `
            -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -ExecutionTimeLimit $Limit
        try {
            $logon = New-ScheduledTaskTrigger -AtLogOn -User $user
            $logon.Delay = $Delay
            Register-ScheduledTask -TaskName $Name -Action $action -Trigger (@($Triggers) + $logon) `
                -Principal $principal -Settings $settings -Description $Description -Force | Out-Null
        }
        catch {
            Write-Warning "a logon trigger needs more rights here; registering $Name without it"
            Register-ScheduledTask -TaskName $Name -Action $action -Trigger @($Triggers) `
                -Principal $principal -Settings $settings -Description $Description -Force | Out-Null
        }
        Write-Host "registered scheduled task $Name"
    }

    $hourly = New-ScheduledTaskTrigger -Once -At ((Get-Date).AddMinutes(5)) -RepetitionInterval (New-TimeSpan -Hours 1)
    Register-WorklogTask $scanTask "scan --root `"$Root`"" @($hourly) 'PT2M' (New-TimeSpan -Minutes 30) `
        "worklog: hourly change scan of $Root"

    $daily = New-ScheduledTaskTrigger -Daily -At $MorningTime
    Register-WorklogTask $morningTask "morning --root `"$Root`"" @($daily) 'PT10M' (New-TimeSpan -Hours 3) `
        "worklog: daily close-out and daily-report run for $Root"
}

# ---------------------------------------------------------------- baseline
if (-not $NoBaseline) {
    Write-Step 'Starting the baseline text extraction in the background (resumable; continues every morning)'
    Start-Process -FilePath $wlw -ArgumentList @('baseline', '--root', "`"$Root`"") -WindowStyle Hidden
}

Write-Host ''
Write-Host 'Done. Next steps:' -ForegroundColor Green
Write-Host "  1. Check DRM readability:  & '$wl' doctor --root '$Root'"
Write-Host "  2. Status:                 & '$wl' status --root '$Root'"
if ($Workspace) {
    Write-Host "  3. In DS Cowork:           open $Root as the workspace and reload it, then"
    Write-Host '                             /daily-report   and every 1-2 weeks  /weekly-report'
    Write-Host '                             (schedule the daily report in the app: see docs\TESTING.md)'
}
else {
    Write-Host '  3. In opencode:            /daily-report   and every 1-2 weeks  /weekly-report'
}
Write-Host "  Logs: $Root\.worklog\logs"
