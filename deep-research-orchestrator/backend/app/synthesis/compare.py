"""Comparison Engine — 決定論的な結果比較。

正規化済みclaims/sources/evidenceのみを入力とし、LLMを使わない。
- 全Runner一致の発見 / 一部Runnerのみの発見
- 矛盾する主張 (多数決で隠さない — 全立場を保持)
- 根拠不足の主張 (evidenceゼロ)
- 調査範囲の差 (sourceの重なり)
"""

from __future__ import annotations

from collections import defaultdict
from difflib import SequenceMatcher
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Claim, EngineRun, Evidence, RunStatus, Source

SIMILARITY_THRESHOLD = 0.75


def _cluster_key(claim: Claim) -> str | None:
    return claim.meta.get("key") if isinstance(claim.meta, dict) else None


def _similar(a: str, b: str) -> bool:
    return SequenceMatcher(None, a, b).ratio() >= SIMILARITY_THRESHOLD


def _claim_dict(claim: Claim, engine_id: str, evidence_count: int) -> dict[str, Any]:
    return {
        "claim_id": claim.id,
        "run_id": claim.run_id,
        "engine_id": engine_id,
        "text": claim.text,
        "value": claim.meta.get("value") if isinstance(claim.meta, dict) else None,
        "evidence_count": evidence_count,
    }


def compare_job(session: Session, job_id: str) -> dict[str, Any]:
    """job内の成功したrun群のclaimsを比較する。"""
    runs = list(
        session.scalars(
            select(EngineRun).where(
                EngineRun.job_id == job_id,
                EngineRun.status == RunStatus.succeeded.value,
            )
        )
    )
    run_engine = {r.id: r.engine_id for r in runs}
    succeeded_engines = sorted(run_engine.values())

    claims = list(session.scalars(select(Claim).where(Claim.job_id == job_id)))
    claims = [c for c in claims if c.run_id in run_engine]
    evidence_counts: dict[str, int] = defaultdict(int)
    if claims:
        rows = session.execute(
            select(Evidence.claim_id).where(Evidence.claim_id.in_([c.id for c in claims]))
        )
        for (claim_id,) in rows:
            evidence_counts[claim_id] += 1

    # --- クラスタリング: 明示key優先、なければ正規化テキスト類似 ---
    clusters: list[dict[str, Any]] = []
    key_clusters: dict[str, int] = {}
    for claim in sorted(claims, key=lambda c: (c.run_id, c.position)):
        key = _cluster_key(claim)
        assigned = None
        if key is not None:
            if key in key_clusters:
                assigned = key_clusters[key]
        else:
            for idx, cluster in enumerate(clusters):
                if cluster["key"] is not None:
                    continue
                if any(
                    _similar(claim.normalized_text, m["_normalized"]) for m in cluster["members"]
                ):
                    assigned = idx
                    break
        if assigned is None:
            clusters.append({"key": key, "members": []})
            assigned = len(clusters) - 1
            if key is not None:
                key_clusters[key] = assigned
        member = _claim_dict(claim, run_engine[claim.run_id], evidence_counts[claim.id])
        member["_normalized"] = claim.normalized_text
        clusters[assigned]["members"].append(member)

    # --- 分類 ---
    agreements: list[dict[str, Any]] = []
    partial_findings: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    unsupported: list[dict[str, Any]] = []

    for cluster in clusters:
        members = cluster["members"]
        for m in members:
            m.pop("_normalized", None)
        engines = sorted({m["engine_id"] for m in members})
        values = {m["value"] for m in members if m["value"] is not None}
        has_conflict = len(values) > 1
        no_evidence = [m for m in members if m["evidence_count"] == 0]

        for m in no_evidence:
            unsupported.append(m)

        entry = {
            "key": cluster["key"],
            "engines": engines,
            "claims": members,
        }
        if has_conflict:
            # 矛盾は多数決で隠さない — 全claimと全valueを保持
            entry["values"] = sorted(values)
            conflicts.append(entry)
        elif len(engines) == len(succeeded_engines) and len(succeeded_engines) > 1:
            agreements.append(entry)
        else:
            partial_findings.append(entry)

    # --- 調査範囲の差 (source重なり) ---
    sources = list(session.scalars(select(Source).where(Source.job_id == job_id)))
    sources = [s for s in sources if s.run_id in run_engine]
    by_canonical: dict[str, set[str]] = defaultdict(set)
    for s in sources:
        by_canonical[s.canonical_url].add(run_engine[s.run_id])
    coverage = {
        "total_unique_sources": len(by_canonical),
        "shared_by_all": sorted(
            url for url, engines in by_canonical.items()
            if len(engines) == len(succeeded_engines) and len(succeeded_engines) > 1
        ),
        "engine_unique": {
            engine: sorted(
                url for url, engines in by_canonical.items() if engines == {engine}
            )
            for engine in succeeded_engines
        },
        "sources_per_engine": {
            engine: sum(1 for s in sources if run_engine[s.run_id] == engine)
            for engine in succeeded_engines
        },
    }

    open_questions = [
        f"矛盾が未解決: {c['key'] or c['claims'][0]['text'][:60]} "
        f"(値: {' / '.join(str(v) for v in c.get('values', []))})"
        for c in conflicts
    ]
    open_questions.extend(
        f"根拠不足の主張: {u['text'][:80]} ({u['engine_id']})" for u in unsupported
    )

    return {
        "engines_compared": succeeded_engines,
        "agreements": agreements,
        "partial_findings": partial_findings,
        "conflicts": conflicts,
        "unsupported_claims": unsupported,
        "coverage": coverage,
        "open_questions": open_questions,
    }
