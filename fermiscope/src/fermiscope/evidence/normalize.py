"""証拠値の単位正規化(ブリッジ規則つき)。

証拠は「10.4%」「300件」のようにパラメータ単位と異なる表現で報告されることが
多い。ここでは決定論的なブリッジ規則で変換し、仮定を伴う変換は
必ず注記文字列として返す(無言の変換をしない)。
"""

from __future__ import annotations

from fermiscope.formula.units import convert_value

# 比率・無次元とみなせる目標単位(この場合のみ % / 無単位の橋渡しを許す)。
_RATIO_TARGETS = {"", "dimensionless", "ratio", "rate", "percent", "%", "割合"}

# レート単位の分子として橋渡しを許す数量単位の同義集合(明示的・監査可能・係数なし)。
# 例: 「調律 300件」は tuning/(person*year) の分子(tuning)として解釈してよい。
# ここに無い組合せの count→レート変換は行わず、未解決化する(汎用変換の禁止)。
_RATE_NUMERATOR_SYNONYMS: dict[str, set[str]] = {
    "tuning": {"tuning", "event"},
    "event": {"event", "tuning"},
    "item": {"item"},
    "piano": {"piano", "item"},
    "vehicle": {"vehicle", "item"},
    "person": {"person"},
    "household": {"household"},
    "store": {"store"},
    "company": {"company"},
    "JPY": {"JPY"},
}


def normalize_value(value: float, from_unit: str, to_unit: str) -> tuple[float | None, str]:
    """証拠値をパラメータ単位へ正規化する。

    明示的で監査可能な規則のみで変換する。汎用の『無次元→任意単位』『任意カウント→
    任意の合成レート』変換は行わない(単位が食い違えば None を返して未解決化する)。

    Returns:
        (正規化値 or None, 仮定の注記。注記が空なら無仮定の変換)
    """
    fu = (from_unit or "").strip()
    tu = (to_unit or "").strip()
    if fu == tu:
        return value, ""
    if not fu:
        # 証拠に単位が無い場合、目標が比率・無次元のときのみ小数としてそのまま採用する。
        # 具体的なカウント/通貨単位へは無言変換しない(汎用の空変換の禁止)。
        if tu in _RATIO_TARGETS:
            return value, ""
        return None, ""
    # 1) 厳密変換(Pint)。失敗時は下の明示ブリッジ規則へ進む。
    try:
        return convert_value(value, fu, tu), ""
    except Exception:  # noqa: S110 — 変換不能は想定内で、後段の規則で処理する
        pass
    # 2) パーセント → 比率・レート(値/100)。目標が比率・無次元・レートのときのみ。
    if fu == "percent" and (tu in _RATIO_TARGETS or "/" in tu):
        return value / 100.0, (
            f"%表記をパラメータ単位({tu})の比率として解釈しました(値/100)。"
        )
    # 3) カウント単位 → レート単位の分子。明示的な同義表で分子が一致する場合のみ許す。
    #    分母(期間・母数)は問いの文脈に一致すると仮定し、注記に必ず残す。
    if "/" in tu:
        numerator = tu.split("/", 1)[0].strip("() ")
        allowed = _RATE_NUMERATOR_SYNONYMS.get(numerator, {numerator})
        if fu in allowed:
            return value, (
                f"証拠の数量単位({fu})をレート単位({tu})の分子として解釈しました"
                "(分母の期間・母数は問いの文脈に一致すると仮定。要確認)。"
            )
    # 明示規則に該当しない変換は行わない(誤った値の無警告表示より未解決を優先)。
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
