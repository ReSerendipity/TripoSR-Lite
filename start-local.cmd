@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo [MeshMint] creating virtual environment...
  python -m venv .venv
  if errorlevel 1 goto :error
)

call ".venv\Scripts\activate.bat"
python -m pip install --upgrade pip >nul 2>nul
python -m pip install -r requirements.txt
if errorlevel 1 goto :error

echo.
echo [MeshMint] http://127.0.0.1:7861
echo [MeshMint] 未下载权重时界面会显示 NOT_READY 与 DEMO 演示网格。
python -m meshmint serve --host 127.0.0.1 --port 7861
goto :eof

:error
echo.
echo [MeshMint] 启动失败，请检查 Python 与依赖安装。
pause
