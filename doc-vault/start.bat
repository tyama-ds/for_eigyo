@echo off
rem ============================================================
rem  Doc Vault 起動バッチ
rem  ダブルクリックで Doc Vault を起動し、ブラウザを自動で開く。
rem  すでに起動していれば、ブラウザで開くだけ（二重には起動しない）。
rem
rem  オプションはそのまま server.py に渡る。例:
rem    start.bat --port 8761
rem    start.bat --data-dir D:\docvault-data
rem    start.bat --host 0.0.0.0     LAN に公開する（アクセストークン必須）
rem
rem  %~dp0 = この bat ファイルが置いてあるフォルダのフルパス。
rem  デスクトップから使うときは、bat をコピーせず「ショートカット」を作る。
rem  改行は CRLF のまま保存する（LF だと日本語版 Windows で chcp の行を読み違えて即終了する）。
rem  ( ) のブロックは使わない（Program Files (x86) のようにパスに ) があると壊れるため）。
rem ============================================================
chcp 65001 >nul
title Doc Vault
setlocal

set "APP=%~dp0server.py"
if exist "%APP%" goto app_found
echo [エラー] server.py が見つかりません:
echo   "%APP%"
echo この bat は doc-vault フォルダに置いたまま使ってください。
echo デスクトップから使うときは、bat をコピーせず「ショートカット」を作ってください。
pause
exit /b 1

:app_found
rem Python を探す（py ランチャー優先、なければ python）
set "PYCMD=py -3"
%PYCMD% --version >nul 2>nul
if not errorlevel 1 goto python_found
set "PYCMD=python"
%PYCMD% --version >nul 2>nul
if not errorlevel 1 goto python_found
echo [エラー] Python が見つかりません。
echo https://www.python.org/ から Python 3.11 以降をインストールするか、PATH を確認してください。
pause
exit /b 1

:python_found
%PYCMD% -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>nul
if errorlevel 1 echo [注意] 動作確認は Python 3.11 以降で行っています。古い版では動かないことがあります。

rem PDF の取り込みには pypdf が要る。入っていなければ、入れるかどうかを聞く（60 秒で「入れない」）
%PYCMD% -c "import importlib.util, sys; sys.exit(0 if importlib.util.find_spec('pypdf') else 1)" >nul 2>nul
if not errorlevel 1 goto deps_ok
echo.
echo PDF の取り込みに使う pypdf が入っていません（Word・Excel・PowerPoint は無くても読めます）。
echo pip で requirements.txt の pypdf と anthropic（Claude を使うとき）を入れられます。
choice /c YN /t 60 /d N /m "いまインストールしますか"
if errorlevel 2 goto deps_ok
%PYCMD% -m pip install -r "%~dp0requirements.txt"
if not errorlevel 1 goto deps_ok
echo.
echo [注意] インストールに失敗しました。社内ネットワークでは、プロキシを指定して入れてください:
echo   %PYCMD% -m pip install --proxy http://プロキシ:8080 -r "%~dp0requirements.txt"
echo このまま起動します（PDF は取り込めません）。
pause

:deps_ok
echo Doc Vault を起動します（終了するには Ctrl+C か、このウィンドウを閉じる）
%PYCMD% "%APP%" --open %*

rem 異常終了時はメッセージを読めるように止める
if errorlevel 1 pause
