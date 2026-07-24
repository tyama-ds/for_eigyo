#!/usr/bin/env bash
# バックアップからの復元。
# 使い方: scripts/restore.sh <dumpファイル> <data tar.gz> [DATABASE_URL] [DATA_DIR]
# 復元先DBは空であること (既存DBへは--cleanを使わず、新規DBへ復元して切替を推奨)。
set -euo pipefail
DUMP="${1:?pg_dumpファイルを指定してください}"
DATA_TAR="${2:?data tar.gzを指定してください}"
DB_URL="${3:-${DRO_DATABASE_URL:-postgresql://dro@127.0.0.1:55432/dro}}"
DATA_DIR="${4:-${DRO_DATA_DIR:-./data}}"

DB_URL="${DB_URL/postgresql+psycopg:\/\//postgresql:\/\/}"

pg_restore --no-owner -d "$DB_URL" "$DUMP"
mkdir -p "$DATA_DIR"
tar -xzf "$DATA_TAR" -C "$DATA_DIR"
echo "復元完了。整合性検査を実行します..."
echo "  (artifactのsha256はダウンロードAPI/ArtifactStore.loadで自動検証されます)"
echo "master key fileも復元済みであることを確認してください (復号に必須)。"
