# 本机验证：调用 tooling/check.ps1（后端 lint/测试 + 前端 typecheck/build/单测），
# 再请求 /api/v1/health/ready，最后输出中文结论。
# 用法（项目根目录，服务已启动时）：
#   powershell -ExecutionPolicy Bypass -File tooling/verify.ps1
#   powershell -ExecutionPolicy Bypass -File tooling/verify.ps1 -BaseUrl http://127.0.0.1:8010
#   powershell -ExecutionPolicy Bypass -File tooling/verify.ps1 -SkipHealth

[CmdletBinding()]
param(
    [string]$BaseUrl = 'http://127.0.0.1:8000',

    [switch]$SkipHealth
)

$ErrorActionPreference = 'Continue'

$root = Split-Path -Parent $PSScriptRoot
$backend = Join-Path $root 'backend'
$py = Join-Path $backend '.venv\Scripts\python.exe'
$checkScript = Join-Path $PSScriptRoot 'check.ps1'
$BaseUrl = $BaseUrl.TrimEnd('/')

$checkOk = $false
$healthOk = $false
$healthSkipped = $false

Write-Host '== 执行 tooling/check.ps1 =='
try {
    & $checkScript
    $checkOk = $true
}
catch {
    $checkOk = $false
    Write-Host "检查脚本失败：$($_.Exception.Message)" -ForegroundColor Red
}

if ($SkipHealth) {
    $healthSkipped = $true
    Write-Host '== 健康探针已跳过（-SkipHealth） =='
}
else {
    Write-Host '== 请求健康探针 =='
    if (-not (Test-Path -LiteralPath $py)) {
        Write-Host "未找到虚拟环境 Python：$py" -ForegroundColor Red
        $healthOk = $false
    }
    else {
        $healthUrl = "$BaseUrl/api/v1/health/ready"
        Push-Location $backend
        try {
            & $py -m dajidali healthcheck --url $healthUrl --timeout 10
            $healthOk = ($LASTEXITCODE -eq 0)
        }
        finally {
            Pop-Location
        }
    }
}

Write-Host ''
Write-Host '== 验证结论 =='
if ($checkOk) {
    Write-Host '后端/前端检查：通过' -ForegroundColor Green
}
else {
    Write-Host '后端/前端检查：未通过' -ForegroundColor Red
}
if ($healthSkipped) {
    Write-Host '健康探针：已跳过' -ForegroundColor Yellow
}
elseif ($healthOk) {
    Write-Host '健康探针：通过' -ForegroundColor Green
}
else {
    Write-Host '健康探针：未通过（请确认服务已启动，或检查上方错误）' -ForegroundColor Red
}

if ($checkOk -and ($healthSkipped -or $healthOk)) {
    Write-Host '总体结论：通过' -ForegroundColor Green
    exit 0
}

Write-Host '总体结论：未通过' -ForegroundColor Red
exit 1
