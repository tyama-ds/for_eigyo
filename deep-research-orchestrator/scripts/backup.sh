#!/usr/bin/env bash
# PostgreSQLとDATA_DIRのバックアップ。
# 使い方: scripts/backup.sh <出力dir> [DATABASE_URL] [DATA_DIR]
# compose環境の例:
#   scripts/backup.sh ./backups "postgresql://dro:PASS@localhost:5432/dro" ./data
set -euo pipefail
OUT_DIR="${1:?出力dirを指定してください}"
DB_URL="${2:-${DRO_DATABASE_URL:-postgresql://dro@127.0.0.1:55432/dro}}"
DATA_DIR="${3:-${DRO_DATA_DIR:-./data}}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$OUT_DIR"

# SQLAlchemy形式のURLをlibpq形式へ
DB_URL="${DB_URL/postgresql+psycopg:\/\//postgresql:\/\/}"

pg_dump -Fc -f "$OUT_DIR/dro-$STAMP.dump" "$DB_URL"
tar -czf "$OUT_DIR/data-$STAMP.tar.gz" -C "$DATA_DIR" .
sha256sum "$OUT_DIR/dro-$STAMP.dump" "$OUT_DIR/data-$STAMP.tar.gz" > "$OUT_DIR/backup-$STAMP.sha256"
echo "バックアップ完了: $OUT_DIR/dro-$STAMP.dump / data-$STAMP.tar.gz"
echo "注意: master key file (DRO_MASTER_KEY_FILE) は別途安全な場所へバックアップしてください。"
