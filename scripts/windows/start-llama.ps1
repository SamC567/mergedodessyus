#Requires -Version 5.1
<#
  Start llama-server (Vulkan build) with gpt-oss-20b for Odysseus.

  Tuned for an 8 GB RX 5700 XT: all layers on GPU (-ngl 99) with the MoE
  experts of the first N layers kept on CPU (--n-cpu-moe). 11 leaves ~800 MiB
  of VRAM headroom at 16k context; raise it if other apps need VRAM, lower it
  only if you have more free (Vulkan silently spills to shared memory when
  over budget, which tanks speed).

  The --alias matches the app's default_model so the endpoint's model id is
  stable regardless of where the GGUF lives.

  Usage: powershell -ExecutionPolicy Bypass -File scripts\windows\start-llama.ps1 [-CpuMoe 11] [-Foreground]
#>
param(
    [int]$CpuMoe = 11,
    [int]$Ctx = 16384,
    [int]$Port = 8080,
    [string]$BindHost = "127.0.0.1",
    [string]$ModelPath = "",
    [string]$LlamaExe = "",
    [switch]$Foreground
)

$ErrorActionPreference = "Stop"
$repo = Resolve-Path (Join-Path $PSScriptRoot "..\..")

function Test-Port($p) {
    try { $c = New-Object Net.Sockets.TcpClient; $c.Connect("127.0.0.1", $p); $c.Close(); return $true } catch { return $false }
}

if (Test-Port $Port) { Write-Host "llama-server: port $Port already in use - assuming it's running."; exit 0 }

if (-not $LlamaExe) {
    $cmd = Get-Command llama-server -ErrorAction SilentlyContinue
    if ($cmd) { $LlamaExe = $cmd.Source }
    else {
        $LlamaExe = Get-ChildItem "$env:LOCALAPPDATA\Microsoft\WinGet\Packages\ggml.llamacpp_*\llama-server.exe" -ErrorAction SilentlyContinue |
            Select-Object -First 1 -ExpandProperty FullName
    }
}
if (-not $LlamaExe -or -not (Test-Path $LlamaExe)) { throw "llama-server.exe not found. Install with: winget install ggml.llamacpp" }

if (-not $ModelPath) {
    $ModelPath = Get-ChildItem "$env:USERPROFILE\.cache\huggingface\hub\models--unsloth--gpt-oss-20b-GGUF\snapshots\*\gpt-oss-20b-Q4_K_M.gguf" -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending | Select-Object -First 1 -ExpandProperty FullName
}
if (-not $ModelPath -or -not (Test-Path $ModelPath)) { throw "gpt-oss-20b-Q4_K_M.gguf not found in the Hugging Face cache. Pass -ModelPath." }

$llamaArgs = @(
    "-m", "`"$ModelPath`"",
    "--alias", "unsloth/gpt-oss-20b-GGUF",
    "--host", $BindHost, "--port", "$Port",
    "-c", "$Ctx", "--jinja", "-ngl", "99", "--n-cpu-moe", "$CpuMoe"
)

if ($Foreground) { & $LlamaExe @llamaArgs; exit $LASTEXITCODE }

$logDir = Join-Path $repo "logs"
New-Item -ItemType Directory -Force $logDir | Out-Null
$proc = Start-Process -FilePath $LlamaExe -ArgumentList $llamaArgs -WindowStyle Hidden -PassThru `
    -RedirectStandardOutput (Join-Path $logDir "llama-server.out.log") `
    -RedirectStandardError (Join-Path $logDir "llama-server.log")

Write-Host "llama-server: starting (pid $($proc.Id), --n-cpu-moe $CpuMoe, -c $Ctx)..."
for ($i = 0; $i -lt 120; $i++) {
    if ($proc.HasExited) { throw "llama-server exited (code $($proc.ExitCode)). See logs\llama-server.log" }
    try {
        if ((Invoke-RestMethod "http://127.0.0.1:$Port/health" -TimeoutSec 2).status -eq "ok") {
            $nctx = ((Invoke-RestMethod "http://127.0.0.1:$Port/slots" -TimeoutSec 5) | Select-Object -First 1).n_ctx
            Write-Host "llama-server: ready on http://${BindHost}:$Port (n_ctx=$nctx)" -ForegroundColor Green
            exit 0
        }
    } catch {}
    Start-Sleep 2
}
throw "llama-server did not become healthy within 240s. See logs\llama-server.log"
