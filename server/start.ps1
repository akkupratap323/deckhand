# Starts the backend without stopping anything first - fails clearly if the
# port is already taken rather than silently doing nothing useful.
#
# Usage:
#   .\start.ps1
#   .\start.ps1 -Port 7860 -LogPath ..\backend.log
#
# See restart.ps1's header comment for why this is
# -RedirectStandardOutput/-RedirectStandardError to two separate files rather
# than cmd.exe redirection - that path was tried and failed silently.

param(
    [int]$Port = 7860,
    [string]$LogPath = "$PSScriptRoot\..\backend.log"
)

$ErrorActionPreference = "Stop"

$existing = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "Port $Port is already in use (PID $($existing.OwningProcess -join ', ')). Run .\stop.ps1 first, or use .\restart.ps1." -ForegroundColor Yellow
    exit 1
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
