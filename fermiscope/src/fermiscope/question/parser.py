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
    # 「市」は市場・市況等の複合語を誤検出しないよう、直後に区・町・村・場が続かない場合のみ
    r"[一-龠ぁ-んァ-ン]{1,6}市(?![区町村場況街民])|日本国?内?|全国|世界)"
)

_YEAR_PATTERN = re.compile(r"((?:19|20)\d{2})年")

# 全角数字→半角
_ZEN2HAN = str.maketrans("０１２３４５６７８９", "0123456789")

# 期間単位語 → 正準単位(長い語を先に判定)。
_PERIOD_UNIT_WORDS: list[tuple[str, str]] = [
    ("時間", "hour"),
    ("週間", "week"),
    ("週", "week"),
    ("か月", "month"),
    ("ヶ月", "month"),
    ("カ月", "month"),
    ("箇月", "month"),
    ("日", "day"),
    ("年", "year"),
    ("月", "month"),
]

# 正準単位 → 表示用の期間文字列(レート、量=1)。
_RATE_PERIOD_LABEL = {
    "hour": "1時間", "day": "1日", "week": "1週間", "month": "1か月", "year": "1年間",
}


def _period_unit_of(word: str) -> str:
    for w, unit in _PERIOD_UNIT_WORDS:
        if w in word:
            return unit
    return ""


def _parse_period(question: str) -> tuple[str, float, str, bool] | None:
    """問いから期間を構造化して取り出す。

    Returns (表示文字列, 量, 正準単位, レートか) または None(期間なし)。
    「毎日」→ (1日, 1, day, True)。「7日間」→ (7日間, 7, day, False)。
    「年間」→ (1年間, 1, year, True)。日付・年号(2021年 等)は期間として拾わない。
    """
    q = question.translate(_ZEN2HAN)
    # 1) 「毎<単位>」= 単位あたりのレート(量=1)
    for word, unit in (("毎時", "hour"), ("毎日", "day"), ("毎週", "week"),
                       ("毎月", "month"), ("毎年", "year")):
        if word in q:
            return _RATE_PERIOD_LABEL[unit], 1.0, unit, True
    # 2) 「<単位>あたり/当たり」= レート(量=1)。例: 1日あたり、日当たり
    m = re.search(r"([0-9]*)\s*(時間|日|週間?|か月|ヶ月|カ月|年)\s*(?:あたり|当たり)", q)
    if m:
        unit = _period_unit_of(m.group(2))
        if unit:
            return _RATE_PERIOD_LABEL[unit], 1.0, unit, True
    # 3) 数値つき「N<単位>間」= 期間全体の合計(量=N)。例: 7日間、3か月間
    m = re.search(r"([0-9]+)\s*(時間|日間|週間|か月間|ヶ月間|カ月間|年間)", q)
    if m:
        n = float(m.group(1))
        unit = _period_unit_of(m.group(2))
        if unit and n != 1:
            return f"{m.group(1)}{m.group(2)}", n, unit, False
        if unit:
            return _RATE_PERIOD_LABEL[unit], 1.0, unit, True
    # 4) 数値なしの「<単位>間」= レート(年間/月間/週間/日間 = 〜あたり)
    m = re.search(r"(?<![0-9])(時間|日間|週間|か月間|ヶ月間|カ月間|年間|月間)", q)
    if m:
        unit = _period_unit_of(m.group(1))
        if unit:
            return _RATE_PERIOD_LABEL[unit], 1.0, unit, True
    # 5) 「N<単位>に/で」= N==1 ならレート、それ以外は合計。例: 1日に、7日で
    m = re.search(r"([0-9]+)\s*(時間|日|週間?|か月|ヶ月|カ月|年)\s*(?:に|で)", q)
    if m:
        n = float(m.group(1))
        unit = _period_unit_of(m.group(2))
        if unit and n == 1:
            return _RATE_PERIOD_LABEL[unit], 1.0, unit, True
        if unit:
            return f"{m.group(1)}{m.group(2)}間", n, unit, False
    return None

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

# 既知の目標単位(日本語助数詞 + 正準単位)。未知の単位は無言変換せず 422 にする。
_KNOWN_DISPLAY_UNITS = {ja for _, ja, _ in _UNIT_HINTS} | {
    "名", "匹", "個", "軒", "機", "隻", "冊", "枚", "杯", "着", "足",
}
_KNOWN_CANONICAL_UNITS = {pint for _, _, pint in _UNIT_HINTS} | {
    "person", "household", "item", "event", "JPY", "store", "company", "dimensionless",
}


def is_known_target_unit(unit: str) -> bool:
    """目標単位が既知(日本語助数詞または正準単位)か。空は許容(暫定推定)。"""
    u = (unit or "").strip()
    if not u:
        return True
    return u in _KNOWN_DISPLAY_UNITS or u in _KNOWN_CANONICAL_UNITS


def _extract_subject(question: str, geography: str) -> str:
    text = question
    if geography:
        text = text.replace(geography, "")
    # 期間表現を除去(毎日/N日に/N日間/1日あたり/年間 等)。subject に混ぜない。
    text = re.sub(
        r"(毎[時日週月年]"
        r"|[0-9０-９]+\s*(?:時間|日間?|週間?|か月間?|ヶ月間?|カ月間?|年間?)\s*(?:に|で|の間)?"
        r"|(?:時間|日|週|か月|年)\s*(?:あたり|当たり)"
        r"|年間|月間|週間)",
        "",
        text,
    )
    # 「都内」「市内」等の残り(地域語の接尾)を除去
    text = re.sub(r"^(都内|市内|県内|府内|国内|内)", "", text.strip())
    # 先頭に残る助詞(の/で/に/は/が/を/における/では/には/の中で)を除去
    text = re.sub(
        r"^[\s、。]*(における|の中で|には|では|の|で|に|は|が|を)\s*",
        "",
        text.strip(),
    )
    # 末尾の疑問・述語を刈り込む
    text = re.sub(
        r"(は|が)?(何人いる|何人|何台|何本|何匹|何件|何世帯|何社|何店|何回|いくら|"
        r"どれ(?:くらい|ぐらい)|いる|ある|存在する|必要|売れる|使われる|使用される|"
        r"廃棄される|消費される|います|ますか?|です|だ|である|でしょうか?|の?数量?|"
        r"か|\?|？|。|\s)+$",
        "",
        text.strip(),
    )
    # 文末に助詞や「〜は」が残った場合の後始末
    text = re.sub(r"(は|が|を|の)$", "", text.strip())
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
    period_quantity = 1.0
    period_unit = ""
    period_is_rate = True
    parsed_period = _parse_period(question)
    if parsed_period is not None:
        time_period, period_quantity, period_unit, period_is_rate = parsed_period
        stock_or_flow = StockOrFlow.FLOW
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
        period_quantity=period_quantity,
        period_unit=period_unit,
        period_is_rate=period_is_rate,
        stock_or_flow=stock_or_flow,
        target_metric=f"{subject}の{'数量' if stock_or_flow == StockOrFlow.STOCK else 'フロー量'}",
        target_unit=target_unit,
        target_unit_display=target_unit_ja,
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
