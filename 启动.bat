@echo off
cd /d "%~dp0"

rem One-click RESTART for the local GongKao info platform.
rem 1) kill any instance already listening on port 8000
rem 2) start a fresh server (its window STAYS OPEN and shows any error)
rem 3) verify the port is really up, then open the browser

for /f "tokens=5" %%a in ('netstat -ano ^| findstr "LISTENING" ^| findstr ":8000"') do taskkill /PID %%a /F >nul 2>&1
ping -n 2 127.0.0.1 >nul

if exist ".venv\Scripts\python.exe" goto :venv

where uv >nul 2>nul
if errorlevel 1 goto :nouv

start "GongKao Platform" cmd /c "uv run python src\api.py & echo. & echo === Server exited === & pause"
goto :check

:venv
start "GongKao Platform" cmd /c "run_server.bat"

:check
echo Waiting for the platform to come up...
ping -n 5 127.0.0.1 >nul
netstat -ano | findstr "LISTENING" | findstr ":8000" >nul
if errorlevel 1 goto :down

start "" "http://127.0.0.1:8000"
echo Platform is running at http://127.0.0.1:8000
echo Keep the SERVER window (GongKao Platform) open. Close it to stop the platform.
echo This launcher window will close soon.
ping -n 8 127.0.0.1 >nul
exit

:down
echo.
echo [ERROR] Server did NOT start. Look at the "GongKao Platform" window
echo         for the error message, then send it to me.
pause
exit

:nouv
echo Python venv not found and uv is not installed.
echo Install uv first: https://docs.astral.sh/uv/getting-started/installation/
pause
