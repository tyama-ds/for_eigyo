"""プロンプト定数のJSON出力例が壊れていないことの回帰テスト。

行長修正（E501）の機械分割で、JSON例のキー名や単語の途中に改行が入ると
モデルの出力形式を悪化させる。例が1行で完結していることを検証する。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


def _json_example_lines(text: str) -> list[str]:
    return [ln for ln in text.split("\n") if ln.strip().startswith("{")]


def test_prompt_json_examples_are_single_line():
    import llmlab.copilotresearch as cr
    import llmlab.loopsys as ls

    prompts = {
        "copilotresearch._OUTLINE_SYSTEM": cr._OUTLINE_SYSTEM,
        "copilotresearch._REFLECT_SYSTEM_like": getattr(cr, "_REFLECT_SYSTEM", ""),
        "loopsys._RAG_REWRITE_SYSTEM": ls._RAG_REWRITE_SYSTEM,
        "loopsys._RAG_GRADE_SYSTEM": ls._RAG_GRADE_SYSTEM,
    }
    for name, p in prompts.items():
        if not p:
            continue
        for ln in _json_example_lines(p):
            assert ln.count("{") == ln.count("}"), \
                f"{name}: JSON例が複数行に割れている: {ln!r}"
        # キー名の途中に改行が入っていない（"xxx\n": のパターンを禁止）
        assert not re.search(r'"[^"\n]*\n[^"\n]*"\s*:', p), \
            f"{name}: キー名の途中に改行が入っている"


def test_prompt_no_midword_linebreak_before_punct():
    """機械分割の痕跡（行頭が句点・閉じ括弧で始まる）が無い。"""
    import llmlab.loopsys as ls

    for p in (ls._RAG_REWRITE_SYSTEM, ls._RAG_GRADE_SYSTEM):
        for ln in p.split("\n"):
            assert not ln.startswith(("。", "」", "）")), f"文の途中で分割: {ln!r}"


if __name__ == "__main__":
    import pytest

    sys.exit(pytest.main([__file__, "-q"]))
