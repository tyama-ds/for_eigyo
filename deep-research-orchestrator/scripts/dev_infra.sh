#!/usr/bin/env bash
# ローカル開発/テスト用のPostgreSQL+Redis起動 (Dockerデーモン不要環境向け)。
# 使い方: scripts/dev_infra.sh start|stop|status
set -euo pipefail

PGBIN="${PGBIN:-/usr/lib/postgresql/16/bin}"
PGDATA="${DRO_DEV_PGDATA:-/var/tmp/dro-pgdata}"
PGPORT="${DRO_DEV_PGPORT:-55432}"
REDIS_PORT="${DRO_DEV_REDIS_PORT:-56379}"

run_as_pg() {
  if [ "$(id -u)" = "0" ]; then
    su postgres -s /bin/bash -c "$*"
  else
    bash -c "$*"
  fi
}

start() {
  if [ ! -f "$PGDATA/PG_VERSION" ]; then
    mkdir -p "$PGDATA"
    if [ "$(id -u)" = "0" ]; then chown postgres:postgres "$PGDATA"; fi
    run_as_pg "$PGBIN/initdb -D $PGDATA -U dro --auth=trust -E UTF8" >/dev/null
  fi
  run_as_pg "$PGBIN/pg_ctl -D $PGDATA -o '-p $PGPORT -c listen_addresses=127.0.0.1 -c unix_socket_directories=$PGDATA' -l $PGDATA/log start" || true
  sleep 1
  run_as_pg "$PGBIN/psql -h 127.0.0.1 -p $PGPORT -U dro -d postgres -tc \"SELECT 1 FROM pg_database WHERE datname='dro'\" | grep -q 1" \
    || run_as_pg "$PGBIN/createdb -h 127.0.0.1 -p $PGPORT -U dro dro"
  redis-cli -p "$REDIS_PORT" ping >/dev/null 2>&1 \
    || redis-server --port "$REDIS_PORT" --daemonize yes --save '' --appendonly no
  echo "PostgreSQL: 127.0.0.1:$PGPORT (db=dro user=dro), Redis: 127.0.0.1:$REDIS_PORT"
}

stop() {
  run_as_pg "$PGBIN/pg_ctl -D $PGDATA stop -m fast" || true
  redis-cli -p "$REDIS_PORT" shutdown nosave 2>/dev/null || true
}

status() {
  run_as_pg "$PGBIN/pg_ctl -D $PGDATA status" || true
  redis-cli -p "$REDIS_PORT" ping 2>/dev/null || echo "redis: not running"
}

case "${1:-start}" in
  start) start ;;
  stop) stop ;;
  status) status ;;
  *) echo "usage: $0 start|stop|status" >&2; exit 1 ;;
esac
