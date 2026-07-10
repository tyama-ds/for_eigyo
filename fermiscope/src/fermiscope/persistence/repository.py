"""ProjectRepository — プロジェクト状態の保存と読み込み。

方針(DECISIONS D-011):
- プロジェクト全状態は Pydantic ダンプの JSON を `projects.state_json` に保存
  (単一の真実源、PostgreSQL JSONB への移行が容易)
- 監査イベント・証拠・検索は照会性のため正規化テーブルにも展開
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    DateTime,
    Float,
    ForeignKey,
    String,
    Text,
    create_engine,
    select,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from fermiscope.domain.models import EstimateProject


class Base(DeclarativeBase):
    pass


class ProjectRow(Base):
    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    name: Mapped[str] = mapped_column(String(300), default="")
    question: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    state_json: Mapped[dict] = mapped_column(JSON)


class AuditEventRow(Base):
    __tablename__ = "audit_events"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    category: Mapped[str] = mapped_column(String(60))
    message: Mapped[str] = mapped_column(Text)
    data: Mapped[dict] = mapped_column(JSON, default=dict)


class EvidenceRow(Base):
    __tablename__ = "evidence_items"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    parameter_id: Mapped[str] = mapped_column(String(120), index=True)
    url: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text, default="")
    source_class: Mapped[str] = mapped_column(String(10), default="unknown")
    evidence_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    publication_date: Mapped[str] = mapped_column(String(40), default="")
    retrieval_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    content_hash: Mapped[str] = mapped_column(String(64), default="")


class SearchQueryRow(Base):
    __tablename__ = "search_queries"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    parameter_id: Mapped[str] = mapped_column(String(120), default="")
    query: Mapped[str] = mapped_column(Text)
    purpose: Mapped[str] = mapped_column(String(40), default="")
    provider: Mapped[str] = mapped_column(String(40), default="")
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    results_count: Mapped[float] = mapped_column(Float, default=0)


class ProjectRepository:
    def __init__(self, database_url: str) -> None:
        connect_args = {"check_same_thread": False} if database_url.startswith("sqlite") else {}
        self.engine = create_engine(database_url, connect_args=connect_args, future=True)
        Base.metadata.create_all(self.engine)

    def save(self, project: EstimateProject) -> None:
        project.updated_at = datetime.now(UTC)
        state = project.model_dump(mode="json")
        with Session(self.engine) as session:
            row = session.get(ProjectRow, project.id)
            if row is None:
                row = ProjectRow(
                    id=project.id,
                    created_at=project.created_at,
                )
                session.add(row)
            row.name = project.name
            row.question = project.question.original_question
            row.updated_at = project.updated_at
            row.state_json = state

            # 正規化テーブルは全消し・再挿入(規模が小さいため単純さを優先)
            for table, rows in self._normalized_rows(project):
                session.query(table).filter_by(project_id=project.id).delete()
                session.add_all(rows)
            session.commit()

    def _normalized_rows(self, project: EstimateProject) -> list[tuple[type, list]]:
        audit_rows = [
            AuditEventRow(
                id=ev.id,
                project_id=project.id,
                timestamp=ev.timestamp,
                category=ev.category,
                message=ev.message,
                data=ev.data,
            )
            for ev in project.audit_events
        ]
        evidence_rows = [
            EvidenceRow(
                id=e.id,
                project_id=project.id,
                parameter_id=e.parameter_id,
                url=e.url,
                title=e.title,
                source_class=e.source_class.value,
                evidence_score=e.evidence_score,
                publication_date=e.publication_date,
                retrieval_date=e.retrieval_date,
                content_hash=e.content_hash,
            )
            for e in project.evidence.values()
        ]
        search_rows = [
            SearchQueryRow(
                id=q.id,
                project_id=project.id,
                parameter_id=q.parameter_id,
                query=q.query,
                purpose=q.purpose.value,
                provider=q.provider,
                executed_at=q.executed_at,
                results_count=q.results_count,
            )
            for q in project.searches
        ]
        return [
            (AuditEventRow, audit_rows),
            (EvidenceRow, evidence_rows),
            (SearchQueryRow, search_rows),
        ]

    def load(self, project_id: str) -> EstimateProject | None:
        with Session(self.engine) as session:
            row = session.get(ProjectRow, project_id)
            if row is None:
                return None
            return EstimateProject.model_validate(row.state_json)

    def list_projects(self, limit: int = 100) -> list[dict]:
        with Session(self.engine) as session:
            rows = session.execute(
                select(ProjectRow).order_by(ProjectRow.updated_at.desc()).limit(limit)
            ).scalars()
            return [
                {
                    "id": r.id,
                    "name": r.name,
                    "question": r.question,
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                    "updated_at": r.updated_at.isoformat() if r.updated_at else None,
                }
                for r in rows
            ]

    def delete(self, project_id: str) -> bool:
        with Session(self.engine) as session:
            row = session.get(ProjectRow, project_id)
            if row is None:
                return False
            for table in (AuditEventRow, EvidenceRow, SearchQueryRow):
                session.query(table).filter_by(project_id=project_id).delete()
            session.delete(row)
            session.commit()
            return True
