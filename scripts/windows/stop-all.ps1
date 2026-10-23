#Requires -Version 5.1
<#
  Stop the local Odysseus stack started by start-all.ps1: whatever is listening
  on the app, Chroma and llama-server ports.

  Usage: powershell -ExecutionPolicy Bypass -File scripts\windows\stop-all.ps1
#>
param([int[]]$Ports = @(7000, 8100, 8080))

foreach ($port in $Ports) {
    $pids = Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue |
        Select-Object -ExpandProperty OwningProcess -Unique
    if (-not $pids) { Write-Host "port ${port}: nothing listening"; continue }
    foreach ($procId in $pids) {
        $name = (Get-Process -Id $procId -ErrorAction SilentlyContinue).ProcessName
        Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue
        Write-Host "port ${port}: stopped $name (pid $procId)"
    }
}
