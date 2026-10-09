@echo off
setlocal
cd /d "%~dp0"
if defined LOCAL_CHAT_PYTHON (
  "%LOCAL_CHAT_PYTHON%" setup_app.py %*
  goto finish
)
where py >nul 2>nul
if not errorlevel 1 (
  py -3 setup_app.py %*
  goto finish
)
python setup_app.py %*
:finish
if errorlevel 1 (
  echo Setup failed. See the error above.
  pause
  exit /b 1
)
echo Setup complete. Open Launch.cmd to start.
pause
