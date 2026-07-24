"""pytest共通設定。

統合テストはローカルのPostgreSQL/Redis実プロセスを使う (Docker不要)。
scripts/dev_infra.sh start で起動できる。到達できない場合、統合テストは
明確な理由付きでskipされる — skipは「未検証」であり成功ではない。
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

# app import前に環境変数を確定させる (Settingsはlru_cache)
TEST_PG = os.environ.get("DRO_TEST_PG", "postgresql+psycopg://dro@127.0.0.1:55432")
TEST_REDIS = os.environ.get("DRO_TEST_REDIS", "redis://127.0.0.1:56379")
_dbname = f"dro_test_{uuid.uuid4().hex[:8]}"
_redis_db = os.environ.get("DRO_TEST_REDIS_DB", "5")

_data_dir = Path(os.environ.get("DRO_TEST_DATA_DIR", f"/var/tmp/dro-test-{_dbname}"))

os.environ.setdefault("DRO_DATABASE_URL", f"{TEST_PG}/{_dbname}")
os.environ.setdefault("DRO_REDIS_URL", f"{TEST_REDIS}/{_redis_db}")
os.environ.setdefault("DRO_DATA_DIR", str(_data_dir))
os.environ.setdefault("DRO_MASTER_KEY", "test-master-key-for-tests-only")
os.environ.setdefault("DRO_MASTER_KEY_FILE", "/nonexistent")
os.environ.setdefault("DRO_RUN_POLL_INTERVAL_SECONDS", "0.15")
os.environ.setdefault("DRO_STUCK_RUN_HEARTBEAT_SECONDS", "6")
os.environ.setdefault("DRO_RETRY_BACKOFF_BASE_SECONDS", "0.2")
os.environ.setdefault("DRO_RETRY_BACKOFF_MAX_SECONDS", "1.0")
os.environ.setdefault("DRO_RUN_DEFAULT_TIMEOUT_SECONDS", "60")
os.environ.setdefault("DRO_SEARCH_PROVIDER", "searxng")
os.environ.setdefault("DRO_SEARXNG_ENDPOINT", "http://127.0.0.1:1/unused")
os.environ.setdefault("DRO_LOG_LEVEL", "WARNING")
os.environ.setdefault("DRO_API_RATE_LIMIT_PER_MINUTE", "100000")

import pytest  # noqa: E402

BACKEND_DIR = Path(__file__).resolve().parent.parent
PROJECT_DIR = BACKEND_DIR.parent


def _pg_available() -> bool:
    try:
        import psycopg

        with psycopg.connect(
            TEST_PG.replace("postgresql+psycopg", "postgresql") + "/postgres",
            connect_timeout=3,
        ):
            return True
    except Exception:
        return False


def _redis_available() -> bool:
    try:
        import redis as redis_lib

        redis_lib.Redis.from_url(f"{TEST_REDIS}/{_redis_db}").ping()
        return True
    except Exception:
        return False


PG_OK = _pg_available()
REDIS_OK = _redis_available()

requires_infra = pytest.mark.skipif(
    not (PG_OK and REDIS_OK),
    reason="ローカルPostgreSQL/Redisが起動していません (scripts/dev_infra.sh start)。"
    "このテストは未検証です — skipは成功ではありません。",
)


@pytest.fixture(scope="session")
def test_db_url() -> str:
    """テスト用DBを作成しmigrationを適用、終了時に破棄する。"""
    if not PG_OK:
        pytest.skip("PostgreSQL未起動")
    import psycopg

    admin_dsn = TEST_PG.replace("postgresql+psycopg", "postgresql") + "/postgres"
    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{_dbname}"')
    url = f"{TEST_PG}/{_dbname}"
    env = dict(os.environ, DRO_DATABASE_URL=url)
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=BACKEND_DIR, env=env, check=True, capture_output=True,
    )
    yield url
    from app.db.session import reset_engine

    reset_engine()
    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{_dbname}" WITH (FORCE)')


@pytest.fixture(scope="session")
def data_dir() -> Path:
    _data_dir.mkdir(parents=True, exist_ok=True)
    return _data_dir


@pytest.fixture(scope="session")
def mock_runner(test_db_url):
    """Mock Runnerをuvicorn subprocessで起動する。"""
    from tests.fixtures.servers import free_port

    port = free_port()
    env = dict(os.environ)
    env["PYTHONPATH"] = (
        f"{PROJECT_DIR / 'runners' / 'common'}:{PROJECT_DIR / 'runners' / 'mock'}"
    )
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "main:app", "--host", "127.0.0.1",
         "--port", str(port), "--log-level", "warning"],
        cwd=PROJECT_DIR / "runners" / "mock",
        env=env,
    )
    url = f"http://127.0.0.1:{port}"
    import httpx

    for _ in range(100):
        try:
            if httpx.get(f"{url}/healthz", timeout=1.0).status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.1)
    else:
        proc.terminate()
        raise RuntimeError("mock runnerが起動しません")
    os.environ["DRO_RUNNER_MOCK_URL"] = url
    yield url
    proc.terminate()
    proc.wait(timeout=10)


class WorkerHandle:
    """Celery worker subprocessの制御 (再起動テスト用)。"""

    def __init__(self, env: dict[str, str]):
        self.env = env
        self.proc: subprocess.Popen | None = None

    def start(self) -> None:
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "celery", "-A", "app.orchestrator.celery_app",
             "worker", "--loglevel", "warning", "--concurrency", "8",
             "--pool", "threads"],
            cwd=BACKEND_DIR,
            env=self.env,
        )

    def kill(self) -> None:
        if self.proc is not None:
            self.proc.kill()
            self.proc.wait(timeout=15)
            self.proc = None

    def stop(self) -> None:
        if self.proc is not None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=10)
            self.proc = None


@pytest.fixture(scope="session")
def celery_worker_proc(test_db_url, mock_runner, data_dir):
    if not REDIS_OK:
        pytest.skip("Redis未起動")
    # broker/backend DBを空にしてから起動
    import redis as redis_lib

    redis_lib.Redis.from_url(os.environ["DRO_REDIS_URL"]).flushdb()
    env = dict(os.environ)
    env["DRO_DATABASE_URL"] = test_db_url
    env["DRO_RUNNER_MOCK_URL"] = mock_runner
    handle = WorkerHandle(env)
    handle.start()
    time.sleep(2.0)
    yield handle
    handle.stop()


@pytest.fixture(scope="session")
def api_client(test_db_url, mock_runner):
    """in-process TestClient。lifespanでbootstrapが走る。"""
    from fastapi.testclient import TestClient

    from app.config import get_settings
    from app.db.session import reset_engine

    get_settings.cache_clear()
    reset_engine()
    from app.main import app

    with TestClient(app) as client:
        yield client


@pytest.fixture()
def db_session(test_db_url):
    from app.db.session import session_scope

    with session_scope() as session:
        yield session
