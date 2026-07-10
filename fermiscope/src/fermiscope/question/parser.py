"""問いの正規化(ルールベース+LLM補助)。

不明な項目は勝手に確定せず、ProvisionalField として明示した上で
合理的なデフォルトを置いて処理を進める(結果画面で修正可能)。
"""

from __future__ import annotations

import re

from fermiscope.domain.enums import StockOrFlow
from fermiscope.domain.models import ProvisionalField, QuestionSpec
from fermiscope.llm.base import LLMProvider

_GEO_PATTERN = re.compile(
    r"(東京都|北海道|(?:京都|大阪)府|[一-龠ぁ-んァ-ン]{1,4}県|"
    r"[一-龠ぁ-んァ-ン]{1,6}市|日本国?内?|全国|世界)"
)

_YEAR_PATTERN = re.compile(r"((?:19|20)\d{2})年")

_FLOW_HINTS = [
    (re.compile(r"1日|毎日|日あたり|日間"), "1日"),
    (re.compile(r"年間|1年|毎年|年あたり"), "1年間"),
    (re.compile(r"月間|毎月|月あたり"), "1か月"),
]

_UNIT_HINTS: list[tuple[re.Pattern[str], str, str]] = [
    (re.compile(r"何人|人数|何名"), "人", "person"),
    (re.compile(r"何世帯"), "世帯", "household"),
    (re.compile(r"何台|台数"), "台", "item"),
    (re.compile(r"何本"), "本", "item"),
    (re.compile(r"何件|件数"), "件", "event"),
    (re.compile(r"いくら|market|市場規模|金額|売上"), "円", "JPY"),
    (re.compile(r"何店|店舗数"), "店", "store"),
    (re.compile(r"何社"), "社", "company"),
    (re.compile(r"何回"), "回", "event"),
]

_STOCK_HINTS = re.compile(r"何人いる|何台ある|何店ある|存在する|台数は|人数は|何人か")


def _extract_subject(question: str, geography: str) -> str:
    text = question
    if geography:
        text = text.replace(geography, "")
    # 「都内」「市内」等の残り(地域語の接尾)を除去
    text = re.sub(r"^(都内|市内|県内|府内|国内|内)", "", text.strip())
    # 助詞・疑問表現を刈り込む
    text = re.sub(r"(には|では|における|の中で|に|で)", " ", text, count=1)
    text = re.sub(
        r"(は|が)?(何人|何台|何本|何件|何世帯|いくら|どれ(?:くらい|ぐらい)|何人いる|いる|ある|必要|か|\?|\?|。)+$",
        "",
        text.strip(),
    )
    return text.strip() or question.strip()


def parse_question_rule_based(
    question: str,
    geography_hint: str = "",
    reference_date_hint: str = "",
    target_unit_hint: str = "",
    known_facts: list[str] | None = None,
    current_year: int = 2026,
) -> QuestionSpec:
    """ルールベースの問い正規化。"""
    provisional: list[ProvisionalField] = []

    geo_match = _GEO_PATTERN.search(question)
    geography = geography_hint or (geo_match.group(1) if geo_match else "")
    if not geography:
        geography = "日本"
        provisional.append(
            ProvisionalField(
                field="geography",
                assumed_value="日本",
                reason="問いに地域の記載がないため日本全国を暫定設定。",
            )
        )
    if geography in ("国内", "日本国内", "全国"):
        geography = "日本"

    year_match = _YEAR_PATTERN.search(question)
    reference_date = reference_date_hint or (year_match.group(1) if year_match else "")
    if not reference_date:
        reference_date = str(current_year)
        provisional.append(
            ProvisionalField(
                field="reference_date",
                assumed_value=str(current_year),
                reason="基準時点の記載がないため現在年を暫定設定。",
            )
        )

    stock_or_flow = StockOrFlow.UNKNOWN
    time_period = ""
    for pattern, period in _FLOW_HINTS:
        if pattern.search(question):
            stock_or_flow = StockOrFlow.FLOW
            time_period = period
            break
    if stock_or_flow == StockOrFlow.UNKNOWN:
        if _STOCK_HINTS.search(question) or re.search(r"何人|何台|人数|台数", question):
            stock_or_flow = StockOrFlow.STOCK
        else:
            stock_or_flow = StockOrFlow.STOCK
            provisional.append(
                ProvisionalField(
                    field="stock_or_flow",
                    assumed_value="stock",
                    reason="ストック/フローが判別できないためストックを暫定設定。",
                )
            )

    target_unit_ja = target_unit_hint
    target_unit = ""
    for pattern, ja, pint_unit in _UNIT_HINTS:
        if target_unit_hint and target_unit_hint == ja:
            target_unit = pint_unit
            break
        if not target_unit_hint and pattern.search(question):
            target_unit_ja = ja
            target_unit = pint_unit
            break
    if not target_unit:
        target_unit_ja = target_unit_ja or "件"
        target_unit = "event"
        provisional.append(
            ProvisionalField(
                field="target_unit",
                assumed_value=target_unit_ja,
                reason="単位が判別できないため「件」を暫定設定。",
            )
        )

    subject = _extract_subject(question, geography)

    return QuestionSpec(
        original_question=question,
        subject=subject,
        geography=geography,
        reference_date=reference_date,
        time_period=time_period,
        stock_or_flow=stock_or_flow,
        target_metric=f"{subject}の{'数量' if stock_or_flow == StockOrFlow.STOCK else 'フロー量'}",
        target_unit=target_unit,
        known_facts=known_facts or [],
        requested_precision="order_of_magnitude",
        language="ja",
        provisional=provisional,
        parsed_by="rule",
    )


async def parse_question(
    question: str,
    llm: LLMProvider | None = None,
    geography_hint: str = "",
    reference_date_hint: str = "",
    target_unit_hint: str = "",
    known_facts: list[str] | None = None,
    current_year: int = 2026,
) -> tuple[QuestionSpec, bool]:
    """問いを正規化する。

    Returns:
        (QuestionSpec, ai_assisted)
    """
    spec = parse_question_rule_based(
        question,
        geography_hint=geography_hint,
        reference_date_hint=reference_date_hint,
        target_unit_hint=target_unit_hint,
        known_facts=known_facts,
        current_year=current_year,
    )
    # ルールで暫定項目が多い場合のみLLM補助(AIフォールバック条件を明示)
    ai_assisted = False
    needs_help = len(spec.provisional) >= 2
    if needs_help and llm is not None and llm.available:
        result = await llm.classify_question(question)
        if result is not None:
            ai_assisted = True
            spec.parsed_by = "llm"
            if result.subject and any(p.field == "subject" for p in spec.provisional):
                spec.subject = result.subject
            if result.geography and any(p.field == "geography" for p in spec.provisional):
                spec.geography = result.geography
            if result.target_unit and any(p.field == "target_unit" for p in spec.provisional):
                spec.target_unit = result.target_unit
            if result.stock_or_flow in ("stock", "flow"):
                spec.stock_or_flow = StockOrFlow(result.stock_or_flow)
            spec.inclusions = result.inclusions or spec.inclusions
            spec.exclusions = result.exclusions or spec.exclusions
    return spec, ai_assisted
