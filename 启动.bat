@echo off
chcp 65001 >nul
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" (
  start "西安公考情报平台" ".venv\Scripts\python.exe" src\api.py
) else (
  where uv >nul 2>nul
  if errorlevel 1 (
    echo 未找到 .venv 且未安装 uv，请先安装 uv：https://docs.astral.sh/uv/
    pause
    exit /b 1
  )
  start "西安公考情报平台" cmd /c "uv run python src\api.py"
)

echo 正在启动平台...
timeout /t 3 /nobreak >nul
start "" "http://127.0.0.1:8000"
echo 平台已启动：http://127.0.0.1:8000
echo 关闭弹出的服务窗口即可停止平台。
timeout /t 8 >nul
