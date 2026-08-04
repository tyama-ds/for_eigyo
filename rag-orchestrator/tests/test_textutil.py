"""textutil（チャンク分割 / トークン化 / BM25 / RRF / JSON 抽出）のユニットテスト。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ragcore.textutil import (BM25, cosine, extract_json, rrf_fuse,  # noqa: E402
                              split_chunks, tokenize, top_k_cosine)


class TestSplitChunks(unittest.TestCase):
    def test_short_text_single_chunk(self):
        self.assertEqual(split_chunks("こんにちは"), ["こんにちは"])

    def test_empty(self):
        self.assertEqual(split_chunks("   "), [])

    def test_long_text_respects_size(self):
        text = "\n\n".join(f"段落{i}。" + "あ" * 300 for i in range(10))
        chunks = split_chunks(text, size=800, overlap=50)
        self.assertGreater(len(chunks), 1)
        for c in chunks:
            self.assertLessEqual(len(c), 800 + 400)  # 持ち越し分の余裕
        # 内容が失われていない（各段落マーカーがどこかに存在する）
        joined = "".join(chunks)
        for i in range(10):
            self.assertIn(f"段落{i}", joined)

    def test_sentence_boundary(self):
        text = "短い文です。" * 400
        chunks = split_chunks(text, size=600, overlap=0)
        for c in chunks[:-1]:
            self.assertTrue(c.endswith("。"))


class TestTokenize(unittest.TestCase):
    def test_english_words(self):
        self.assertEqual(tokenize("Hello World 123"), ["hello", "world", "123"])

    def test_cjk_bigrams(self):
        self.assertEqual(tokenize("東京都"), ["東京", "京都"])

    def test_mixed(self):
        toks = tokenize("SkyEdge を青嶺製作所が開発")
        self.assertIn("skyedge", toks)
        self.assertIn("青嶺", toks)


class TestBM25(unittest.TestCase):
    def test_ranking(self):
        docs = ["青嶺製作所はセンサーを作る会社",
                "北浜電機は制御盤の会社",
                "今日の天気は晴れ"]
        bm25 = BM25([tokenize(d) for d in docs])
        hits = bm25.top_k("センサーの会社はどこ", k=3)
        self.assertTrue(hits)
        self.assertEqual(hits[0][0], 0)

    def test_no_match(self):
        bm25 = BM25([tokenize("apple banana")])
        self.assertEqual(bm25.top_k("ぶどう", k=3), [])

    def test_empty_corpus(self):
        bm25 = BM25([])
        self.assertEqual(bm25.top_k("query"), [])


class TestVectors(unittest.TestCase):
    def test_cosine(self):
        self.assertAlmostEqual(cosine([1, 0], [1, 0]), 1.0)
        self.assertAlmostEqual(cosine([1, 0], [0, 1]), 0.0)
        self.assertEqual(cosine([], [1]), 0.0)

    def test_top_k(self):
        vecs = [[1, 0], [0, 1], [0.9, 0.1]]
        hits = top_k_cosine([1, 0], vecs, k=2)
        self.assertEqual([i for i, _ in hits], [0, 2])

    def test_rrf(self):
        fused = rrf_fuse([[0, 1, 2], [2, 0, 1]])
        self.assertEqual(fused[0], 0)   # 両方で上位
        self.assertIn(2, fused[:2])


class TestExtractJson(unittest.TestCase):
    def test_plain(self):
        self.assertEqual(extract_json('{"a": 1}'), {"a": 1})

    def test_with_fence_and_prose(self):
        text = '説明します。\n```json\n{"entities": [{"name": "X"}]}\n```\n以上です。'
        self.assertEqual(extract_json(text), {"entities": [{"name": "X"}]})

    def test_trailing_comma(self):
        self.assertEqual(extract_json('{"a": [1, 2,],}'), {"a": [1, 2]})

    def test_nested_braces_in_string(self):
        self.assertEqual(extract_json('{"a": "文字列に } が入る"}'),
                         {"a": "文字列に } が入る"})

    def test_garbage(self):
        self.assertIsNone(extract_json("JSONはありません"))
        self.assertIsNone(extract_json(""))
        self.assertIsNone(extract_json('{"未閉じ": [1, 2'))


if __name__ == "__main__":
    unittest.main()
