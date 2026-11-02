# 集成验收：后端 lint/测试 + 前端类型检查/构建/单测。
# 用法（在项目根目录）：powershell -ExecutionPolicy Bypass -File tooling/check.ps1

$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot
$backend = Join-Path $root 'backend'
$web = Join-Path $root 'web'
$py = Join-Path $backend '.venv\Scripts\python.exe'

if (-not (Test-Path -LiteralPath $py)) {
    throw "未找到虚拟环境：$py。请先按 README 创建 backend/.venv。"
}

# 优先用 DSH 自带 node/pnpm（本机 PATH 上的 node 版本较旧）
$node = $env:DSH_NODE
$pnpm = $env:DSH_PNPM
if (-not $node) { $node = (Get-Command node -ErrorAction SilentlyContinue).Source }
if (-not $pnpm) { $pnpm = (Get-Command pnpm -ErrorAction SilentlyContinue).Source }

if (-not $node) { throw '未找到 Node.js' }

Push-Location $backend
try {
    Write-Host '== 后端 ruff =='
    & $py -m ruff check src tests
    if ($LASTEXITCODE -ne 0) { throw 'ruff 未通过' }

    Write-Host '== 后端 pytest =='
    & $py -m pytest
    if ($LASTEXITCODE -ne 0) { throw 'pytest 未通过' }
}
finally {
    Pop-Location
}

Push-Location $web
try {
    Write-Host '== 前端 typecheck =='
    & $node $pnpm typecheck
    if ($LASTEXITCODE -ne 0) { throw 'typecheck 未通过' }

    Write-Host '== 前端 build =='
    & $node $pnpm build
    if ($LASTEXITCODE -ne 0) { throw 'build 未通过' }

    Write-Host '== 前端 unit =='
    & $node $pnpm test:unit
    if ($LASTEXITCODE -ne 0) { throw 'test:unit 未通过' }
}
finally {
    Pop-Location
}

Write-Host '全部通过'
