"""Pint による単位管理と式全体の次元整合検査。

フェルミ推定では「世帯 × 台/世帯 × 回/(台·年) ÷ 回/(人·年) = 人」のような
エンティティ単位の整合が本質的なので、person / household / piano などを
独自次元として定義する。未知のエンティティは config/units.txt で追加できる。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import pint

from fermiscope.domain.models import FormulaNode

# エンティティ次元の定義(pint 定義構文)
_ENTITY_DEFINITIONS = [
    "person = [person] = people = capita",
    "household = [household]",
    "piano = [piano]",
    "tuning = [tuning]",
    "JPY = [currency_jpy] = yen",
    "USD = [currency_usd]",
    "item = [item] = unit_item",
    "event = [event]",
    "vehicle = [vehicle]",
    "charger = [charger]",
    "umbrella = [umbrella]",
    "bottle = [bottle]",
    "store = [store]",
    "site = [site]",
    "company = [company]",
    "worker = person",
    "customer = person",
    "tuner = person",  # 調律師は人の部分集合(次元は person)
    # スケール付き単位は Pint の定義として登録する。
    # (単位式 "1e8 JPY" は parse_units が拒否するため、単位そのものとして定義する)
    "man_yen = 1e4 * JPY = 万円",
    "oku_yen = 1e8 * JPY = 億円",
    "cho_yen = 1e12 * JPY = 兆円",
    "wari = 10 * percent = 割",  # 1割 = 10%(無次元比)
]

# 日本語の単位表現 → pint 単位への変換表(問い・証拠の単位正規化に使用)
_JA_UNIT_MAP = {
    "人": "person",
    "名": "person",
    "世帯": "household",
    "円": "JPY",
    "億円": "oku_yen",
    "兆円": "cho_yen",
    "万円": "man_yen",
    "台": "item",
    "本": "item",
    "個": "item",
    "件": "event",
    "回": "event",
    "社": "company",
    "店": "store",
    "店舗": "store",
    "拠点": "site",
    "年": "year",
    "月": "month",
    "日": "day",
    "時間": "hour",
    "km": "kilometer",
    "km2": "kilometer**2",
    "km²": "kilometer**2",
    "%": "percent",
    "パーセント": "percent",
    "割": "wari",
    "無次元": "dimensionless",
}


@lru_cache(maxsize=1)
def get_registry(extra_definitions_path: str | None = None) -> pint.UnitRegistry:
    """エンティティ次元入りの Pint レジストリを返す(プロセス内共有)。"""
    reg: pint.UnitRegistry = pint.UnitRegistry()
    for definition in _ENTITY_DEFINITIONS:
        reg.define(definition)
    # 追加定義ファイル(任意)
    if extra_definitions_path:
        p = Path(extra_definitions_path)
        if p.exists():
            for line in p.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    reg.define(line)
    return reg


def translate_unit_ja(unit_text: str) -> str:
    """日本語単位表現を pint 構文へ変換する(未知語はそのまま返す)。"""
    text = unit_text.strip()
    if not text:
        return "dimensionless"
    if text in _JA_UNIT_MAP:
        return _JA_UNIT_MAP[text]
    # 「人/年」のような複合表現
    for ja, en in sorted(_JA_UNIT_MAP.items(), key=lambda kv: -len(kv[0])):
        if ja in text:
            text = text.replace(ja, f"({en})")
    return text


def normalize_unit(unit_text: str) -> str:
    """単位文字列を pint で解釈できる正規形へ。解釈不能なら元の文字列を返す。"""
    reg = get_registry()
    candidate = translate_unit_ja(unit_text)
    try:
        return str(reg.parse_units(candidate))
    except Exception:
        return unit_text


@dataclass
class UnitCheckResult:
    passed: bool
    detail: str
    result_unit: str = ""
    node_units: dict[str, str] = field(default_factory=dict)


class UnitPropagationError(ValueError):
    pass


def _propagate(
    node: FormulaNode,
    param_units: dict[str, str],
    reg: pint.UnitRegistry,
) -> pint.Quantity:
    if node.kind == "constant":
        return reg.Quantity(1.0, "dimensionless")
    if node.kind == "parameter":
        unit_text = param_units.get(node.parameter_id, "dimensionless") or "dimensionless"
        try:
            return reg.Quantity(1.0, translate_unit_ja(unit_text))
        except Exception as exc:
            raise UnitPropagationError(
                f"パラメータ {node.parameter_id} の単位 '{unit_text}' を解釈できません: {exc}"
            ) from exc
    # 演算ノード
    child_qs = [_propagate(c, param_units, reg) for c in node.children]
    if node.op in ("+", "-"):
        base = child_qs[0]
        for q in child_qs[1:]:
            if q.dimensionality != base.dimensionality:
                raise UnitPropagationError(
                    f"加減算の単位が一致しません: {base.units} と {q.units}"
                )
        return base
    if node.op == "*":
        result = child_qs[0]
        for q in child_qs[1:]:
            result = result * q
        return result
    if node.op == "/":
        result = child_qs[0]
        for q in child_qs[1:]:
            result = result / q
        return result
    if node.op == "**":
        exponent_node = node.children[1]
        if exponent_node.kind != "constant" or exponent_node.value is None:
            raise UnitPropagationError("べき乗の指数は数値定数である必要があります")
        return child_qs[0] ** exponent_node.value
    raise UnitPropagationError(f"未知の演算子: {node.op}")


def check_graph_units(
    root: FormulaNode,
    param_units: dict[str, str],
    target_unit: str,
) -> UnitCheckResult:
    """式ツリー全体の単位を伝播させ、目標単位との次元一致を検査する。"""
    reg = get_registry()
    try:
        result_q = _propagate(root, param_units, reg)
    except UnitPropagationError as exc:
        return UnitCheckResult(passed=False, detail=str(exc))

    target_text = translate_unit_ja(target_unit) if target_unit else "dimensionless"
    try:
        target_q = reg.Quantity(1.0, target_text)
    except Exception as exc:
        return UnitCheckResult(
            passed=False,
            detail=f"目標単位 '{target_unit}' を解釈できません: {exc}",
            result_unit=str(result_q.units),
        )

    # 年率などの時間次元も含めて次元で比較する。
    # 次元が一致しても倍率が異なる(日↔年=365倍、%↔比=100倍、万円↔円=1万倍等)
    # ケースを見逃さないよう、目標単位への換算係数が 1 であることも検査する。
    if result_q.dimensionality == target_q.dimensionality:
        try:
            scale_factor = (result_q / target_q).to("dimensionless").magnitude
        except Exception:  # noqa: BLE001 — 換算不能時は次元一致のみで合格とする
            scale_factor = 1.0
        if abs(scale_factor - 1.0) > 1e-6:
            return UnitCheckResult(
                passed=False,
                detail=(
                    f"単位の倍率不整合: 式の単位 [{result_q.units}] は目標単位 "
                    f"[{target_q.units}] と次元は一致しますが約 {scale_factor:g} 倍のスケール差が"
                    f"あります(例: 日↔年・%↔比・万円↔円)。パラメータの単位を目標単位に"
                    f"揃えてください。"
                ),
                result_unit=str(result_q.units),
            )
        return UnitCheckResult(
            passed=True,
            detail=f"単位整合: 式の単位 [{result_q.units}] は目標単位 [{target_q.units}] と一致します。",
            result_unit=str(result_q.units),
        )
    return UnitCheckResult(
        passed=False,
        detail=(
            f"単位不整合: 式の単位は [{result_q.units}]"
            f"(次元 {result_q.dimensionality})ですが、"
            f"目標単位は [{target_q.units}](次元 {target_q.dimensionality})です。"
        ),
        result_unit=str(result_q.units),
    )


def convert_value(value: float, from_unit: str, to_unit: str) -> float:
    """単位変換(決定論的・Python側)。変換不能なら ValueError。"""
    reg = get_registry()
    q = reg.Quantity(value, translate_unit_ja(from_unit))
    return float(q.to(translate_unit_ja(to_unit)).magnitude)
