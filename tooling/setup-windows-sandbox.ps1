# 本机开发环境补丁：只做两件事，都能用 `git config --unset` 或删除文件撤销。
#
# 1. 让 Python 在受限沙箱里使用临时目录（pip、pytest 缓存等）。
# 2. 让 Git 在受限沙箱里用 OpenSSL 而不是 schannel 建立 TLS 连接（只影响读取操作）。
#
# 用法（在项目根目录）：
#   powershell -ExecutionPolicy Bypass -File tooling/setup-windows-sandbox.ps1

$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot
$venvSite = Join-Path $root 'backend\.venv\Lib\site-packages'
$source = Join-Path $PSScriptRoot 'windows-sandbox\sitecustomize.py'

if (-not (Test-Path -LiteralPath $venvSite)) {
    throw "未找到虚拟环境：$venvSite。请先按 README 创建 backend/.venv。"
}

Copy-Item -LiteralPath $source -Destination (Join-Path $venvSite 'sitecustomize.py') -Force
Write-Host "已安装 sitecustomize.py -> $venvSite"

Push-Location $root
try {
    git config --local http.sslBackend openssl
    Write-Host "已设置本仓库 http.sslBackend=openssl（沙箱内可读取远端；推送仍需要凭据）"
}
finally {
    Pop-Location
}

Write-Host "验证："
& (Join-Path $root 'backend\.venv\Scripts\python.exe') -c "import tempfile, os; d = tempfile.mkdtemp(); open(os.path.join(d, 'ok'), 'w').write('1'); print('  临时目录可用:', d)"
