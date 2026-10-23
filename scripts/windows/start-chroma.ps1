#Requires -Version 5.1
<#
  Start a standalone ChromaDB server for Odysseus on 127.0.0.1:8100.

  The server lives in its own venv (.venv-chroma) because the app venv only
  has the lightweight chromadb-client. The server version is pinned to the
  client's version so the HTTP API matches. Data: data\chroma.

  Usage: powershell -ExecutionPolicy Bypass -File scripts\windows\start-chroma.ps1 [-Foreground]
#>
param(
    [int]$Port = 8100,
    [string]$BindHost = "127.0.0.1",
    [switch]$Foreground
)

$ErrorActionPreference = "Stop"
$repo = Resolve-Path (Join-Path $PSScriptRoot "..\..")
$venv = Join-Path $repo ".venv-chroma"
$chroma = Join-Path $venv "Scripts\chroma.exe"
$dataDir = Join-Path $repo "data\chroma"

function Test-Port($p) {
    try { $c = New-Object Net.Sockets.TcpClient; $c.Connect("127.0.0.1", $p); $c.Close(); return $true } catch { return $false }
}

if (Test-Port $Port) { Write-Host "chroma: port $Port already in use - assuming it's running."; exit 0 }

if (-not (Test-Path $chroma)) {
    $clientVer = (& (Join-Path $repo "venv\Scripts\python.exe") -c "import importlib.metadata as m; print(m.version('chromadb-client'))").Trim()
    Write-Host "chroma: creating $venv with chromadb==$clientVer (matches app's chromadb-client)..."
    py -3.12 -m venv $venv
    & (Join-Path $venv "Scripts\python.exe") -m pip install --quiet --upgrade pip
    & (Join-Path $venv "Scripts\python.exe") -m pip install --quiet "chromadb==$clientVer"
    if (-not (Test-Path $chroma)) { throw "chromadb install failed." }
}

New-Item -ItemType Directory -Force $dataDir | Out-Null
$env:ANONYMIZED_TELEMETRY = "False"
$chromaArgs = @("run", "--host", $BindHost, "--port", "$Port", "--path", "`"$dataDir`"")

if ($Foreground) { & $chroma @chromaArgs; exit $LASTEXITCODE }

$logDir = Join-Path $repo "logs"
New-Item -ItemType Directory -Force $logDir | Out-Null
$proc = Start-Process -FilePath $chroma -ArgumentList $chromaArgs -WindowStyle Hidden -PassThru `
    -RedirectStandardOutput (Join-Path $logDir "chroma.log") `
    -RedirectStandardError (Join-Path $logDir "chroma.err.log")

Write-Host "chroma: starting (pid $($proc.Id))..."
for ($i = 0; $i -lt 30; $i++) {
    if ($proc.HasExited) { throw "chroma exited (code $($proc.ExitCode)). See logs\chroma.err.log" }
    try {
        Invoke-RestMethod "http://127.0.0.1:$Port/api/v2/heartbeat" -TimeoutSec 2 | Out-Null
        Write-Host "chroma: ready on http://${BindHost}:$Port (data: $dataDir)" -ForegroundColor Green
        exit 0
    } catch {}
    Start-Sleep 1
}
throw "chroma did not answer its heartbeat within 30s. See logs\chroma.err.log"
