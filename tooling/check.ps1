# 集成验收：后端 lint/测试 + 前端类型检查/构建/单测。
# 用法（在项目根目录）：powershell -ExecutionPolicy Bypass -File tooling/check.ps1

# PowerShell 5.1 会把原生命令写到 stderr 的正常输出当成错误记录，
# 这里用 Continue 并逐个检查 $LASTEXITCODE。
$ErrorActionPreference = 'Continue'

$root = Split-Path -Parent $PSScriptRoot
$backend = Join-Path $root 'backend'
$web = Join-Path $root 'web'
$py = Join-Path $backend '.venv\Scripts\python.exe'

if (-not (Test-Path -LiteralPath $py)) {
    throw "未找到虚拟环境：$py。请先按 README 创建 backend/.venv。"
}

# 优先用 DSH 自带 node/pnpm（本机 PATH 上的 node 版本较旧，且 pnpm 不在 PATH）
$dshRuntime = 'C:\Users\12160\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies'
$node = $env:DSH_NODE
$pnpm = $env:DSH_PNPM
if (-not $node) { $node = Join-Path $dshRuntime 'node\bin\node.exe' }
if (-not $pnpm) { $pnpm = Join-Path $dshRuntime 'pnpm\bin\pnpm.mjs' }
if (-not (Test-Path -LiteralPath $node)) { $node = (Get-Command node -ErrorAction SilentlyContinue).Source }
if (-not (Test-Path -LiteralPath $pnpm)) { $pnpm = (Get-Command pnpm -ErrorAction SilentlyContinue).Source }

if (-not $node) { throw '未找到 Node.js' }
if (-not $pnpm) { throw '未找到 pnpm（可设置环境变量 DSH_PNPM 指向 pnpm.mjs）' }

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
    # pnpm 会把 "> script" 横幅写到 stderr，屏蔽掉以免污染输出；失败仍由退出码判断。
    Write-Host '== 前端 typecheck =='
    & $node $pnpm typecheck 2>$null
    if ($LASTEXITCODE -ne 0) { throw 'typecheck 未通过' }

    Write-Host '== 前端 build =='
    & $node $pnpm build 2>$null
    if ($LASTEXITCODE -ne 0) { throw 'build 未通过' }

    Write-Host '== 前端 unit =='
    & $node $pnpm test:unit 2>$null
    if ($LASTEXITCODE -ne 0) { throw 'test:unit 未通过' }
}
finally {
    Pop-Location
}

Write-Host '全部通过'
