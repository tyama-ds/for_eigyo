#!/usr/bin/env bash
# E2E用フルスタック起動/停止。
#   scripts/e2e_stack.sh start   # PG/Redis/API(8800)/worker/mock runner/frontend(3000)
#   scripts/e2e_stack.sh stop
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$(pwd)"
RUN_DIR="/var/tmp/dro-e2e"
PG_URL="postgresql+psycopg://dro@127.0.0.1:${DRO_DEV_PGPORT:-55432}/dro_e2e"
REDIS_URL="redis://127.0.0.1:${DRO_DEV_REDIS_PORT:-56379}/9"

export DRO_DATABASE_URL="$PG_URL"
export DRO_REDIS_URL="$REDIS_URL"
export DRO_DATA_DIR="$RUN_DIR/data"
export DRO_MASTER_KEY="e2e-master-key"
export DRO_MASTER_KEY_FILE="/nonexistent"
export DRO_RUNNER_MOCK_URL="http://127.0.0.1:9401"
export DRO_RUN_POLL_INTERVAL_SECONDS="0.2"
export DRO_RETRY_BACKOFF_BASE_SECONDS="0.2"
export DRO_RETRY_BACKOFF_MAX_SECONDS="1.0"
export DRO_STUCK_RUN_HEARTBEAT_SECONDS="10"
export DRO_API_RATE_LIMIT_PER_MINUTE="100000"
export DRO_CORS_ORIGINS='["http://127.0.0.1:3000","http://localhost:3000"]'
export DRO_LOG_LEVEL="WARNING"

PIDS_FILE="$RUN_DIR/pids"

start() {
  mkdir -p "$RUN_DIR/data"
  bash scripts/dev_infra.sh start
  local PSQL="psql postgresql://dro@127.0.0.1:${DRO_DEV_PGPORT:-55432}/postgres"
  $PSQL -tc "SELECT 1 FROM pg_database WHERE datname='dro_e2e'" | grep -q 1 \
    && $PSQL -c "DROP DATABASE dro_e2e WITH (FORCE)"
  $PSQL -c "CREATE DATABASE dro_e2e" >/dev/null
  . .venv/bin/activate
  (cd backend && python -m alembic upgrade head)
  redis-cli -p "${DRO_DEV_REDIS_PORT:-56379}" -n 9 flushdb >/dev/null

  : > "$PIDS_FILE"
  PYTHONPATH="$ROOT/runners/common:$ROOT/runners/mock" \
    python -m uvicorn main:app --app-dir runners/mock --host 127.0.0.1 --port 9401 \
    --log-level warning > "$RUN_DIR/mock.log" 2>&1 &
  echo $! >> "$PIDS_FILE"
  (cd backend && python -m uvicorn app.main:app --host 127.0.0.1 --port 8800 \
    --log-level warning > "$RUN_DIR/api.log" 2>&1 &
   echo $! >> "$PIDS_FILE")
  (cd backend && python -m celery -A app.orchestrator.celery_app worker -B \
    --loglevel warning --concurrency 8 --pool threads > "$RUN_DIR/worker.log" 2>&1 &
   echo $! >> "$PIDS_FILE")
  (cd frontend && npm run start > "$RUN_DIR/frontend.log" 2>&1 &
   echo $! >> "$PIDS_FILE")

  for url in "http://127.0.0.1:9401/healthz" "http://127.0.0.1:8800/readyz" "http://127.0.0.1:3000"; do
    for i in $(seq 1 60); do
      if curl -sf "$url" >/dev/null 2>&1; then break; fi
      sleep 0.5
      if [ "$i" = 60 ]; then echo "起動失敗: $url" >&2; exit 1; fi
    done
  done
  echo "E2E stack ready: frontend=http://127.0.0.1:3000 api=http://127.0.0.1:8800"
}

stop() {
  if [ -f "$PIDS_FILE" ]; then
    while read -r pid; do kill "$pid" 2>/dev/null || true; done < "$PIDS_FILE"
    rm -f "$PIDS_FILE"
  fi
  pkill -f "uvicorn main:app --app-dir runners/mock" 2>/dev/null || true
  pkill -f "uvicorn app.main:app --host 127.0.0.1 --port 8800" 2>/dev/null || true
  pkill -f "celery -A app.orchestrator.celery_app" 2>/dev/null || true
  pkill -f "next-server" 2>/dev/null || true
  echo "stopped"
}

case "${1:-start}" in
  start) start ;;
  stop) stop ;;
  *) echo "usage: $0 start|stop" >&2; exit 1 ;;
esac
