"""GraphRAG エンジンのユニットテスト（フェイク LLM・HTTP なし）。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mock_llm import FakeLLMClient  # noqa: E402
from ragcore.engines.base import EngineContext  # noqa: E402
from ragcore.engines.graphrag import GraphRAGEngine, label_propagation  # noqa: E402

CORPUS = [
    {"id": "d1", "title": "アルファ社について",
     "text": "アルファ社はベータ製品を開発している。アルファ社はガンマ研究所と共同研究を行う。"},
    {"id": "d2", "title": "ベータ製品の詳細",
     "text": "ベータ製品はアルファ社の主力製品である。ガンマ研究所が評価を担当した。"},
]

CFG = {"base_url": "http://mock/v1", "api_key": "", "model": "mock",
       "embed_model": "", "embed_base_url": "", "embed_api_key": "",
       "context_window": 8192, "request_timeout": 30.0, "max_tokens": 800,
       "use_proxy": False, "proxy_url": ""}


def make_ctx(with_embeddings: bool = True) -> EngineContext:
    return EngineContext(llm=FakeLLMClient(with_embeddings=with_embeddings), cfg=CFG)


class TestLabelPropagation(unittest.TestCase):
    def test_two_clusters(self):
        nodes = ["a", "b", "c", "x", "y", "z"]
        edges = {("a", "b"): 5.0, ("b", "c"): 5.0, ("a", "c"): 5.0,
                 ("x", "y"): 5.0, ("y", "z"): 5.0, ("x", "z"): 5.0}
        labels = label_propagation(nodes, edges)
        self.assertEqual(labels["a"], labels["b"])
        self.assertEqual(labels["b"], labels["c"])
        self.assertEqual(labels["x"], labels["y"])
        self.assertNotEqual(labels["a"], labels["x"])

    def test_deterministic(self):
        nodes = list("abcdef")
        edges = {("a", "b"): 1.0, ("c", "d"): 2.0, ("e", "f"): 3.0}
        self.assertEqual(label_propagation(nodes, edges),
                         label_propagation(list(reversed(nodes)), edges))

    def test_isolated_nodes(self):
        labels = label_propagation(["a", "b"], {})
        self.assertNotEqual(labels["a"], labels["b"])


class TestGraphRAGIngest(unittest.TestCase):
    def setUp(self):
        self.engine = GraphRAGEngine()
        self.ctx = make_ctx()
        self.index = self.engine.ingest(CORPUS, self.ctx)

    def test_entities_merged_across_chunks(self):
        names = {e["name"] for e in self.index["entities"]}
        self.assertIn("アルファ社", names)
        self.assertIn("ベータ製品", names)
        self.assertIn("ガンマ研究所", names)
        # 2 文書に登場するエンティティは1つにマージされる
        alpha = [e for e in self.index["entities"] if e["name"] == "アルファ社"]
        self.assertEqual(len(alpha), 1)
        self.assertGreaterEqual(len(alpha[0]["chunks"]), 2)

    def test_relations_and_communities(self):
        self.assertTrue(self.index["relations"])
        self.assertTrue(self.index["communities"])
        top = self.index["communities"][0]
        self.assertGreaterEqual(top["size"], 2)
        self.assertTrue(top["title"])
        self.assertTrue(top["summary"])

    def test_embeddings_present(self):
        self.assertIsNotNone(self.index["entity_vecs"])
        self.assertEqual(len(self.index["entity_vecs"]), len(self.index["entities"]))
        self.assertEqual(len(self.index["chunk_vecs"]), len(self.index["chunks"]))

    def test_index_is_json_serializable(self):
        import json
        json.dumps(self.index, ensure_ascii=False)

    def test_stats(self):
        stats = self.index["stats"]
        self.assertEqual(stats["entities"], len(self.index["entities"]))
        self.assertTrue(stats["has_embeddings"])


class TestGraphRAGIngestNoEmbeddings(unittest.TestCase):
    def test_ingest_without_embeddings(self):
        engine = GraphRAGEngine()
        ctx = make_ctx(with_embeddings=False)
        index = engine.ingest(CORPUS, ctx)
        self.assertIsNone(index["entity_vecs"])
        self.assertFalse(index["stats"]["has_embeddings"])
        # 埋め込みなしでも local 検索は BM25 フォールバックで動く
        result = engine.query(index, "アルファ社について教えて", "local", ctx)
        self.assertTrue(result["answer"])
        self.assertEqual(result["mode"], "local")


class TestGraphRAGQuery(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = GraphRAGEngine()
        cls.ctx = make_ctx()
        cls.index = cls.engine.ingest(CORPUS, cls.ctx)

    def test_global_query(self):
        result = self.engine.query(self.index, "全体としてどんな内容？", "global", self.ctx)
        self.assertEqual(result["mode"], "global")
        self.assertIn("統合回答", result["answer"])
        self.assertTrue(any(c["type"] == "community" for c in result["citations"]))

    def test_local_query(self):
        result = self.engine.query(self.index, "ベータ製品とは？", "local", self.ctx)
        self.assertEqual(result["mode"], "local")
        self.assertTrue(result["answer"])
        types = {c["type"] for c in result["citations"]}
        self.assertIn("entity", types)
        self.assertIn("chunk", types)

    def test_auto_mode_routing(self):
        # 既知エンティティ名を含む質問 → local
        result = self.engine.query(self.index, "アルファ社の主力製品は？", "auto", self.ctx)
        self.assertEqual(result["mode"], "local")
        # 含まない全体質問 → global
        result = self.engine.query(self.index, "資料全体の要点をまとめて", "auto", self.ctx)
        self.assertEqual(result["mode"], "global")


if __name__ == "__main__":
    unittest.main()
