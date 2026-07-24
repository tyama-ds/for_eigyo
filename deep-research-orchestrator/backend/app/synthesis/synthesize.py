"""Grounded Synthesis Engine。

- 統合モデルへ渡す資料は正規化済みclaimsとEvidence Registryに限定する
- 出典は [S1] 形式のIDで参照させ、応答内の引用IDをsource registryへ解決検証する
- 未知URL・未知IDの引用は除去してwarningにする (新しい事実やURLを生成させない)
- 矛盾はcompare結果をそのままセクションへ残す (多数決で隠さない)
"""

from __future__ import annotations

import json
import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.db.models import Claim, EngineRun, Evidence, RunStatus, Source, SynthesisResult
from app.llm.client import LlmError, chat_completion
from app.llm.profiles import (
    ProfileNotConfiguredError,
    effective_proxy_policy,
    load_allowlist,
    resolve_profile,
    resolve_role_profile,
)

_CITATION_RE = re.compile(r"\[(S\d+)\]")

SYSTEM_PROMPT = """あなたは複数の調査エンジンの結果を統合するアナリストです。
与えられた「主張一覧」「出典一覧」「比較結果」だけを根拠に統合レポートを書いてください。

厳守事項:
1. 与えられた資料にない事実・数値・URLを一切追加しない。
2. 事実の記述には必ず [S番号] 形式で出典IDを付ける。出典IDは出典一覧にあるものだけを使う。
3. 矛盾する主張は「矛盾点」として両論併記し、どちらかを勝者として断定しない。
4. 根拠不足の主張は「根拠不足」と明示する。
5. Markdownで、次のセクション構成で書く:
   ## 概要 / ## 一致した発見 / ## 一部のエンジンのみの発見 / ## 矛盾点 /
   ## 根拠不足の主張 / ## 未解決の論点
6. 指定された言語で書く。
"""


def build_synthesis_input(
    session: Session, job_id: str, compare_result: dict[str, Any], language: str
) -> tuple[str, dict[str, dict[str, Any]]]:
    """LLMへ渡すユーザープロンプトと、[S番号] -> source情報 の解決表を作る。"""
    runs = {
        r.id: r
        for r in session.scalars(
            select(EngineRun).where(
                EngineRun.job_id == job_id, EngineRun.status == RunStatus.succeeded.value
            )
        )
    }
    sources = [
        s
        for s in session.scalars(select(Source).where(Source.job_id == job_id))
        if s.run_id in runs
    ]
    # canonical URL単位でsource IDを振る (provenanceとして発見エンジンを列挙)
    registry: dict[str, dict[str, Any]] = {}
    canonical_to_sid: dict[str, str] = {}
    for s in sorted(sources, key=lambda x: (x.canonical_url, x.run_id)):
        if s.canonical_url not in canonical_to_sid:
            sid = f"S{len(canonical_to_sid) + 1}"
            canonical_to_sid[s.canonical_url] = sid
            registry[sid] = {
                "sid": sid,
                "canonical_url": s.canonical_url,
                "url": s.url,
                "title": s.title,
                "excerpt": s.excerpt,
                "engines": [],
                "source_ids": [],
            }
        entry = registry[canonical_to_sid[s.canonical_url]]
        engine = runs[s.run_id].engine_id
        if engine not in entry["engines"]:
            entry["engines"].append(engine)
        entry["source_ids"].append(s.id)

    claims = [
        c
        for c in session.scalars(select(Claim).where(Claim.job_id == job_id))
        if c.run_id in runs
    ]
    claim_lines = []
    for c in sorted(claims, key=lambda x: (x.run_id, x.position)):
        evidence = list(session.scalars(select(Evidence).where(Evidence.claim_id == c.id)))
        sids = []
        for ev in evidence:
            src = session.get(Source, ev.source_id)
            if src is not None and src.canonical_url in canonical_to_sid:
                sids.append(canonical_to_sid[src.canonical_url])
        engine = runs[c.run_id].engine_id
        cite = " ".join(f"[{sid}]" for sid in dict.fromkeys(sids)) or "(引用なし)"
        claim_lines.append(f"- ({engine}) {c.text} {cite}")

    source_lines = [
        f"[{sid}] {info['title'] or '(タイトルなし)'} — {info['canonical_url']} "
        f"(発見: {', '.join(info['engines'])})"
        for sid, info in registry.items()
    ]

    compare_brief = {
        "conflicts": [
            {"key": c.get("key"), "values": c.get("values"), "engines": c.get("engines")}
            for c in compare_result.get("conflicts", [])
        ],
        "unsupported_claims": [
            {"text": u.get("text"), "engine_id": u.get("engine_id")}
            for u in compare_result.get("unsupported_claims", [])
        ],
        "open_questions": compare_result.get("open_questions", []),
    }

    prompt = (
        f"言語: {language}\n\n"
        "## 主張一覧 (エンジン別、引用付き)\n" + "\n".join(claim_lines) + "\n\n"
        "## 出典一覧\n" + "\n".join(source_lines) + "\n\n"
        "## 機械比較の結果 (矛盾・根拠不足)\n"
        + json.dumps(compare_brief, ensure_ascii=False, indent=1)
        + "\n\n上記のみを根拠として統合レポートを書いてください。"
    )
    return prompt, registry


