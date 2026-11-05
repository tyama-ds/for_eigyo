@echo off
rem ============================================================
rem  RAG Orchestrator 起動バッチ
rem  ダブルクリックで RAG Orchestrator を起動し、ブラウザを自動で開く。
rem  すでに起動していれば、ブラウザで開くだけ（二重には起動しない）。
rem
rem  オプションはそのまま server.py に渡る。例:
rem    start.bat --port 9500
rem
rem  %~dp0 = この bat ファイルが置いてあるフォルダのフルパス。
rem  デスクトップから使うときは、bat をコピーせず「ショートカット」を作る。
rem  改行は CRLF のまま保存する（LF だと日本語版 Windows で chcp の行を読み違えて即終了する）。
rem  ( ) のブロックは使わない（Program Files (x86) のようにパスに ) があると壊れるため）。
rem ============================================================
chcp 65001 >nul
title RAG Orchestrator
setlocal

set "APP=%~dp0server.py"
if exist "%APP%" goto app_found
echo [エラー] server.py が見つかりません:
echo   "%APP%"
echo この bat は rag-orchestrator フォルダに置いたまま使ってください。
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

echo RAG Orchestrator を起動します（終了するには Ctrl+C か、このウィンドウを閉じる）
%PYCMD% "%APP%" --open %*

rem 異常終了時はメッセージを読めるように止める
if errorlevel 1 pause
