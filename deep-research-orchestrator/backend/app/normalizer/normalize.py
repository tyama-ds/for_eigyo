"""Result Normalizer — Runner生出力を共通結果形式へ正規化する。

- 取得できない値は推測せずnullとし、warningへ記録する
- original URLとcanonical URLを両方保持する
- URL重複排除はcanonical URL単位で行うが、run単位のprovenance (どのRunnerが
  どのURLを見たか) は sources 行として失わない
- 生出力はraw artifactとして保存済みで、Normalizer更新後に再正規化できる
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.db.models import Claim, EngineRun, Evidence, NormalizedResult, Source

NORMALIZER_VERSION = "1"

_TRACKING_PARAMS = re.compile(r"^(utm_|fbclid|gclid|ref_|mc_eid|mc_cid)")


def canonicalize_url(url: str) -> str:
    """tracking parameter・fragment除去、host小文字化、末尾slash正規化。"""
    try:
        parsed = urlparse(url.strip())
    except ValueError:
        return url
    if not parsed.scheme:
        return url
    query = [
        (k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True)
        if not _TRACKING_PARAMS.match(k.lower())
    ]
    path = parsed.path or "/"
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")
    return urlunparse(
        (
            parsed.scheme.lower(),
            (parsed.hostname or "").lower() + (f":{parsed.port}" if parsed.port else ""),
            path,
            parsed.params,
            urlencode(query),
            "",  # fragment除去
        )
    )


def normalize_claim_text(text: str) -> str:
    """比較用の正規化テキスト (NFKC、空白圧縮、末尾記号除去、小文字化)。"""
    t = unicodedata.normalize("NFKC", text)
    t = re.sub(r"\s+", " ", t).strip().lower()
    t = t.rstrip("。.!?！？")
    return t


def normalize_run_result(
    session: Session,
    run: EngineRun,
    result: dict[str, Any],
    *,
    raw_artifact_id: str | None,
) -> NormalizedResult:
    """Runner結果をDBの正規化結果へ変換する。冪等 (再実行時は置き換え)。"""
    warnings: list[str] = list(result.get("warnings") or [])

    # 冪等化: 既存の正規化結果と関連行を削除して再作成
    existing = session.scalar(
        select(NormalizedResult).where(NormalizedResult.run_id == run.id)
    )
    if existing is not None:
        old_claim_ids = session.scalars(
            select(Claim.id).where(Claim.run_id == run.id)
        ).all()
        if old_claim_ids:
            session.execute(delete(Evidence).where(Evidence.claim_id.in_(old_claim_ids)))
        session.execute(delete(Claim).where(Claim.run_id == run.id))
        session.execute(delete(Source).where(Source.run_id == run.id))
        session.delete(existing)
        session.flush()

    metrics = dict(result.get("metrics") or {})
    for field in ("prompt_tokens", "completion_tokens", "total_tokens", "llm_cost_usd"):
        if metrics.get(field) is None:
            metrics.setdefault("_nulls", []).append(field)
    if metrics.get("_nulls"):
        warnings.append(
            "次のmetricsはエンジンから取得できませんでした (null): "
            + ", ".join(metrics.pop("_nulls"))
        )

    summary = result.get("summary")
    report_md = result.get("report_markdown")
    if not report_md and not summary:
        warnings.append("エンジンがreport/summaryを返しませんでした")

    normalized = NormalizedResult(
        run_id=run.id,
        normalizer_version=NORMALIZER_VERSION,
        summary=summary,
        report_markdown=report_md,
        metrics=metrics,
        warnings=warnings,
        raw_artifact_id=raw_artifact_id,
    )
    session.add(normalized)

    # sources: run内でcanonical URL単位に重複排除 (provenanceはrun行で保持)
    url_to_source: dict[str, Source] = {}
    for s in result.get("sources") or []:
        url = (s.get("url") or "").strip()
        if not url:
            warnings.append("URLのないsourceをスキップしました")
            continue
        canonical = canonicalize_url(url)
        if canonical in url_to_source:
            continue
        source = Source(
            job_id=run.job_id,
            run_id=run.id,
            url=url,
            canonical_url=canonical,
            title=s.get("title"),
            fetched_at=None,
            excerpt=s.get("excerpt"),
            meta=s.get("meta") or {},
        )
        raw_fetched = s.get("fetched_at")
        if raw_fetched:
            from datetime import datetime

            try:
                source.fetched_at = datetime.fromisoformat(raw_fetched)
            except (TypeError, ValueError):
                pass
        session.add(source)
        url_to_source[canonical] = source
    session.flush()

    # claims + evidence
    for pos, c in enumerate(result.get("claims") or []):
        text = (c.get("text") or "").strip()
        if not text:
            continue
        claim = Claim(
            job_id=run.job_id,
            run_id=run.id,
            text=text,
            normalized_text=normalize_claim_text(text),
            position=pos,
            meta={
                k: v
                for k, v in (("key", c.get("key")), ("value", c.get("value")))
                if v is not None
            }
            | (c.get("meta") or {}),
        )
        session.add(claim)
        session.flush()
        for ev in c.get("evidence") or []:
            src_url = canonicalize_url(ev.get("source_url") or "")
            source = url_to_source.get(src_url)
            if source is None:
                # evidenceが未知のURLを指す場合、sourceとして追補する (捏造ではなく
                # engine出力に存在したURL)。
                original = ev.get("source_url") or ""
                if not original:
                    warnings.append(f"claim '{text[:40]}...' のevidenceにURLがありません")
                    continue
                source = Source(
                    job_id=run.job_id,
                    run_id=run.id,
                    url=original,
                    canonical_url=src_url,
                    title=None,
                    excerpt=ev.get("excerpt"),
                    meta={"from_evidence": True},
                )
                session.add(source)
                session.flush()
                url_to_source[src_url] = source
            session.add(
                Evidence(
                    claim_id=claim.id,
                    source_id=source.id,
                    excerpt=ev.get("excerpt"),
                    locator=ev.get("locator"),
                    stance=ev.get("stance") or "supports",
                    verification=ev.get("verification") or "unverified",
                )
            )

    normalized.warnings = warnings
    session.flush()
    return normalized
