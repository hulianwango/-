@echo off
setlocal
cd /d "%~dp0"
set "PORT=8000"
if not "%LIT_MCP_PORT%"=="" set "PORT=%LIT_MCP_PORT%"
if not "%~1"=="" set "PORT=%~1"

echo Starting backend tunnel on local port %PORT%.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start_backend_tunnel.ps1" -Port %PORT% -VisibleWindows -Monitor
echo.
pause
