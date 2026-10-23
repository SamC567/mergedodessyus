#Requires -Version 5.1
<#
  Bring up the whole local Odysseus stack with one command:
    1. llama-server (Qwen3.6-35B-A3B by default, Vulkan) -> http://127.0.0.1:8080
    2. ChromaDB server                                    -> http://127.0.0.1:8100
    3. Odysseus (launch-windows.ps1)                      -> http://127.0.0.1:7000  (foreground; Ctrl+C stops the app only)

  llama-server and Chroma keep running in the background; stop them with
  scripts\windows\stop-all.ps1.

  Usage: powershell -ExecutionPolicy Bypass -File scripts\windows\start-all.ps1 [-Model qwen3.6|gpt-oss] [-CpuMoe N]
#>
param(
    [ValidateSet("gpt-oss", "qwen3.6")]
    [string]$Model = "qwen3.6",
    [int]$CpuMoe = -1,
    [int]$AppPort = 7000
)

$ErrorActionPreference = "Stop"
$repo = Resolve-Path (Join-Path $PSScriptRoot "..\..")

& (Join-Path $PSScriptRoot "start-llama.ps1") -Model $Model -CpuMoe $CpuMoe
& (Join-Path $PSScriptRoot "start-chroma.ps1")

# DuckDuckGo search provider needs ddgs (optional dependency, not in requirements.txt).
$venvPy = Join-Path $repo "venv\Scripts\python.exe"
if (Test-Path $venvPy) {
    & $venvPy -c "import ddgs" 2>$null
    if ($LASTEXITCODE -ne 0) { Write-Host "Installing ddgs for DuckDuckGo search..."; & $venvPy -m pip install --quiet ddgs }
}

# Point the app at the local Chroma explicitly (IPv4 loopback, not 'localhost').
$env:CHROMADB_HOST = "127.0.0.1"
$env:CHROMADB_PORT = "8100"
$env:ODYSSEUS_SKIP_ADMIN_PROMPT = "1"

& (Join-Path $repo "launch-windows.ps1") -Port $AppPort -BindHost "127.0.0.1"
