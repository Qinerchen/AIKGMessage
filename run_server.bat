@echo off
cd /d "%~dp0"

rem Internal helper: runs the platform server in a self-healing loop.
rem Closing this window is THE way to stop the platform.

if not exist ".venv\Scripts\python.exe" goto :nopy

:loop
.venv\Scripts\python.exe src\api.py
echo.
echo === Server exited unexpectedly (see error above). ===
echo Restarting in 3 seconds... Close this window to stop the platform.
ping -n 4 127.0.0.1 >nul
goto :loop

:nopy
echo .venv not found. Run "uv sync" in this folder first.
pause
