<#
.SYNOPSIS
    Scheduled-task wrapper for the Wrike off-day approver notifier.

.DESCRIPTION
    Windows has no cron, so this is the equivalent entry point for Task
    Scheduler. It mirrors run-notifier.sh: resolve the project root, use the
    project venv, take a single-instance lock, append to a log file.

    Register it, hourly on weekdays from 07:05 to 17:05. Note /D is not valid
    with /SC HOURLY, so this uses WEEKLY with a 60 minute repeat over a 10 hour
    window instead:

        schtasks /Create /TN "VFZ Workschedule Emails" /SC WEEKLY ^
          /D MON,TUE,WED,THU,FRI /ST 07:05 /RI 60 /DU 0010:00 ^
          /TR "powershell -NoProfile -ExecutionPolicy Bypass -File \"<project path>\scripts\run-notifier.ps1\""

    Quote the -File path: the project path contains a space.

    The 07:00-18:00 window in .env still applies as a backstop, so a task that
    fires outside it posts nothing.

#>
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]] $ExtraArgs
)

$ErrorActionPreference = "Stop"

# The virtualenv lives at the REPO root and is shared by all three
# integrations, so it is one level ABOVE this integration. Looking for it
# beside this script found nothing and fell back to whatever python was on
# PATH -- which has none of the dependencies.
$integrationDir = Split-Path -Parent $PSScriptRoot
$repoRoot = Split-Path -Parent $integrationDir
Set-Location $integrationDir

$python = Join-Path $repoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) { $python = Join-Path $repoRoot ".venvin\python" }
if (-not (Test-Path $python)) { $python = Join-Path $integrationDir ".venv\Scripts\python.exe" }
if (-not (Test-Path $python)) {
    $python = (Get-Command python -ErrorAction SilentlyContinue).Source
}
if (-not $python) {
    Write-Error "No python interpreter found"
    exit 127
}

$logDir = if ($env:NOTIFIER_LOG_DIR) { $env:NOTIFIER_LOG_DIR } else { Join-Path $integrationDir "logs" }
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }
$logFile = Join-Path $logDir "notifier.log"

# Single instance. Two overlapping runs could both read an empty notification
# history and post the same comment twice.
$mutex = New-Object System.Threading.Mutex($false, "Global\WrikeOffdayNotifier")
if (-not $mutex.WaitOne(0)) {
    Add-Content -Path $logFile -Encoding utf8 `
        -Value "$(Get-Date -Format o) SKIP: previous run still in progress"
    exit 0
}

# The child process is launched with its streams redirected to files rather
# than piped, which looks roundabout but is required. Windows PowerShell 5.1
# turns a native command's stderr into ErrorRecord objects when it is merged
# into the pipeline with `2>&1`, and with $ErrorActionPreference = "Stop" that
# raises a terminating NativeCommandError. Python's logging writes to stderr,
# so `& $python ... 2>&1 | Add-Content` aborted on the very first log line:
# the log kept nothing useful, the exit status was never captured, and every
# run looked like a failure. `*>>` and `2>>` fail the same way or worse.
# Do not "simplify" this back to a pipeline.
$stdoutFile = Join-Path $logDir "notifier.out.$PID.tmp"
$stderrFile = Join-Path $logDir "notifier.err.$PID.tmp"

try {
    Add-Content -Path $logFile -Encoding utf8 -Value "----- $(Get-Date -Format o) starting -----"

    # Start-Process joins -ArgumentList with spaces and does not quote, so any
    # argument containing whitespace has to be quoted here.
    $arguments = @("run_notifier.py", "--watch") + $ExtraArgs
    $quoted = $arguments | ForEach-Object {
        if ($_ -match '[\s"]') { '"' + $_ + '"' } else { $_ }
    }

    $process = Start-Process -FilePath $python -ArgumentList $quoted `
        -NoNewWindow -Wait -PassThru `
        -RedirectStandardOutput $stdoutFile -RedirectStandardError $stderrFile
    $status = $process.ExitCode

    # stderr first: it carries the progress log, and stdout carries the closing
    # summary, so this is the order a reader expects.
    foreach ($stream in @($stderrFile, $stdoutFile)) {
        if (Test-Path $stream) {
            Get-Content $stream | Add-Content -Path $logFile -Encoding utf8
        }
    }

    Add-Content -Path $logFile -Encoding utf8 `
        -Value "----- $(Get-Date -Format o) finished, exit $status -----"
    exit $status
}
finally {
    foreach ($stream in @($stdoutFile, $stderrFile)) {
        if (Test-Path $stream) { Remove-Item $stream -Force }
    }
    $mutex.ReleaseMutex()
    $mutex.Dispose()
}
