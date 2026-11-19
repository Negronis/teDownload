@echo off
chcp 65001 >nul
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo [1/3] Creating venv...
  python -m venv .venv
)

echo [2/3] Installing dependencies...
".venv\Scripts\python.exe" -m pip install -r requirements.txt pyinstaller
if errorlevel 1 exit /b 1

echo [3/3] Building exe...
".venv\Scripts\python.exe" -m PyInstaller --noconfirm tg_downloader.spec
if errorlevel 1 exit /b 1

echo.
echo Done: dist\Telegram批量下载器.exe
echo Put config beside the exe if needed. Session/downloads will be created next to it.
pause
