"""GraphRAG エンジンのユニットテスト（フェイク LLM・HTTP なし）。"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mock_llm import FakeLLMClient, mock_chat_response  # noqa: E402
from ragcore.engines.base import EngineContext  # noqa: E402
from ragcore.engines.graphrag import (GraphRAGEngine, _norm_key,  # noqa: E402
                                      label_propagation)

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


class TransformLLM(FakeLLMClient):
    """モック応答を劣化・加工して現実のLLMの振る舞いを再現するフェイク。"""

    def __init__(self, transform, **kw):
        super().__init__(**kw)
        self._transform = transform

    def chat(self, prompt, *, system="", max_tokens=None, temperature=0.0,
             want_think=False):
        self.stats["chat_calls"] += 1
        text = self._transform(prompt, mock_chat_response(prompt))
        return (text, "（テスト思考）") if want_think else text


class TestNormKey(unittest.TestCase):
    def test_corporate_forms_merged(self):
        self.assertEqual(_norm_key("株式会社青嶺製作所"), _norm_key("青嶺製作所"))
        self.assertEqual(_norm_key("青嶺製作所 株式会社"), _norm_key("青嶺製作所"))
        self.assertEqual(_norm_key("㈱青嶺製作所"), _norm_key("青嶺製作所"))
        self.assertEqual(_norm_key("Aomine Inc."), _norm_key("aomine"))

    def test_nfkc_and_whitespace(self):
        self.assertEqual(_norm_key("ＳｋｙＥｄｇｅ"), _norm_key("SkyEdge"))
        self.assertEqual(_norm_key("Sky Edge"), _norm_key("SkyEdge"))

    def test_pure_corporate_name_survives(self):
        self.assertTrue(_norm_key("株式会社"))   # 全部剥がれても空にならない


class TestAuditRegressions(unittest.TestCase):
    """監査で確認した「情報がみつからない」への各経路の回帰テスト。"""

    def test_ingest_raises_when_no_entities(self):
        # 抽出が全チャンクで JSON にならない（think でトークン切れ等）→ 沈黙せずエラー
        llm = TransformLLM(lambda p, clean: "エンティティを整理すると…（JSONなし）"
                           if "[TASK:graph_extract]" in p else clean)
        ctx = EngineContext(llm=llm, cfg=CFG)
        with self.assertRaises(ValueError) as cm:
            GraphRAGEngine().ingest(CORPUS, ctx)
        self.assertIn("エンティティが1件も抽出できませんでした", str(cm.exception))
        self.assertIn("Max Tokens", str(cm.exception))

    def test_extract_retry_recovers(self):
        # 1回目は JSON 崩れ、再出力要求で正しい JSON → 構築成功 + retries 記録
        def transform(prompt, clean):
            if "[TASK:graph_extract]" in prompt and "【再出力】" not in prompt:
                return "すみません、JSONを出し忘れました。"
            return clean
        ctx = EngineContext(llm=TransformLLM(transform), cfg=CFG)
        index = GraphRAGEngine().ingest(CORPUS, ctx)
        self.assertGreater(index["stats"]["entities"], 0)
        self.assertEqual(index["stats"]["extract_parse_failures"], 0)
        self.assertEqual(index["stats"]["extract_retries"], index["stats"]["chunks"])

    def test_corporate_prefix_relations_survive(self):
        # 関係の端点だけ「株式会社」付き（表記ゆれ）でも正規化でグラフが繋がる
        def transform(prompt, clean):
            if "[TASK:graph_extract]" in prompt:
                data = json.loads(clean)
                for r in data.get("relations", []):
                    r["source"] = "株式会社" + r["source"]
                    r["target"] = "株式会社" + r["target"]
                return json.dumps(data, ensure_ascii=False)
            return clean
        ctx = EngineContext(llm=TransformLLM(transform), cfg=CFG)
        index = GraphRAGEngine().ingest(CORPUS, ctx)
        self.assertGreater(index["stats"]["relations"], 0)
        self.assertGreaterEqual(index["stats"]["summarized_communities"], 1)
        self.assertEqual(index["warnings"], [])

    def test_singleton_summaries_and_warning_when_no_relations(self):
        # 関係が全滅しても、単独コミュニティに要約が付き global が答えられる + 警告
        def transform(prompt, clean):
            if "[TASK:graph_extract]" in prompt:
                data = json.loads(clean)
                data["relations"] = []
                return json.dumps(data, ensure_ascii=False)
            return clean
        engine = GraphRAGEngine()
        ctx = EngineContext(llm=TransformLLM(transform), cfg=CFG)
        index = engine.ingest(CORPUS, ctx)
        self.assertTrue(any("関係が1件も" in w for w in index["warnings"]))
        self.assertTrue(all(c["summary"] for c in index["communities"]))
        result = engine.query(index, "全体を要約して", "global", ctx)
        self.assertNotIn("見つかりませんでした", result["answer"])

    def test_global_fallback_when_map_returns_prose(self):
        # インデックスは健全でも map が散文を返す → チャンク直接回答へフォールバック
        engine = GraphRAGEngine()
        ctx = EngineContext(llm=FakeLLMClient(), cfg=CFG)
        index = engine.ingest(CORPUS, ctx)
        broken_map = TransformLLM(lambda p, clean: "重要度は高いと思います。"
                                  if "[TASK:global_map]" in p else clean)
        qctx = EngineContext(llm=broken_map, cfg=CFG)
        result = engine.query(index, "全体を要約して", "global", qctx)
        self.assertNotIn("見つかりませんでした", result["answer"])
        self.assertEqual(result["mode"], "global(チャンク直接)")
        self.assertTrue(result["stats"].get("fallback"))
        self.assertTrue(any(c["type"] == "chunk" for c in result["citations"]))

    def test_global_lenient_score_parsing(self):
        # map の score が "高" のような文字列でもポイントとして拾える
        engine = GraphRAGEngine()
        ctx = EngineContext(llm=FakeLLMClient(), cfg=CFG)
        index = engine.ingest(CORPUS, ctx)

        def transform(prompt, clean):
            if "[TASK:global_map]" in prompt:
                return json.dumps({"points": [{"text": "ポイント", "score": "高"}]},
                                  ensure_ascii=False)
            return clean
        qctx = EngineContext(llm=TransformLLM(transform), cfg=CFG)
        result = engine.query(index, "全体を要約して", "global", qctx)
        self.assertEqual(result["mode"], "global")   # フォールバックせず通常経路
        self.assertGreater(result["stats"]["points"], 0)


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
