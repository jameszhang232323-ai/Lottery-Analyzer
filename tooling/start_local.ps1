# 本机启动：检查环境 -> 单次迁移 -> 隐藏启动 API/worker/scheduler -> 等待就绪。
# 用法（项目根目录）：
#   powershell -ExecutionPolicy Bypass -File tooling/start_local.ps1 -OpenBrowser
#   powershell -ExecutionPolicy Bypass -File tooling/start_local.ps1 -Port 8010 -DataDir .\backend\data

[CmdletBinding()]
param(
    [ValidateRange(1, 65535)]
    [int]$Port = 8000,

    [string]$DataDir = '',

    [switch]$OpenBrowser
)

$ErrorActionPreference = 'Stop'

function Test-PortInUse {
    param([int]$PortNumber)

    $cmd = Get-Command Get-NetTCPConnection -ErrorAction SilentlyContinue
    if ($null -ne $cmd) {
        $connections = Get-NetTCPConnection -LocalPort $PortNumber -State Listen -ErrorAction SilentlyContinue
        return ($null -ne $connections)
    }

    # 旧版 Windows 没有 Get-NetTCPConnection 时的回退；只用于本机 loopback。
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $client.Connect('127.0.0.1', $PortNumber)
        return $true
    }
    catch {
        return $false
    }
    finally {
        $client.Close()
    }
}

function Get-OwnedProcess {
    param([int]$ProcessId)

    $proc = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
    if ($null -eq $proc) {
        return $null
    }
    $cim = Get-CimInstance Win32_Process -Filter "ProcessId = $ProcessId" -ErrorAction SilentlyContinue
    if ($null -ne $cim -and $cim.CommandLine -and $cim.CommandLine -notmatch 'dajidali') {
        # PID 已被系统复用给其他程序，不能当作本项目进程。
        return $null
    }
    return $proc
}

$root = Split-Path -Parent $PSScriptRoot
$backend = Join-Path $root 'backend'
$py = Join-Path $backend '.venv\Scripts\python.exe'

if (-not (Test-Path -LiteralPath $py)) {
    throw "未找到虚拟环境 Python：$py。请先按 README 创建 backend/.venv 并安装依赖。"
}

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
if (Test-Path -LiteralPath $DataDir -PathType Leaf) {
    throw "数据目录路径指向一个文件：$DataDir。请使用 -DataDir 指定目录。"
}
$runDir = Join-Path $DataDir 'run'
$logDir = Join-Path $DataDir 'logs'
New-Item -ItemType Directory -Force -Path $DataDir, $runDir, $logDir | Out-Null

