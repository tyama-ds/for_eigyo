"""式ツリー・安全な式評価のテスト。"""

import numpy as np
import pytest

from fermiscope.domain.models import FormulaGraph
from fermiscope.formula.graph import (
    FormulaEvalError,
    evaluate_node,
    render_expression,
    replace_parameter,
)
from fermiscope.formula.parser import FormulaParseError, parse_expression


def test_parse_and_evaluate_basic():
    tree = parse_expression("a * b / c")
    assert evaluate_node(tree, {"a": 10.0, "b": 3.0, "c": 2.0}) == pytest.approx(15.0)


def test_evaluate_with_numpy_arrays():
    tree = parse_expression("a * b")
    result = evaluate_node(tree, {"a": np.array([1.0, 2.0]), "b": np.array([3.0, 4.0])})
    assert np.allclose(result, [3.0, 8.0])


def test_division_by_zero_scalar_raises():
    tree = parse_expression("a / b")
    with pytest.raises(FormulaEvalError):
        evaluate_node(tree, {"a": 1.0, "b": 0.0})


def test_division_by_zero_array_yields_nan():
    tree = parse_expression("a / b")
    result = evaluate_node(tree, {"a": np.array([1.0, 1.0]), "b": np.array([2.0, 0.0])})
    assert np.isnan(result[1]) and result[0] == pytest.approx(0.5)


def test_power_requires_constant_exponent():
    parse_expression("a ** 2")  # OK
    with pytest.raises(FormulaParseError):
        parse_expression("a ** b")


def test_unknown_parameter_rejected():
    with pytest.raises(FormulaParseError):
        parse_expression("a * b", known_parameters={"a"})


@pytest.mark.parametrize(
    "malicious",
    [
        "__import__('os').system('ls')",
        "().__class__.__bases__",
        "open('/etc/passwd')",
        "a[0]",
        "lambda: 1",
        "[x for x in range(10)]",
        "a if b else c",
        "'string'",
        "a; import os",
        "_secret",
    ],
)
def test_injection_rejected(malicious):
    with pytest.raises(FormulaParseError):
        parse_expression(malicious)


def test_eval_not_used():
    """安全性検査: parserモジュールがeval/execを使っていないこと。"""
    import inspect

    import fermiscope.formula.parser as parser_mod

    source = inspect.getsource(parser_mod)
    assert "eval(" not in source.replace("ast.literal_eval", "")
    assert "exec(" not in source


def test_render_roundtrip():
    tree = parse_expression("a * (b + c) / d")
    rendered = render_expression(tree)
    tree2 = parse_expression(rendered)
    values = {"a": 2.0, "b": 3.0, "c": 4.0, "d": 5.0}
    assert evaluate_node(tree, values) == pytest.approx(evaluate_node(tree2, values))


def test_replace_parameter_decomposition():
    units = {"a": "item", "b": "dimensionless", "b1": "dimensionless", "b2": "dimensionless"}
    tree = parse_expression("a * b")
    graph = FormulaGraph(root=tree, target_unit="item", expression="a * b")
    new_graph = replace_parameter(graph, "b", "b1 * b2", units)
    assert set(new_graph.leaf_parameter_ids()) == {"a", "b1", "b2"}
    value = evaluate_node(new_graph.root, {"a": 2.0, "b1": 3.0, "b2": 4.0})
    assert value == pytest.approx(24.0)
