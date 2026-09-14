param([ValidateRange(1, 65535)][int]$Port = 8810)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
if (-not (Test-Path -LiteralPath '.venv/Scripts/python.exe')) {
    python -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw 'Python環境を作成できませんでした。Python 3.11以上を確認してください。' }
}
& ./.venv/Scripts/python.exe -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw '依存パッケージのインストールに失敗しました。pip の proxy 設定を確認してください。' }
& ./.venv/Scripts/python.exe run.py --port $Port
