"""証拠間の矛盾検出。

矛盾は平均して隠さず、定義差・時点差・地域差・調査方法差を分析して表示する
(絶対条件10)。
"""

from __future__ import annotations

import re

from fermiscope.config import Settings
from fermiscope.domain.models import Contradiction, EvidenceItem, ParameterEstimate
from fermiscope.evidence.dates import parse_year
from fermiscope.evidence.normalize import normalize_value

# 定義・母集団テキストの表記揺れ(空白・句読点・全角半角)を吸収して比較する
_NORM_STRIP = re.compile(r"[\s、。,.　・:：/／()()「」\"']+")
_FULLWIDTH_DIGITS = str.maketrans("0123456789", "0123456789")


def _norm_text(s: str) -> str:
    return _NORM_STRIP.sub("", (s or "").translate(_FULLWIDTH_DIGITS)).lower()


def _text_conflict(a: str, b: str) -> bool:
    """双方に記載があり、正規化しても内容が異なる場合のみ True(表記揺れは無視)。"""
    if not a or not b:
        return False
    return _norm_text(a) != _norm_text(b)


def _normalized(ev: EvidenceItem, unit: str) -> float | None:
    if ev.extracted_value is None:
        return None
    if not unit:
        return ev.extracted_value
    value, _ = normalize_value(ev.extracted_value, ev.unit, unit)
    return value


def _analyze_differences(a: EvidenceItem, b: EvidenceItem) -> dict[str, str]:
    analysis: dict[str, str] = {}
    if _text_conflict(a.exact_definition, b.exact_definition):
        analysis["definition"] = (
            f"定義差: 「{a.exact_definition}」と「{b.exact_definition}」で対象の定義が異なります。"
        )
    ya, yb = parse_year(a.time_period or a.publication_date), parse_year(
        b.time_period or b.publication_date
    )
    if ya and yb and ya != yb:
        analysis["time"] = f"時点差: {ya}年と{yb}年のデータで、時点が {abs(ya - yb)} 年ずれています。"
    if a.geography and b.geography and a.geography != b.geography:
        analysis["geography"] = f"地域差: 「{a.geography}」と「{b.geography}」で対象地域が異なります。"
    ma, mb = bool(a.methodology_summary), bool(b.methodology_summary)
    if a.methodology_summary and b.methodology_summary and a.methodology_summary != b.methodology_summary:
        analysis["method"] = "調査方法差: 両者の調査方法が異なります(各証拠の方法欄を参照)。"
    elif ma != mb:
        analysis["method"] = "調査方法差: 一方は方法を明示、他方は方法不明です。"
    if _text_conflict(a.population_definition, b.population_definition):
        analysis["population"] = (
            f"母集団差: 「{a.population_definition}」と「{b.population_definition}」。"
        )
    if not analysis:
        analysis["unknown"] = (
            "定義・時点・地域・方法の記載からは差の原因を特定できませんでした。"
            "少なくとも一方の測定に問題がある可能性があります。"
        )
    return analysis


def detect_contradictions(
    param: ParameterEstimate,
    items: list[EvidenceItem],
    settings: Settings,
) -> list[Contradiction]:
    """パラメータに紐づく証拠間の不一致を検出する。

    クラスタ代表(スコア最高)同士で比較し、比が閾値を超えるペアを矛盾とする。
    定義が非互換なペアは統合対象から外す(incompatible_reason 設定)。
    """
    threshold = settings.scoring.contradiction.ratio_threshold
    # クラスタ代表を選ぶ(反証・訂正目的の証拠は敵対的検証側で扱うため除外)
    from fermiscope.domain.enums import SearchPurpose

    reps: dict[str, EvidenceItem] = {}
    for ev in items:
        if not ev.accepted or ev.extracted_value is None:
            continue
        if ev.search_purpose in (SearchPurpose.COUNTER_EVIDENCE, SearchPurpose.CORRECTION):
            continue
        key = ev.cluster_id or ev.id
        cur = reps.get(key)
        if cur is None or (ev.evidence_score or 0) > (cur.evidence_score or 0):
            reps[key] = ev

    rep_list = [ev for ev in reps.values() if _normalized(ev, param.unit) is not None]
    contradictions: list[Contradiction] = []
    for i in range(len(rep_list)):
        for j in range(i + 1, len(rep_list)):
            a, b = rep_list[i], rep_list[j]
            va, vb = _normalized(a, param.unit), _normalized(b, param.unit)
            if va is None or vb is None or va <= 0 or vb <= 0:
                continue
            ratio = max(va, vb) / min(va, vb)
            if ratio <= threshold:
                continue
            analysis = _analyze_differences(a, b)
            contradictions.append(
                Contradiction(
                    parameter_id=param.id,
                    evidence_ids=[a.id, b.id],
                    ratio=round(ratio, 2),
                    analysis=analysis,
                    note=(
                        f"証拠間で値が {ratio:.1f} 倍乖離しています"
                        f"({va:g} {param.unit} vs {vb:g} {param.unit})。"
                        "平均で隠さず、差の原因分析を確認してください。"
                    ),
                )
            )
            # 定義が非互換なら低スコア側を統合から除外(表示は残す)
            if "definition" in analysis or "population" in analysis:
                weaker = a if (a.evidence_score or 0) < (b.evidence_score or 0) else b
                stronger = b if weaker is a else a
                weaker.incompatible_reason = (
                    f"証拠 {stronger.id} と定義が非互換のため統合から除外"
                    "(矛盾レコードを参照)。"
                )
    return contradictions
