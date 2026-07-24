"""Artifact StoreとSecret Storeの単体テスト (実DB使用)。"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.artifacts.store import ArtifactError, ArtifactStore, QuotaExceededError
from app.config import get_settings
from app.db.models import Artifact, SecretItem
from app.security.secrets import SecretStore

from tests.conftest import requires_infra

pytestmark = requires_infra


@pytest.fixture()
def store(db_session, data_dir):
    return ArtifactStore(db_session, get_settings())


class TestArtifactStore:
    def test_small_content_inline_in_db(self, store, db_session):
        artifact = store.save(content=b"small", kind="raw_result", mime="application/json",
                              job_id="j" * 8, run_id="r" * 8)
        assert artifact.relative_path is None
        loaded, content = store.load(artifact.id)
        assert content == b"small"
        assert loaded.sha256 == artifact.sha256

    def test_large_content_on_filesystem_atomic(self, store, data_dir):
        big = os.urandom(300_000)
        artifact = store.save(content=big, kind="snapshot", mime="application/octet-stream",
                              job_id="job1", run_id="run1")
        assert artifact.relative_path is not None
        path = data_dir / "artifacts" / artifact.relative_path
        assert path.is_file()
        # 途中fileが残っていない
        assert not list(path.parent.glob(".tmp-*"))
        _, content = store.load(artifact.id)
        assert content == big

    def test_path_traversal_rejected(self, store, db_session):
        artifact = store.save(content=os.urandom(300_000), kind="snapshot",
                              mime="application/octet-stream", job_id="job2", run_id="run2")
        row = db_session.get(Artifact, artifact.id)
        row.relative_path = "../../etc/passwd"
        db_session.flush()
        with pytest.raises(ArtifactError):
            store.load(artifact.id)

    def test_symlink_rejected(self, store, db_session, data_dir):
        artifact = store.save(content=os.urandom(300_000), kind="snapshot",
                              mime="application/octet-stream", job_id="job3", run_id="run3")
        real_path = data_dir / "artifacts" / artifact.relative_path
        link_path = real_path.with_name(real_path.name + "lnk")
        os.symlink("/etc/passwd", link_path)
        row = db_session.get(Artifact, artifact.id)
        row.relative_path = str(
            (link_path.relative_to(data_dir / "artifacts"))
        )
        db_session.flush()
        with pytest.raises(ArtifactError):
            store.load(artifact.id)

    def test_integrity_check_detects_tamper(self, store, db_session, data_dir):
        artifact = store.save(content=os.urandom(300_000), kind="snapshot",
                              mime="application/octet-stream", job_id="job4", run_id="run4")
        path = data_dir / "artifacts" / artifact.relative_path
        path.write_bytes(b"tampered")
        with pytest.raises(ArtifactError, match="sha256"):
            store.load(artifact.id)

    def test_quota_exceeded(self, db_session, data_dir):
        from sqlalchemy import func, select

        used = db_session.scalar(
            select(func.coalesce(func.sum(Artifact.size), 0)).where(
                Artifact.relative_path.is_not(None)
            )
        ) or 0
        settings = get_settings().model_copy(
            update={"artifact_quota_bytes": used + 400_000}
        )
        store = ArtifactStore(db_session, settings)
        store.save(content=os.urandom(300_000), kind="snapshot",
                   mime="application/octet-stream", job_id="job5", run_id="run5")
        with pytest.raises(QuotaExceededError):
            store.save(content=os.urandom(300_000), kind="snapshot",
                       mime="application/octet-stream", job_id="job5", run_id="run5b")

    def test_retention_cleanup(self, store, db_session, data_dir):
        artifact = store.save(content=os.urandom(300_000), kind="log",
                              mime="text/plain", job_id="job6", run_id="run6")
        row = db_session.get(Artifact, artifact.id)
        row.expires_at = datetime.now(UTC) - timedelta(days=1)
        db_session.flush()
        removed = store.cleanup_expired()
        assert removed >= 1
        assert not (data_dir / "artifacts" / artifact.relative_path).exists()

    def test_invalid_segment_rejected(self, store):
        with pytest.raises(ArtifactError):
            store.save(content=os.urandom(300_000), kind="snapshot",
                       mime="application/octet-stream", job_id="../evil", run_id="run7")


class TestSecretStore:
    def test_roundtrip_and_ciphertext_at_rest(self, db_session):
        store = SecretStore(db_session, get_settings())
        secret_id = store.put("test:apikey", "sk-plaintext-value-123")
        row = db_session.get(SecretItem, secret_id)
        assert b"sk-plaintext-value-123" not in row.ciphertext
        assert store.reveal(secret_id) == "sk-plaintext-value-123"

    def test_overwrite_same_name(self, db_session):
        store = SecretStore(db_session, get_settings())
        id1 = store.put("test:rotate", "old-value-000000")
        id2 = store.put("test:rotate", "new-value-111111")
        assert id1 == id2
        assert store.reveal(id2) == "new-value-111111"

    def test_no_plaintext_anywhere_in_db(self, db_session):
        store = SecretStore(db_session, get_settings())
        store.put("test:leakcheck", "super-unique-plaintext-9x7z")
        rows = db_session.scalars(select(SecretItem)).all()
        for row in rows:
            assert b"super-unique-plaintext-9x7z" not in row.ciphertext
