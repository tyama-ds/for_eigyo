"""安全な式パーサ。

`eval` / `exec` は使用しない。Python の `ast` モジュールで構文木を作り、
四則演算・べき乗・単項マイナス・数値定数・パラメータ名のみを許可する
ホワイトリスト方式で FormulaNode ツリーへ変換する。
"""

from __future__ import annotations

import ast

from fermiscope.domain.models import FormulaNode

_ALLOWED_BINOPS: dict[type[ast.operator], str] = {
    ast.Add: "+",
    ast.Sub: "-",
    ast.Mult: "*",
    ast.Div: "/",
    ast.Pow: "**",
}


class FormulaParseError(ValueError):
    """許可されない構文・名前を含む式。"""


def _convert(node: ast.expr, known_parameters: set[str] | None) -> FormulaNode:
    if isinstance(node, ast.BinOp):
        op_type = type(node.op)
        if op_type not in _ALLOWED_BINOPS:
            raise FormulaParseError(f"許可されない演算子です: {op_type.__name__}")
        op = _ALLOWED_BINOPS[op_type]
        left = _convert(node.left, known_parameters)
        right = _convert(node.right, known_parameters)
        if op == "**":
            if right.kind != "constant" or right.value is None:
                raise FormulaParseError("べき乗の指数は数値定数のみ許可されます")
            if abs(right.value) > 8:
                raise FormulaParseError("べき乗の指数が大きすぎます(オーバーフロー防止のため|指数|<=8)")
        return FormulaNode(kind="op", op=op, children=[left, right])  # type: ignore[arg-type]
    if isinstance(node, ast.UnaryOp):
        if isinstance(node.op, ast.USub):
            operand = _convert(node.operand, known_parameters)
            if operand.kind == "constant" and operand.value is not None:
                return FormulaNode(kind="constant", value=-operand.value)
            minus_one = FormulaNode(kind="constant", value=-1.0)
            return FormulaNode(kind="op", op="*", children=[minus_one, operand])
        raise FormulaParseError(f"許可されない単項演算子です: {type(node.op).__name__}")
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, int | float):
            raise FormulaParseError(f"数値以外の定数は許可されません: {node.value!r}")
        return FormulaNode(kind="constant", value=float(node.value))
    if isinstance(node, ast.Name):
        name = node.id
        if name.startswith("_"):
            raise FormulaParseError(f"アンダースコアで始まる名前は許可されません: {name}")
        if known_parameters is not None and name not in known_parameters:
            raise FormulaParseError(f"未定義のパラメータです: {name}")
        return FormulaNode(kind="parameter", parameter_id=name)
    # 関数呼び出し・属性参照・添字・内包表記などはすべて拒否
    raise FormulaParseError(f"許可されない構文です: {type(node).__name__}")


def parse_expression(expression: str, known_parameters: set[str] | None = None) -> FormulaNode:
    """式文字列を FormulaNode ツリーに変換する。

    Args:
        expression: 例 "households * ownership_rate / capacity"
        known_parameters: 指定時、この集合にない名前をエラーにする。
    """
    if not expression or not expression.strip():
        raise FormulaParseError("式が空です")
    if len(expression) > 2000:
        raise FormulaParseError("式が長すぎます")
    # 演算子数を制限し、深いネストによる RecursionError / DoS を未然に防ぐ
    if sum(expression.count(op) for op in "+-*/") > 200:
        raise FormulaParseError("式が複雑すぎます(演算子が多すぎます)")
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        raise FormulaParseError(f"式の構文エラー: {exc.msg}") from exc
    try:
        return _convert(tree.body, known_parameters)
    except RecursionError:
        raise FormulaParseError("式のネストが深すぎます") from None


def expression_parameters(expression: str) -> list[str]:
    """式に含まれるパラメータ名(出現順・重複なし)。"""
    return parse_expression(expression).leaf_parameter_ids()
