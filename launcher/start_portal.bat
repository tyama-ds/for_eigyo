@echo off
rem ============================================================
rem  App Portal 起動バッチ
rem  ダブルクリックでポータルを起動し、ブラウザを自動で開く
rem  ポートを変えたい場合:  start_portal.bat --port 9200
rem
rem  %~dp0 = この bat ファイルが置いてあるフォルダのフルパス。
rem  launcher.py をフルパスで指定するので、どこから実行しても
rem  （ショートカット経由・別フォルダから呼んでも）確実に見つかる。
rem ============================================================
chcp 65001 >nul
title App Portal

set "PORTAL=%~dp0launcher.py"

if not exist "%PORTAL%" (
    echo [エラー] launcher.py が見つかりません:
    echo   %PORTAL%
    echo.
    echo この bat は launcher.py と同じフォルダ（for_eigyo\launcher\）に
    echo 置いたまま使ってください。デスクトップ等から使いたい場合は、
    echo bat 本体をコピーせず「ショートカット」を作成してください。
    pause
    exit /b 1
)

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
%PYCMD% "%PORTAL%" --open %*

rem 異常終了時はメッセージを読めるように止める
if errorlevel 1 pause
