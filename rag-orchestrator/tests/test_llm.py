"""llm モジュール（think 分離）のユニットテスト。

Qwen3 / DeepSeek-R1 系の推論トークンの出方すべてで、
回答と推論過程が正しく分離されることを確認する。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ragcore.llm import split_think, strip_think  # noqa: E402


class TestSplitThink(unittest.TestCase):
    def test_no_think(self):
        self.assertEqual(split_think("ただの回答"), ("ただの回答", ""))
        self.assertEqual(split_think(""), ("", ""))

    def test_closed_block(self):
        answer, think = split_think("<think>考える</think>回答です")
        self.assertEqual(answer, "回答です")
        self.assertEqual(think, "考える")

    def test_multiple_blocks(self):
        answer, think = split_think("<think>A</think>回答<thinking>B</thinking>続き")
        self.assertEqual(answer, "回答続き")
        self.assertIn("A", think)
        self.assertIn("B", think)

    def test_orphan_closing_tag(self):
        # チャットテンプレートが開始タグを消費するモデル（Qwen3 で頻出）
        answer, think = split_think("まず整理すると…</think>最終回答")
        self.assertEqual(answer, "最終回答")
        self.assertEqual(think, "まず整理すると…")

    def test_unclosed_opening_tag(self):
        # max_tokens 到達で閉じタグが出ないまま切れた場合（従来は回答に漏れていた）
        answer, think = split_think("<think>延々と考えて途中で切れた")
        self.assertEqual(answer, "")
        self.assertEqual(think, "延々と考えて途中で切れた")

    def test_answer_then_unclosed(self):
        answer, think = split_think("<think>A</think>回答<think>切れた思考")
        self.assertEqual(answer, "回答")
        self.assertIn("A", think)
        self.assertIn("切れた思考", think)

    def test_strip_think_hides_unclosed(self):
        self.assertEqual(strip_think("<think>漏れてはいけない"), "")
        self.assertEqual(strip_think("<think>x</think>回答"), "回答")


if __name__ == "__main__":
    unittest.main()
