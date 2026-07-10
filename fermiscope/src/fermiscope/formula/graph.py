"""式グラフの構築・決定論的評価・レンダリング・部分置換。"""

from __future__ import annotations

from collections.abc import Mapping

import numpy as np

from fermiscope.domain.models import FormulaGraph, FormulaNode
from fermiscope.formula.parser import parse_expression
from fermiscope.formula.units import check_graph_units

Number = float | np.ndarray


class FormulaEvalError(ValueError):
    pass


def evaluate_node(node: FormulaNode, values: Mapping[str, Number]) -> Number:
    """式ツリーを決定論的に評価する(スカラー/NumPy配列両対応)。"""
    if node.kind == "constant":
        if node.value is None:
            raise FormulaEvalError("定数ノードに値がありません")
        return node.value
    if node.kind == "parameter":
        if node.parameter_id not in values:
            raise FormulaEvalError(f"パラメータ値が未設定です: {node.parameter_id}")
        return values[node.parameter_id]
    children = [evaluate_node(c, values) for c in node.children]
    if node.op == "+":
        result: Number = children[0]
        for c in children[1:]:
            result = result + c
        return result
    if node.op == "-":
        result = children[0]
        for c in children[1:]:
            result = result - c
        return result
    if node.op == "*":
        result = children[0]
        for c in children[1:]:
            result = result * c
        return result
    if node.op == "/":
        result = children[0]
        for c in children[1:]:
            if isinstance(c, np.ndarray):
                # MC評価: ゼロ割は NaN とし、後段で失敗反復として集計する
                with np.errstate(divide="ignore", invalid="ignore"):
                    result = np.where(c == 0, np.nan, result / np.where(c == 0, np.nan, c))
            else:
                if c == 0:
                    raise FormulaEvalError("ゼロ除算が発生しました")
                result = result / c
        return result
    if node.op == "**":
        return children[0] ** children[1]
    raise FormulaEvalError(f"未知の演算子: {node.op}")


def evaluate_graph(graph: FormulaGraph, values: Mapping[str, Number]) -> Number:
    return evaluate_node(graph.root, values)


_PRECEDENCE = {"+": 1, "-": 1, "*": 2, "/": 2, "**": 3}


def render_expression(node: FormulaNode, symbols: Mapping[str, str] | None = None) -> str:
    """式ツリーを人間可読な式文字列へ戻す。"""

    def render(n: FormulaNode, parent_prec: int) -> str:
        if n.kind == "constant":
            v = n.value if n.value is not None else 0.0
            return str(int(v)) if float(v).is_integer() and abs(v) < 1e15 else repr(v)
        if n.kind == "parameter":
            if symbols and n.parameter_id in symbols and symbols[n.parameter_id]:
                return symbols[n.parameter_id]
            return n.parameter_id
        prec = _PRECEDENCE.get(n.op, 0)
        parts = [render(c, prec + (1 if i > 0 and n.op in ("-", "/") else 0)) for i, c in enumerate(n.children)]
        text = f" {n.op} ".join(parts)
        if prec < parent_prec:
            return f"({text})"
        return text

    return render(node, 0)


def build_graph(
    expression: str,
    target_unit: str,
    param_units: dict[str, str],
    known_parameters: set[str] | None = None,
) -> FormulaGraph:
    """式文字列から FormulaGraph を構築し、単位検査を実行する。"""
    root = parse_expression(expression, known_parameters or set(param_units.keys()))
    check = check_graph_units(root, param_units, target_unit)
    return FormulaGraph(
        root=root,
        expression=render_expression(root),
        target_unit=target_unit,
        unit_check_passed=check.passed,
        unit_check_detail=check.detail,
    )


def replace_parameter(
    graph: FormulaGraph,
    parameter_id: str,
    sub_expression: str,
    param_units: dict[str, str],
) -> FormulaGraph:
    """末端パラメータを下位式で置換した新しいグラフを返す(再分解用)。"""
    subtree = parse_expression(sub_expression, set(param_units.keys()))

    def replace(n: FormulaNode) -> FormulaNode:
        if n.kind == "parameter" and n.parameter_id == parameter_id:
            return subtree.model_copy(deep=True)
        if n.children:
            return n.model_copy(update={"children": [replace(c) for c in n.children]})
        return n

    new_root = replace(graph.root)
    check = check_graph_units(new_root, param_units, graph.target_unit)
    return FormulaGraph(
        root=new_root,
        expression=render_expression(new_root),
        target_unit=graph.target_unit,
        unit_check_passed=check.passed,
        unit_check_detail=check.detail,
    )
