"""SSE再送・再起動回復・artifact・backup/restore — 受入 4,5,15,16。"""

from __future__ import annotations

import json
import os
import subprocess
import time

import pytest

from tests.conftest import BACKEND_DIR, requires_infra
from tests.integration.helpers import (
    FAST,
    create_job,
    event_history,
    get_job,
    wait_for_job,
    wait_for_run_status,
)

pytestmark = requires_infra


@pytest.fixture(autouse=True)
def _stack(api_client, celery_worker_proc):
    yield


class TestSseReplay:
    def test_full_replay_from_zero_and_resume_from_last_event_id(self, api_client):
        """受入4: reload/SSE再接続で状態とイベントが復元される。"""
        job = create_job(api_client, ["mock-fast"])
        wait_for_job(api_client, job["id"])

        # 全履歴の再取得 (reload相当)
        events = event_history(api_client, job["id"], after=0)
        seqs = [e["seq"] for e in events]
        assert seqs == sorted(seqs)
        assert seqs[0] == 1 and len(set(seqs)) == len(seqs)  # 連番・重複なし
        types = {e["type"] for e in events}
        assert {"job_created", "job_status", "run_status"} <= types

        # SSE: Last-Event-ID相当の途中からの再送
        mid = seqs[len(seqs) // 2]
        with api_client.stream(
            "GET", f"/api/jobs/{job['id']}/events",
            headers={"Last-Event-ID": str(mid)},
        ) as resp:
            assert resp.status_code == 200
            received: list[int] = []
            buf_id = None
            for line in resp.iter_lines():
                if line.startswith("id:"):
                    buf_id = int(line.split(":", 1)[1].strip())
                elif line.startswith("data:") and buf_id is not None:
                    received.append(buf_id)
                    buf_id = None
                elif line.startswith("event: stream_end"):
                    break
                if len(received) >= len([s for s in seqs if s > mid]):
                    break
            assert received and received[0] == mid + 1

    def test_sse_stream_during_run(self, api_client):
        job = create_job(api_client, ["mock-fast"])
        got_stage = False
        with api_client.stream("GET", f"/api/jobs/{job['id']}/events") as resp:
            start = time.monotonic()
            for line in resp.iter_lines():
                if line.startswith("event: engine_stage"):
                    got_stage = True
                    break
                if time.monotonic() - start > 30:
                    break
        assert got_stage


class TestRestartRecovery:
    def test_worker_restart_recovers_run(self, api_client, celery_worker_proc):
        """受入5: worker再起動後も状態を失わず、runが完走する。"""
        job = create_job(
            api_client, ["mock-slow"],
            engine_options={"mock-slow": {"speed_factor": 3.0, "seed": 42}},
        )
        wait_for_run_status(api_client, job["id"], "mock-slow", ("researching",),
                            timeout=30)
        # workerを強制killして再起動 (実行中タスクは失われる)
        celery_worker_proc.kill()
        time.sleep(1.0)
        celery_worker_proc.start()
        time.sleep(1.0)
        # reconcile相当を明示実行 (beatはテストでは動かさない)
        from app.orchestrator.tasks import reconcile_stuck_runs

        deadline = time.monotonic() + 120
        final = None
        while time.monotonic() < deadline:
            job_state = get_job(api_client, job["id"])
            if job_state["status"] in ("completed", "partial", "failed", "cancelled"):
                final = job_state
                break
            # heartbeat途絶 (6s設定) を待ってreconcileを叩く
            reconcile_stuck_runs.delay()
            time.sleep(3.0)
        assert final is not None, "worker再起動後にjobが終了しません"
        assert final["status"] == "completed", final
        # イベント履歴にreconcileの形跡
        events = event_history(api_client, job["id"])
        assert any(e["type"] == "reconcile" for e in events)

    def test_api_restart_state_from_postgres(self, api_client, test_db_url):
        """API再起動相当: 新しいアプリインスタンスから状態が読める (正本はPG)。"""
        job = create_job(api_client, ["mock-fast"])
        final = wait_for_job(api_client, job["id"])
        from fastapi.testclient import TestClient

        from app.main import create_app

        with TestClient(create_app()) as fresh_client:
            resp = fresh_client.get(f"/api/jobs/{job['id']}")
            assert resp.status_code == 200
            assert resp.json()["status"] == final["status"]
            events = fresh_client.get(
                f"/api/jobs/{job['id']}/events/history", params={"after": 0}
            ).json()
            assert events


class TestArtifacts:
    def test_artifact_download_and_traversal_rejected(self, api_client):
        """受入15: artifact取得、path traversal/symlink拒否 (単体テストと併用)。"""
        job = create_job(api_client, ["mock-fast"])
        wait_for_job(api_client, job["id"])
        results = api_client.get(f"/api/jobs/{job['id']}/results").json()
        raw_id = results[0]["raw_artifact_id"]
        assert raw_id
        resp = api_client.get(f"/api/artifacts/{raw_id}")
        assert resp.status_code == 200
        raw = json.loads(resp.content)
        assert raw["raw"]["deterministic"] is True
        assert resp.headers["X-Artifact-Sha256"]
        # 存在しない/不正ID
        assert api_client.get("/api/artifacts/../etc/passwd").status_code in (404, 422)
        assert api_client.get("/api/artifacts/nonexistent-id").status_code == 404


class TestBackupRestore:
    def test_pg_and_datadir_backup_restore(self, api_client, test_db_url, data_dir,
                                            tmp_path):
        """受入16: PostgreSQLとDATA_DIRのbackup/restore後にjob/event/result/artifact
        の対応が復元される。"""
        job = create_job(api_client, ["mock-fast", "mock-slow"])
        wait_for_job(api_client, job["id"])
        results = api_client.get(f"/api/jobs/{job['id']}/results").json()
        raw_ids = [r["raw_artifact_id"] for r in results if r["raw_artifact_id"]]
        assert raw_ids

        pg_bin = os.environ.get("PGBIN", "/usr/lib/postgresql/16/bin")
        dsn = test_db_url.replace("postgresql+psycopg", "postgresql")
        dump_file = tmp_path / "backup.dump"
        subprocess.run(
            [f"{pg_bin}/pg_dump", "-Fc", "-f", str(dump_file), dsn],
            check=True, capture_output=True,
        )
        # DATA_DIRのtar backup
        tar_file = tmp_path / "data.tar"
        subprocess.run(["tar", "-cf", str(tar_file), "-C", str(data_dir), "."],
                       check=True, capture_output=True)

        # restore先DBを作成
        import uuid as _uuid

        restore_db = f"dro_restore_{_uuid.uuid4().hex[:8]}"
        admin = dsn.rsplit("/", 1)[0] + "/postgres"
        import psycopg

        with psycopg.connect(admin, autocommit=True) as conn:
            conn.execute(f'CREATE DATABASE "{restore_db}"')
        restore_dsn = dsn.rsplit("/", 1)[0] + f"/{restore_db}"
        restore_data = tmp_path / "restored-data"
        restore_data.mkdir()
        try:
            subprocess.run(
                [f"{pg_bin}/pg_restore", "--no-owner", "-d", restore_dsn, str(dump_file)],
                check=True, capture_output=True,
            )
            subprocess.run(["tar", "-xf", str(tar_file), "-C", str(restore_data)],
                           check=True, capture_output=True)

            # 復元環境で対応関係を検証 (別プロセス相当: 新しいengine/settings)
            env = dict(os.environ)
            env["DRO_DATABASE_URL"] = restore_dsn.replace("postgresql://",
                                                          "postgresql+psycopg://")
            env["DRO_DATA_DIR"] = str(restore_data)
            code = (
                "import json;"
                "from app.config import get_settings;"
                "from app.db.session import session_scope;"
                "from app.artifacts.store import ArtifactStore;"
                f"job_id={job['id']!r}; raw_ids={raw_ids!r};"
                "from sqlalchemy import select;"
                "from app.db.models import ResearchJob, JobEvent, NormalizedResult;"
                "s=get_settings();"
                "exec('''\n"
                "with session_scope() as session:\n"
                "    job = session.get(ResearchJob, job_id)\n"
                "    assert job is not None and job.status == 'completed'\n"
                "    events = session.scalars(select(JobEvent).where("
                "JobEvent.job_id==job_id)).all()\n"
                "    assert events\n"
                "    store = ArtifactStore(session, s)\n"
                "    for rid in raw_ids:\n"
                "        artifact, content = store.load(rid)\n"
                "        data = json.loads(content)\n"
                "        assert data['raw']['deterministic'] is True\n"
                "    print('RESTORE_OK')\n"
                "''')"
            )
            proc = subprocess.run(
                ["python", "-c", code], env=env, cwd=BACKEND_DIR,
                capture_output=True, text=True,
            )
            assert "RESTORE_OK" in proc.stdout, proc.stderr
        finally:
            with psycopg.connect(admin, autocommit=True) as conn:
                conn.execute(f'DROP DATABASE IF EXISTS "{restore_db}" WITH (FORCE)')
