@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Please run Setup.cmd first.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -m chat_app %*
if errorlevel 1 pause
