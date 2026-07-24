"""ローカルArtifact Store。

- 小さいコンテンツ (<= inline_max_bytes) はPostgreSQLへinline保存
- 大きいコンテンツは DATA_DIR/artifacts/{job_id}/{run_id}/{artifact_id} へ保存
- temp file + fsync + atomic rename で書き込み、途中fileを完成artifactにしない
- path traversal / symlink / quota超過を拒否
- APIへfilesystem pathを出さない (artifact idのみ)
"""

from __future__ import annotations

import hashlib
import os
import tempfile
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.db.models import Artifact


class ArtifactError(RuntimeError):
    pass


class QuotaExceededError(ArtifactError):
    pass


_SAFE_SEGMENT = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ-_"


def _check_segment(segment: str) -> str:
    if not segment or any(c not in _SAFE_SEGMENT for c in segment):
        raise ArtifactError(f"不正なpath segment: {segment!r}")
    return segment


class ArtifactStore:
    def __init__(self, session: Session, settings: Settings):
        self._session = session
        self._settings = settings
        self._root = settings.data_dir / "artifacts"

    # ---------- 書き込み ----------

    def save(
        self,
        *,
        content: bytes,
        kind: str,
        mime: str,
        job_id: str | None = None,
        run_id: str | None = None,
        retention_days: int | None = None,
    ) -> Artifact:
        if len(content) > self._settings.artifact_max_single_bytes:
            raise ArtifactError(
                f"artifactサイズ上限超過: {len(content)} > "
                f"{self._settings.artifact_max_single_bytes}"
            )
        artifact_id = str(uuid.uuid4())
        sha256 = hashlib.sha256(content).hexdigest()
        days = retention_days or self._settings.artifact_retention_days
        expires_at = datetime.now(UTC) + timedelta(days=days)

        artifact = Artifact(
            id=artifact_id,
            job_id=job_id,
            run_id=run_id,
            kind=kind,
            mime=mime,
            size=len(content),
            sha256=sha256,
            expires_at=expires_at,
        )

        if len(content) <= self._settings.artifact_inline_max_bytes:
            artifact.content_inline = content
            artifact.relative_path = None
        else:
            self._check_quota(len(content))
            rel = self._relative_path(job_id, run_id, artifact_id)
            final_path = self._root / rel
            final_path.parent.mkdir(parents=True, exist_ok=True)
            # temp file → fsync → atomic rename
            fd, tmp_name = tempfile.mkstemp(dir=final_path.parent, prefix=".tmp-")
            try:
                with os.fdopen(fd, "wb") as f:
                    f.write(content)
                    f.flush()
                    os.fsync(f.fileno())
                os.rename(tmp_name, final_path)
                dir_fd = os.open(final_path.parent, os.O_RDONLY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
            except BaseException:
                if os.path.exists(tmp_name):
                    os.unlink(tmp_name)
                raise
            artifact.relative_path = str(rel)

        self._session.add(artifact)
        self._session.flush()
        return artifact

    def _relative_path(self, job_id: str | None, run_id: str | None, artifact_id: str) -> Path:
        job_seg = _check_segment(job_id or "shared")
        run_seg = _check_segment(run_id or "job")
        art_seg = _check_segment(artifact_id)
        return Path(job_seg) / run_seg / art_seg

    def _check_quota(self, incoming: int) -> None:
        used = self._session.scalar(
            select(func.coalesce(func.sum(Artifact.size), 0)).where(
                Artifact.relative_path.is_not(None)
            )
        )
        if (used or 0) + incoming > self._settings.artifact_quota_bytes:
            raise QuotaExceededError(
                f"artifact quota超過: 使用中 {used} + 追加 {incoming} > "
                f"{self._settings.artifact_quota_bytes}"
            )

    # ---------- 読み出し ----------

    def load(self, artifact_id: str, *, verify_integrity: bool = True) -> tuple[Artifact, bytes]:
        artifact = self._session.get(Artifact, artifact_id)
        if artifact is None:
            raise KeyError(f"artifact {artifact_id} not found")
        if artifact.content_inline is not None:
            content = bytes(artifact.content_inline)
        else:
            content = self._read_file(artifact)
        if verify_integrity:
            actual = hashlib.sha256(content).hexdigest()
            if actual != artifact.sha256:
                raise ArtifactError(
                    f"artifact整合性エラー: sha256不一致 (id={artifact_id})"
                )
        return artifact, content

    def _read_file(self, artifact: Artifact) -> bytes:
        rel = Path(artifact.relative_path or "")
        # path traversal防御: 各segmentを再検証し、resolve後もroot配下であることを確認
        for part in rel.parts:
            _check_segment(part)
        path = (self._root / rel).resolve()
        root_resolved = self._root.resolve()
        if not path.is_relative_to(root_resolved):
            raise ArtifactError("path traversalを拒否しました")
        # symlink拒否 (中間ディレクトリ含む)
        current = path
        while current != root_resolved:
            if current.is_symlink():
                raise ArtifactError("symlink経由のアクセスを拒否しました")
            current = current.parent
        if not path.is_file():
            raise ArtifactError(f"artifact fileが見つかりません: id={artifact.id}")
        return path.read_bytes()

    # ---------- retention ----------

    def cleanup_expired(self, now: datetime | None = None) -> int:
        now = now or datetime.now(UTC)
        expired = list(
            self._session.scalars(select(Artifact).where(Artifact.expires_at < now))
        )
        count = 0
        for artifact in expired:
            if artifact.relative_path:
                path = self._root / artifact.relative_path
                try:
                    if path.is_file() and not path.is_symlink():
                        path.unlink()
                except OSError:
                    pass
            self._session.delete(artifact)
            count += 1
        self._session.flush()
        return count
