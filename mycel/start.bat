@echo off
rem Mycel を起動してブラウザを開く（ダブルクリックで実行）
cd /d "%~dp0"
where py >nul 2>nul && (py -3 server.py --open %*) || (python server.py --open %*)
pause
