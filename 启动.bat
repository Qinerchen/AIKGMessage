@echo off
cd /d "%~dp0"

rem One-click RESTART for the local GongKao info platform.
rem 1) kill any instance already listening on port 8000
rem 2) start a fresh server in its own window
rem 3) open the browser

for /f "tokens=5" %%a in ('netstat -ano ^| findstr "LISTENING" ^| findstr ":8000"') do taskkill /PID %%a /F >nul 2>&1
ping -n 2 127.0.0.1 >nul

if exist ".venv\Scripts\python.exe" goto :venv

where uv >nul 2>nul
if errorlevel 1 goto :nouv

start "GongKao Platform" cmd /c "uv run python src\api.py"
goto :open

:venv
start "GongKao Platform" ".venv\Scripts\python.exe" src\api.py

:open
echo Platform is restarting at http://127.0.0.1:8000 ...
ping -n 4 127.0.0.1 >nul
start "" "http://127.0.0.1:8000"
echo Platform restarted. Close the server window to stop it.
ping -n 7 127.0.0.1 >nul
exit

:nouv
echo Python venv not found and uv is not installed.
echo Install uv first: https://docs.astral.sh/uv/getting-started/installation/
pause
