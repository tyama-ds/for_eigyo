"""Artifact download API — filesystem pathを公開せず、artifact IDで認可済み取得。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.orm import Session

from app.api.deps import db_session, settings_dep
from app.artifacts.store import ArtifactError, ArtifactStore
from app.config import Settings

router = APIRouter(prefix="/api", tags=["artifacts"])


@router.get("/artifacts/{artifact_id}")
def download_artifact(
    artifact_id: str,
    session: Session = Depends(db_session),
    settings: Settings = Depends(settings_dep),
) -> Response:
    store = ArtifactStore(session, settings)
    try:
        artifact, content = store.load(artifact_id)
    except KeyError as e:
        raise HTTPException(status_code=404, detail="artifact not found") from e
    except ArtifactError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    # 実行可能コンテンツとしての解釈を防ぐ
    safe_mime = artifact.mime
    if safe_mime in ("text/html", "application/xhtml+xml", "image/svg+xml"):
        safe_mime = "text/plain; charset=utf-8"
    return Response(
        content=content,
        media_type=safe_mime,
        headers={
            "Content-Disposition": f'attachment; filename="artifact-{artifact.id}"',
            "X-Content-Type-Options": "nosniff",
            "X-Artifact-Sha256": artifact.sha256,
        },
    )
