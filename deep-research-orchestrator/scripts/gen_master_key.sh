#!/usr/bin/env bash
# secrets at rest用master keyの生成。
# 出力先はDB・リポジトリと別に保管し、バックアップすること (失うと保存済みsecretは復号不能)。
set -euo pipefail
OUT="${1:-./secrets/dro_master_key}"
mkdir -p "$(dirname "$OUT")"
if [ -f "$OUT" ]; then
  echo "既に存在します: $OUT (上書きしません)" >&2
  exit 1
fi
umask 077
head -c 32 /dev/urandom | base64 | tr -d '\n' > "$OUT"
echo "master keyを生成しました: $OUT"
echo "このファイルをバックアップし、リポジトリへコミットしないでください。"
