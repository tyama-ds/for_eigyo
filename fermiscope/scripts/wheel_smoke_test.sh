#!/usr/bin/env bash
# 非editable wheel をクリーン環境へインストールし、demo と serve(静的ファイル取得)を検証する。
# CI から実行する(外部APIキー不要・モック検索のみ)。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

cd "$ROOT"
rm -rf dist build
python -m build --wheel
WHEEL="$(ls dist/*.whl)"

python -m venv "$WORK/venv"
"$WORK/venv/bin/pip" install -q "$WHEEL"

export FERMISCOPE_DATA_DIR="$WORK/data"
mkdir -p "$FERMISCOPE_DATA_DIR"

# 1) demo(パッケージ外のCWDから実行)
cd /tmp
"$WORK/venv/bin/fermiscope" demo | grep -q "結論" && echo "OK: wheel demo"

# 2) serve + 静的ファイル取得
"$WORK/venv/bin/fermiscope" serve --host 127.0.0.1 --port 8796 &
SV=$!
trap 'kill $SV 2>/dev/null || true; rm -rf "$WORK"' EXIT
for _ in $(seq 1 20); do
  if curl -sf http://127.0.0.1:8796/ >/dev/null 2>&1; then break; fi
  sleep 1
done
curl -sf http://127.0.0.1:8796/ >/dev/null && echo "OK: wheel serve /"
curl -sf http://127.0.0.1:8796/static/css/app.css >/dev/null && echo "OK: wheel serve static"
curl -sf http://127.0.0.1:8796/api/config >/dev/null && echo "OK: wheel serve api"
echo "wheel smoke test passed"
