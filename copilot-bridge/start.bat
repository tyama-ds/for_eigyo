@echo off
rem Copilot Bridge を起動する（ダブルクリックで実行）。config.json があれば読む
cd /d "%~dp0"
where py >nul 2>nul && (py -3 server.py %*) || (python server.py %*)
pause