Write-Host '== 检查 Python 环境 =='
$versionText = (& $py -c "import sys; print(str(sys.version_info[0]) + '.' + str(sys.version_info[1]))" 2>$null | Select-Object -Last 1)
if ($LASTEXITCODE -ne 0) {
    throw '无法运行 backend/.venv 中的 Python，请检查虚拟环境是否损坏。'
}
try {
    $pythonVersion = [version]$versionText.Trim()
}
catch {
    throw "无法识别 Python 版本：$versionText"
}
if ($pythonVersion -lt [version]'3.12') {
    throw "Python 版本过低：$versionText。本项目需要 3.12 或更高版本。"
}
& $py -c "import dajidali, uvicorn, alembic" 2>$null
if ($LASTEXITCODE -ne 0) {
    throw "虚拟环境缺少项目依赖。请在项目根目录执行：backend\.venv\Scripts\python.exe -m pip install -e `"backend[dev]`""
}

Write-Host '== 检查前端构建产物 =='
$staticDir = Join-Path $root 'web\dist'
$staticAvailable = Test-Path -LiteralPath (Join-Path $staticDir 'index.html')
if ($staticAvailable) {
    $env:DAJIDALI_STATIC_DIR = $staticDir
    Write-Host "静态资源：$staticDir"
}
else {
    Remove-Item Env:\DAJIDALI_STATIC_DIR -ErrorAction SilentlyContinue
    Write-Warning "未找到 $staticDir\index.html。后端仍会启动，但浏览器根路径不会显示界面。请先在 web 目录执行 pnpm build。"
}

Write-Host '== 检查重复启动与端口 =='
foreach ($name in @('api', 'worker', 'scheduler')) {
    $pidFile = Join-Path $runDir "$name.pid"
    if (-not (Test-Path -LiteralPath $pidFile)) {
        continue
    }
    $raw = (Get-Content -LiteralPath $pidFile -Raw -ErrorAction SilentlyContinue)
    $processId = 0
    if (-not [int]::TryParse(([string]$raw).Trim(), [ref]$processId)) {
        Write-Warning "PID 文件内容无效，已清理：$pidFile"
        Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue
        continue
    }
    $owned = Get-OwnedProcess -ProcessId $processId
    if ($null -eq $owned) {
        Write-Warning "发现过期 PID 文件（进程 $processId 不存在或不属于本项目），已清理：$pidFile"
        Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue
        continue
    }
    throw "检测到 $name 已在运行（PID $processId）。请先执行 tooling\stop_local.ps1，或确认后手动停止该进程。"
}

if (Test-PortInUse -PortNumber $Port) {
    throw "端口 $Port 已被占用。请关闭占用该端口的进程，或使用 -Port 指定其他端口。"
}

Write-Host '== 执行数据库迁移（每次启动只执行一次） =='
Push-Location $backend
try {
    & $py -m dajidali init-db --data-dir $DataDir
    if ($LASTEXITCODE -ne 0) {
        throw "数据库迁移失败（退出码 $LASTEXITCODE）。请查看上方输出后重试。"
    }
}
finally {
    Pop-Location
}

$env:DAJIDALI_DATA_DIR = $DataDir
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$started = @()

function Start-DajidaliProcess {
    param(
        [string]$Name,
        [string[]]$Arguments
    )

    $outLog = Join-Path $logDir "$Name-$stamp.out.log"
    $errLog = Join-Path $logDir "$Name-$stamp.err.log"
    $proc = Start-Process -FilePath $py -ArgumentList $Arguments -WorkingDirectory $backend -WindowStyle Hidden -RedirectStandardOutput $outLog -RedirectStandardError $errLog -PassThru
    Set-Content -LiteralPath (Join-Path $runDir "$Name.pid") -Value $proc.Id -Encoding Ascii
    $item = [pscustomobject]@{
        Name    = $Name
        Process = $proc
        OutLog  = $outLog
        ErrLog  = $errLog
    }
    return $item
}

try {
    Write-Host '== 启动 API / worker / scheduler =='
    $started += Start-DajidaliProcess -Name 'api' -Arguments @(
        '-m', 'dajidali', 'serve-api', '--host', '127.0.0.1', '--port', [string]$Port
    )
    $started += Start-DajidaliProcess -Name 'worker' -Arguments @(
        '-m', 'dajidali', 'serve-worker', '--poll-interval', '2'
    )
    $started += Start-DajidaliProcess -Name 'scheduler' -Arguments @(
        '-m', 'dajidali', 'serve-scheduler', '--interval', '30'
    )

    $readyUrl = "http://127.0.0.1:$Port/api/v1/health/ready"
    $deadline = (Get-Date).AddSeconds(60)
    $ready = $false
    Write-Host "等待服务就绪：$readyUrl"
    while ((Get-Date) -lt $deadline) {
        if ($null -eq (Get-Process -Id $started[0].Process.Id -ErrorAction SilentlyContinue)) {
            break
        }
        try {
            $response = Invoke-WebRequest -Uri $readyUrl -UseBasicParsing -TimeoutSec 2
            if ($response.StatusCode -eq 200) {
                $ready = $true
                break
            }
        }
        catch {
            Start-Sleep -Milliseconds 500
        }
    }

    if (-not $ready) {
        throw "API 未在 60 秒内就绪。请查看日志：$($started[0].OutLog) 和 $($started[0].ErrLog)。"
    }

    Write-Host ''
    Write-Host '本机服务已启动：' -ForegroundColor Green
    Write-Host "  界面：http://127.0.0.1:$Port/"
    Write-Host "  健康检查：$readyUrl"
    Write-Host "  数据目录：$DataDir"
    Write-Host "  日志目录：$logDir"
    Write-Host '停止服务：powershell -ExecutionPolicy Bypass -File tooling\stop_local.ps1'
    if (-not $staticAvailable) {
        Write-Host "  说明：前端尚未构建，根路径可能返回 404；请先在 web 目录执行 pnpm build。"
    }
    if ($OpenBrowser) {
        if ($staticAvailable) {
            Start-Process "http://127.0.0.1:$Port/"
        }
        else {
            Start-Process "http://127.0.0.1:$Port/api/v1/docs"
        }
    }
}
catch {
    Write-Host "启动失败：$($_.Exception.Message)" -ForegroundColor Red
    foreach ($item in $started) {
        if ($null -ne (Get-Process -Id $item.Process.Id -ErrorAction SilentlyContinue)) {
            Stop-Process -Id $item.Process.Id -Force -ErrorAction SilentlyContinue
        }
        Remove-Item -LiteralPath (Join-Path $runDir "$($item.Name).pid") -Force -ErrorAction SilentlyContinue
    }
    throw
}
