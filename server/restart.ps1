# Stops whatever is listening on the backend port and starts a fresh copy.
#
# Usage:
#   .\restart.ps1
#   .\restart.ps1 -Port 7860 -LogPath ..\backend.log
#
# To watch just the latency lines while it runs, in a second terminal:
#   Get-Content ..\backend.log -Wait | Select-String 'latency'
#
# Implementation note: this went through two broken versions before landing
# here. Start-Process cannot redirect stdout and stderr to the SAME file
# (-RedirectStandardOutput/-RedirectStandardError both pointed at one path
# throws). Routing through cmd.exe's own `> file 2>&1` to work around that
# then broke on argument quoting - Start-Process re-quotes each
# comma-separated -ArgumentList element independently, which mangled the
# manually embedded quotes cmd.exe needed, and it failed silently under
# -WindowStyle Hidden with no error and no log file. What actually works,
# confirmed against a live run: two separate log files via
# -RedirectStandardOutput/-RedirectStandardError, no cmd.exe involved. Almost
# everything (loguru, uvicorn's own startup lines) writes to stderr, so
# LogPath below is genuinely the useful one; the stdout file exists but is
# usually near-empty.

param(
    [int]$Port = 7860,
    [string]$LogPath = "$PSScriptRoot\..\backend.log"
)

$ErrorActionPreference = "Stop"

$existing = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($existing) {
    foreach ($conn in $existing) {
        Write-Host "Stopping process $($conn.OwningProcess) on port $Port..."
        Stop-Process -Id $conn.OwningProcess -Force -ErrorAction SilentlyContinue
    }
    Start-Sleep -Seconds 2
}

$venvPython = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    throw "Virtualenv not found at $venvPython - run this from a checkout with server/.venv already set up."
}

$stdoutLog = [System.IO.Path]::ChangeExtension($LogPath, ".out.log")
Remove-Item $LogPath, $stdoutLog -Force -ErrorAction SilentlyContinue

Write-Host "Starting backend, logging to $LogPath ..."
Start-Process -FilePath $venvPython -ArgumentList "main.py" -WorkingDirectory $PSScriptRoot `
    -RedirectStandardOutput $stdoutLog -RedirectStandardError $LogPath -WindowStyle Hidden

Write-Host "Waiting for it to come up..."
$deadline = (Get-Date).AddSeconds(60)
do {
    Start-Sleep -Seconds 2
    try {
        $health = Invoke-RestMethod "http://localhost:$Port/api/health" -TimeoutSec 3
        Write-Host "Backend is up: $($health | ConvertTo-Json -Compress)"
        exit 0
    } catch {
        # not up yet
    }
} while ((Get-Date) -lt $deadline)

Write-Host "Backend did not respond within 60s - check $LogPath" -ForegroundColor Yellow
exit 1
