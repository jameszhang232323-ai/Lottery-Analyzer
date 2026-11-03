# 本机停止：按 data/run/*.pid 停止 API/worker/scheduler，并清理 PID 文件。
# 用法（项目根目录）：
#   powershell -ExecutionPolicy Bypass -File tooling/stop_local.ps1
#   powershell -ExecutionPolicy Bypass -File tooling/stop_local.ps1 -DataDir .\backend\data

[CmdletBinding()]
param(
    [string]$DataDir = ''
)

$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot
$backend = Join-Path $root 'backend'

if ([string]::IsNullOrWhiteSpace($DataDir)) {
    if (-not [string]::IsNullOrWhiteSpace($env:DAJIDALI_DATA_DIR)) {
        $DataDir = $env:DAJIDALI_DATA_DIR
    }
    else {
        $DataDir = Join-Path $backend 'data'
    }
}
if (-not [System.IO.Path]::IsPathRooted($DataDir)) {
    $DataDir = Join-Path (Get-Location).Path $DataDir
}
$DataDir = [System.IO.Path]::GetFullPath($DataDir)
$runDir = Join-Path $DataDir 'run'

if (-not (Test-Path -LiteralPath $runDir -PathType Container)) {
    Write-Host "未发现运行目录：$runDir（服务可能未启动）。"
    exit 0
}

$failed = $false
foreach ($name in @('api', 'worker', 'scheduler')) {
    $pidFile = Join-Path $runDir "$name.pid"
    if (-not (Test-Path -LiteralPath $pidFile)) {
        Write-Host "未发现 $name 的 PID 文件，跳过。"
        continue
    }

    $raw = (Get-Content -LiteralPath $pidFile -Raw -ErrorAction SilentlyContinue)
    $processId = 0
    if (-not [int]::TryParse(([string]$raw).Trim(), [ref]$processId)) {
        Write-Warning "$name 的 PID 文件内容无效，已清理：$pidFile"
        Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue
        continue
    }

    $proc = Get-Process -Id $processId -ErrorAction SilentlyContinue
    if ($null -eq $proc) {
        Write-Host "$name（PID $processId）进程不存在，已清理 PID 文件。"
        Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue
        continue
    }

    $cim = Get-CimInstance Win32_Process -Filter "ProcessId = $processId" -ErrorAction SilentlyContinue
    if ($null -ne $cim -and $cim.CommandLine -and $cim.CommandLine -notmatch 'dajidali') {
        Write-Warning "$name 的 PID $processId 已被其他程序复用，不结束该进程，仅清理 PID 文件。"
        Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue
        continue
    }

    try {
        Write-Host "停止 $name（PID $processId）..."
        & taskkill.exe /PID $processId /T /F 2>$null | Out-Null
        if ($LASTEXITCODE -ne 0) {
            Stop-Process -Id $processId -Force -ErrorAction SilentlyContinue
        }
        Wait-Process -Id $processId -Timeout 5 -ErrorAction SilentlyContinue
        if ($null -ne (Get-Process -Id $processId -ErrorAction SilentlyContinue)) {
            throw "进程 $processId 仍在运行，请以管理员身份重试或手动结束。"
        }
        Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue
        Write-Host "$name 已停止。"
    }
    catch {
        $failed = $true
        Write-Host "停止 $name 失败：$($_.Exception.Message)" -ForegroundColor Red
    }
}

if ($failed) {
    Write-Host '停止完成，但存在失败项，请按上方提示处理。' -ForegroundColor Yellow
    exit 1
}

Write-Host '本机服务已全部停止。' -ForegroundColor Green
exit 0
