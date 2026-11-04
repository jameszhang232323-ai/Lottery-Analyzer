@echo off
title 大吉大利 - 启动
echo ================================
echo   大吉大利 福彩分析工作台
echo   正在启动，请稍候...
echo ================================
echo.
cd /d "%~dp0"
powershell -ExecutionPolicy Bypass -File tooling\start_local.ps1 -OpenBrowser
if errorlevel 1 (
    echo.
    echo 启动失败，请查看上方错误信息。
    echo 常见问题:
    echo   1. backend\.venv 不存在 - 先执行 python -m venv backend\.venv
    echo   2. web\dist 不存在 - 先在 web 目录执行 pnpm build
    echo   3. 其他错误 - 查看 backend\data\logs 下的日志
    pause
) else (
    echo.
    echo 服务已启动，地址见上方 “界面:” 一行，默认 8000 端口。
    echo 停止服务: 双击 停止服务.bat
    echo 本窗口可以关闭，服务在后台继续运行。
    ping -n 6 127.0.0.1 >nul
)
