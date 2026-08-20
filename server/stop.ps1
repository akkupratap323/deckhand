# Stops whatever is listening on the backend port.
#
# Usage:
#   .\stop.ps1
#   .\stop.ps1 -Port 7860

param(
    [int]$Port = 7860
)

$existing = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if (-not $existing) {
    Write-Host "Nothing listening on port $Port."
    exit 0
}

foreach ($conn in $existing) {
    Write-Host "Stopping process $($conn.OwningProcess) on port $Port..."
    Stop-Process -Id $conn.OwningProcess -Force -ErrorAction SilentlyContinue
}

Start-Sleep -Seconds 2
$still = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($still) {
    Write-Host "Still bound after stopping - something else grabbed the port immediately." -ForegroundColor Yellow
    exit 1
}
Write-Host "Port $Port is free."
