"""research_planner — 末端パラメータごとの検索計画。

検索目的(定義確認〜訂正情報)を分け、日英のクエリを生成する。
調査モード(fast/standard/careful)で目的の数を調整する。
"""

from __future__ import annotations

from fermiscope.domain.enums import ResearchMode, SearchPurpose
from fermiscope.domain.models import ParameterEstimate, SearchQuery
from fermiscope.llm.base import LLMProvider

_PURPOSES_BY_MODE = {
    ResearchMode.FAST: [
        SearchPurpose.DIRECT_VALUE,
        SearchPurpose.PRIMARY_SOURCE,
    ],
    ResearchMode.STANDARD: [
        SearchPurpose.DIRECT_VALUE,
        SearchPurpose.PRIMARY_SOURCE,
        SearchPurpose.LATEST_VALUE,
        SearchPurpose.COUNTER_EVIDENCE,
    ],
    ResearchMode.CAREFUL: [
        SearchPurpose.DEFINITION,
        SearchPurpose.DIRECT_VALUE,
        SearchPurpose.PRIMARY_SOURCE,
        SearchPurpose.METHODOLOGY,
        SearchPurpose.LATEST_VALUE,
        SearchPurpose.ALTERNATIVE_VALUE,
        SearchPurpose.COUNTER_EVIDENCE,
        SearchPurpose.CORRECTION,
    ],
}

_PURPOSE_SUFFIX_JA = {
    SearchPurpose.DEFINITION: "定義",
    SearchPurpose.DIRECT_VALUE: "",
    SearchPurpose.PRIMARY_SOURCE: "政府統計",
    SearchPurpose.METHODOLOGY: "調査方法",
    SearchPurpose.LATEST_VALUE: "最新 統計",
    SearchPurpose.ALTERNATIVE_VALUE: "代替 推定",
    SearchPurpose.COUNTER_EVIDENCE: "批判 限界",
    SearchPurpose.CORRECTION: "訂正 改訂",
}

_PURPOSE_SUFFIX_EN = {
    SearchPurpose.DEFINITION: "definition",
    SearchPurpose.DIRECT_VALUE: "",
    SearchPurpose.PRIMARY_SOURCE: "official statistics",
    SearchPurpose.METHODOLOGY: "methodology",
    SearchPurpose.LATEST_VALUE: "latest statistics",
    SearchPurpose.ALTERNATIVE_VALUE: "alternative estimate",
    SearchPurpose.COUNTER_EVIDENCE: "criticism limitations",
    SearchPurpose.CORRECTION: "correction revised",
}


async def plan_searches(
    param: ParameterEstimate,
    mode: ResearchMode,
    reference_date: str,
    llm: LLMProvider | None = None,
    include_english: bool = True,
) -> tuple[list[SearchQuery], bool]:
    """パラメータの検索計画を作る。

    Returns:
        (SearchQueryのリスト, ai_assisted)
    """
    ai_assisted = False
    terms_ja = list(param.search_terms_ja)
    terms_en = list(param.search_terms_en)

    # 検索語が空の場合のみLLMフォールバック(条件を明示)
    if not terms_ja and llm is not None and llm.available:
        proposal = await llm.propose_queries(param.name, param.description, param.target_geography)
        if proposal is not None and (proposal.queries_ja or proposal.queries_en):
            terms_ja = proposal.queries_ja[:3]
            terms_en = proposal.queries_en[:2]
            ai_assisted = True
    if not terms_ja:
        terms_ja = [f"{param.target_geography} {param.name}".strip()]

    queries: list[SearchQuery] = []
    seen: set[str] = set()

    def add(query_text: str, purpose: SearchPurpose, language: str) -> None:
        text = " ".join(query_text.split())
        if not text or text.lower() in seen:
            return
        seen.add(text.lower())
        queries.append(
            SearchQuery(parameter_id=param.id, purpose=purpose, query=text, language=language)
        )

    purposes = _PURPOSES_BY_MODE[mode]
    primary_term_ja = terms_ja[0]
    for purpose in purposes:
        suffix = _PURPOSE_SUFFIX_JA[purpose]
        if purpose == SearchPurpose.DIRECT_VALUE:
            for term in terms_ja[:2]:
                add(term, purpose, "ja")
        elif purpose == SearchPurpose.LATEST_VALUE and reference_date:
            add(f"{primary_term_ja} {reference_date}", purpose, "ja")
        else:
            add(f"{primary_term_ja} {suffix}", purpose, "ja")

    if include_english and terms_en:
        add(terms_en[0], SearchPurpose.DIRECT_VALUE, "en")
        if mode == ResearchMode.CAREFUL:
            add(
                f"{terms_en[0]} {_PURPOSE_SUFFIX_EN[SearchPurpose.COUNTER_EVIDENCE]}",
                SearchPurpose.COUNTER_EVIDENCE,
                "en",
            )
    return queries, ai_assisted
