@echo off
title 大吉大利 - 停止
echo ================================
echo   正在停止服务...
echo ================================
echo.
cd /d "%~dp0"
powershell -ExecutionPolicy Bypass -File tooling\stop_local.ps1
echo.
echo 服务已停止。
ping -n 4 127.0.0.1 >nul
