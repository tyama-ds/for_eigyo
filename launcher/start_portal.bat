@echo off
rem ============================================================
rem  App Portal 起動バッチ
rem  ダブルクリックでポータルを起動し、ブラウザを自動で開く
rem  ポートを変えたい場合:  start_portal.bat --port 9200
rem ============================================================
chcp 65001 >nul
title App Portal
cd /d "%~dp0"

rem Python を探す（py ランチャー優先、なければ python）
set "PYCMD=py -3"
%PYCMD% --version >nul 2>nul
if errorlevel 1 set "PYCMD=python"
%PYCMD% --version >nul 2>nul
if errorlevel 1 (
    echo [エラー] Python が見つかりません。
    echo https://www.python.org/ からインストールするか、PATH を確認してください。
    pause
    exit /b 1
)

echo App Portal を起動します（終了するには Ctrl+C か このウィンドウを閉じる）
%PYCMD% launcher.py --open %*

rem 異常終了時はメッセージを読めるように止める
if errorlevel 1 pause