def validate_citations(
    report_md: str, registry: dict[str, dict[str, Any]]
) -> tuple[str, list[dict[str, Any]], list[str]]:
    """レポート内の [S番号] をregistryへ解決する。未知IDは除去しwarning。"""
    warnings: list[str] = []
    used: dict[str, dict[str, Any]] = {}

    def _replace(match: re.Match[str]) -> str:
        sid = match.group(1)
        if sid in registry:
            used[sid] = registry[sid]
            return match.group(0)
        warnings.append(f"未知の引用ID [{sid}] を除去しました (LLMが創作した可能性)")
        return ""

    cleaned = _CITATION_RE.sub(_replace, report_md)
    # LLMが生のURLを書いた場合、registryにないURLはwarning
    for url in re.findall(r"https?://[^\s)\]>\"']+", cleaned):
        canonical_urls = {info["canonical_url"] for info in registry.values()}
        raw_urls = {info["url"] for info in registry.values()}
        if url.rstrip(".,)") not in canonical_urls | raw_urls:
            warnings.append(f"出典一覧にないURLがレポートに含まれています: {url[:120]}")
    citations = [
        {
            "sid": sid,
            "url": info["url"],
            "canonical_url": info["canonical_url"],
            "title": info["title"],
            "excerpt": info["excerpt"],
            "engines": info["engines"],
            "source_ids": info["source_ids"],
        }
        for sid, info in used.items()
    ]
    return cleaned, citations, warnings


def run_synthesis(
    session: Session,
    settings: Settings,
    job_id: str,
    compare_result: dict[str, Any],
    *,
    language: str = "ja",
    profile_id: str | None = None,
) -> SynthesisResult:
    """統合レポートを生成する。LLM未設定なら status=unavailable で理由を残す。"""
    synthesis = session.scalar(
        select(SynthesisResult).where(SynthesisResult.job_id == job_id)
    )
    if synthesis is None:
        synthesis = SynthesisResult(job_id=job_id)
        session.add(synthesis)
        session.flush()

    synthesis.attempt += 1
    synthesis.status = "running"
    # 決定論的な比較セクションは常に保存 (LLM不要部分)
    synthesis.sections = compare_result
    session.flush()

    try:
        if profile_id:
            profile = resolve_profile(session, settings, profile_id)
        else:
            profile = resolve_role_profile(session, settings, "synthesis")
    except ProfileNotConfiguredError as e:
        synthesis.status = "unavailable"
        synthesis.error = str(e)
        session.flush()
        return synthesis

    prompt, registry = build_synthesis_input(session, job_id, compare_result, language)
    policy = effective_proxy_policy(session, settings)
    allowlist = load_allowlist(session)

    try:
        response = chat_completion(
            profile,
            [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            max_tokens=4096,
            temperature=0.2,
            policy=policy,
            allowlist=allowlist,
        )
    except LlmError as e:
        synthesis.status = "failed"
        synthesis.error = str(e)
        synthesis.llm_profile_id = profile.profile_id
        session.flush()
        return synthesis

    cleaned, citations, warnings = validate_citations(response.text, registry)
    synthesis.report_markdown = cleaned
    synthesis.citations = citations
    synthesis.warnings = warnings
    synthesis.llm_profile_id = profile.profile_id
    synthesis.status = "succeeded"
    synthesis.error = None
    session.flush()
    return synthesis
