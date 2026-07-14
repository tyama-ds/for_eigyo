"""証拠値の単位正規化(ブリッジ規則つき)。

証拠は「10.4%」「300件」のようにパラメータ単位と異なる表現で報告されることが
多い。ここでは決定論的なブリッジ規則で変換し、仮定を伴う変換は
必ず注記文字列として返す(無言の変換をしない)。
"""

from __future__ import annotations

from fermiscope.formula.units import convert_value

# ブリッジ可能な「単純カウント単位」
_SIMPLE_COUNT_UNITS = {
    "event",
    "item",
    "person",
    "household",
    "tuning",
    "piano",
    "day",
    "store",
    "company",
    "JPY",
}


def normalize_value(value: float, from_unit: str, to_unit: str) -> tuple[float | None, str]:
    """証拠値をパラメータ単位へ正規化する。

    Returns:
        (正規化値 or None, 仮定の注記。注記が空なら無仮定の変換)
    """
    fu = (from_unit or "").strip()
    tu = (to_unit or "").strip()
    if not fu or fu == tu:
        return value, ""
    # 1) 厳密変換(Pint)。失敗時は下のブリッジ規則へ進む。
    try:
        return convert_value(value, fu, tu), ""
    except Exception:  # noqa: S110 — 変換不能は想定内で、後段の規則で処理する
        pass
    # 2) パーセント → 比率・レート(値/100 を目標単位の値とみなす)
    if fu == "percent":
        return value / 100.0, (
            f"%表記をパラメータ単位({tu})の比率として解釈しました(値/100)。"
        )
    # 3) 無次元 → そのまま目標単位とみなす
    if fu == "dimensionless":
        return value, f"無次元値をパラメータ単位({tu})として解釈しました。"
    # 4) 単純カウント単位 → 複合レート単位(文脈上のレート表現とみなす)
    if fu in _SIMPLE_COUNT_UNITS and "/" in tu:
        return value, (
            f"証拠の単位({fu})を文脈からパラメータのレート単位({tu})として解釈しました。"
        )
    return None, ""


def expected_units_for(param_unit: str) -> set[str]:
    """パラメータ単位に対して抽出時に許容する証拠単位の集合。

    誤抽出(例: 「1人の調律師」の「1人」)を防ぐためのフィルタ。
    空文字列は「単位なしの小数」を許容することを意味する。
    """
    u = (param_unit or "").replace(" ", "")
    if u in ("", "dimensionless", "percent"):
        return {"percent", ""}
    numerator = u.split("/")[0].lstrip("(")
    mapping = {
        "tuning": {"event"},
        "event": {"event"},
        "item": {"item"},
        "piano": {"item"},
        "vehicle": {"item"},
        "umbrella": {"item"},
        "bottle": {"item"},
        "charger": {"item"},
        "person": {"person"},
        "tuner": {"person"},
        "household": {"household"},
        "day": {"day"},
        "hour": {"hour"},
        "minute": {"minute"},
        "month": {"month"},
        "year": {"year"},
        "kilometer": {"kilometer"},
        "km": {"kilometer"},
        "kilogram": {"kilogram"},
        "kg": {"kilogram"},
        "JPY": {"JPY"},
        "store": {"store"},
        "company": {"company"},
    }
    base = set(mapping.get(numerator, {""}))
    if "/" in u:
        # 比率・レートは % や単位なし小数でも報告され得る
        base |= {"percent", ""}
    return base
