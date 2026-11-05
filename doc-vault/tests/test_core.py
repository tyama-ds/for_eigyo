"""コア部品のユニットテスト（テキスト処理・設定・LLM ポリシー・Claude 呼び出し形・グラフ・ルーター）。"""
from __future__ import annotations

import json
import sys
import tempfile
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from docvault import config, db, graph, rag  # noqa: E402
from docvault.llm import LLM, LLMError, PolicyError, _as_messages, _ThinkFilter, strip_think  # noqa: E402
from docvault.textutil import (BM25Index, VectorIndex, build_chunks, extract_json,  # noqa: E402
                               pack_vec, rrf_fuse, tokenize, unpack_vec)


class TestText(unittest.TestCase):
    def test_tokenize_cjk_and_models(self):
        toks = tokenize("SkyEdge S-2 の品質")
        self.assertIn("skyedge", toks)
        self.assertIn("s-2", toks)
        self.assertIn("品質", toks)

    def test_build_chunks_merges_short_sections_and_ranges(self):
        secs = [{"locator": f"スライド{i}", "heading": "", "page": i, "text": f"短い本文{i}"}
                for i in range(1, 4)]
        chunks = build_chunks(secs, size=900)
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0]["locator"], "スライド1-3")
        long = [{"locator": "p.1", "heading": "", "page": 1, "text": "あ。" * 800}]
        chunks = build_chunks(long, size=500, overlap=50)
        self.assertGreater(len(chunks), 2)
        self.assertTrue(all(c["locator"] == "p.1" for c in chunks))
        self.assertEqual([c["ord"] for c in chunks], list(range(len(chunks))))

    def test_bm25_add_remove(self):
        idx = BM25Index()
        idx.add("a", "熱処理炉の温度管理")
        idx.add("b", "営業部の売上報告")
        self.assertEqual(idx.search("熱処理炉")[0][0], "a")
        idx.remove("a")
        self.assertEqual(idx.search("熱処理炉"), [])
        self.assertEqual(len(idx), 1)
        self.assertEqual(idx.search("売上", allow=lambda k: k != "b"), [])

    def test_vectors_and_rrf(self):
        vi = VectorIndex()
        vi.add("x", [1.0, 0.0])
        vi.add("y", [0.0, 1.0])
        self.assertEqual(vi.search([0.9, 0.1], k=1)[0][0], "x")
        self.assertEqual(vi.search([1, 0, 0]), [])               # 次元違いは無視
        self.assertEqual(unpack_vec(pack_vec([0.5, 0.25])), [0.5, 0.25])
        fused = rrf_fuse([["a", "b"], ["b", "c"]])
        self.assertEqual(fused[0][0], "b")

    def test_extract_json_tolerant(self):
        self.assertEqual(extract_json('説明です ```json\n{"a": [1,2,],}\n```'), {"a": [1, 2]})
        self.assertEqual(extract_json("｛”a”：1｝"), {"a": 1})
        self.assertIsNone(extract_json("JSON なし"))


class TestLLMPolicyAndShapes(unittest.TestCase):
    def setUp(self):
        self.cfg = config._merge(config.DEFAULT_CONFIG, {})
        self.cfg["profiles"]["local"].update(base_url="http://127.0.0.1:9/v1", model="m")
        self.cfg["profiles"]["cloud"].update(provider="anthropic", model="claude-opus-5-5",
                                             api_key="sk-test")

    def test_policy_blocks_internal_to_cloud(self):
        llm = LLM(self.cfg)
        with self.assertRaises(PolicyError):
            llm.chat("cloud", "社外秘の本文", sensitivity="internal")
        with self.assertRaises(PolicyError):
            llm.embed("cloud", ["社外秘"], sensitivity="internal")
        with self.assertRaises(PolicyError):
            list(llm.stream("cloud", "x", sensitivity="internal"))

    def test_think_filter_streaming(self):
        f = _ThinkFilter()
        out = "".join(f.feed(p) for p in ["<thi", "nk>考え", "中</think>答", "えです"]) + f.flush()
        self.assertEqual(out, "答えです")
        self.assertEqual(strip_think("推論…</think>結論"), "結論")
        self.assertEqual(strip_think("結論<think>途中で切れ"), "結論")

    def test_messages_normalized(self):
        msgs = _as_messages([{"role": "assistant", "content": "先頭のassistant"},
                             {"role": "user", "content": "a"}, {"role": "user", "content": "b"},
                             {"role": "system", "content": "捨てる"}])
        self.assertEqual(msgs, [{"role": "user", "content": "a\n\nb"}])

    def test_anthropic_request_shape_with_fake_sdk(self):
        calls = []

        class FakeMessage:
            def __init__(self, text, stop="end_turn"):
                self.content = [types.SimpleNamespace(type="thinking", thinking=""),
                                types.SimpleNamespace(type="text", text=text)]
                self.stop_reason = stop

        class FakeStream:
            def __init__(self, kw):
                self.kw = kw
                self.text_stream = iter(["こん", "にちは"])

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def get_final_message(self):
                return FakeMessage("こんにちは")

        class FakeMessages:
            def __init__(self, beta):
                self.beta = beta

            def create(self, **kw):
                calls.append(("create", self.beta, kw))
                return FakeMessage("REFUSE" if "拒否" in kw["messages"][0]["content"] else "OK",
                                   "refusal" if "拒否" in kw["messages"][0]["content"] else "end_turn")

            def stream(self, **kw):
                calls.append(("stream", self.beta, kw))
                return FakeStream(kw)

        class FakeClient:
            def __init__(self, **kw):
                calls.append(("client", None, kw))
                self.messages = FakeMessages(False)
                self.beta = types.SimpleNamespace(messages=FakeMessages(True))

        class APIStatusError(Exception):
            status_code = 500

        fake = types.ModuleType("anthropic")
        fake.Anthropic = FakeClient
        fake.DefaultHttpxClient = lambda **kw: ("httpx", kw)
        for name in ("AuthenticationError", "RateLimitError", "APIConnectionError"):
            setattr(fake, name, type(name, (Exception,), {}))
        fake.APIStatusError = APIStatusError
        sys.modules["anthropic"] = fake
        try:
            llm = LLM(self.cfg)
            out = llm.chat("cloud", "公開情報の質問", sensitivity="public", system="SYS",
                           purpose="ingest")
            self.assertEqual(out, "OK")
            _, beta, kw = calls[-1]
            self.assertTrue(beta)                                   # fallback は beta 経由
            self.assertEqual(kw["betas"], ["server-side-fallback-2026-07-01"])
            self.assertEqual(kw["extra_body"], {"fallbacks": "default"})
            self.assertEqual(kw["system"], "SYS")
            self.assertEqual(kw["output_config"], {"effort": "low"})  # 取り込みは low
            self.assertNotIn("temperature", kw)                     # Claude には送らない
            self.assertGreaterEqual(kw["max_tokens"], 16000)
            client_kw = next(c[2] for c in calls if c[0] == "client")
            self.assertEqual(client_kw["api_key"], "sk-test")
            text = "".join(llm.stream("cloud", "質問", sensitivity="public"))
            self.assertEqual(text, "こんにちは")
            self.assertNotIn("output_config", calls[-1][2])        # 回答はモデル既定の effort
            with self.assertRaises(LLMError):
                llm.chat("cloud", "拒否されるはず", sensitivity="public")
            self.cfg["profiles"]["cloud"]["model"] = "claude-haiku-5-5"
            LLM(self.cfg).chat("cloud", "x", sensitivity="public")
            self.assertFalse(calls[-1][1])                          # fallback 非対応は通常 API
        finally:
            del sys.modules["anthropic"]

    def test_anthropic_missing_sdk_message(self):
        sys.modules["anthropic"] = None              # import 失敗を再現
        try:
            with self.assertRaises(LLMError) as cm:
                LLM(self.cfg).chat("cloud", "x", sensitivity="public")
            self.assertIn("pip install anthropic", str(cm.exception))
        finally:
            del sys.modules["anthropic"]


class TestConfig(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.old = config.CONFIG_FILE
        config.CONFIG_FILE = Path(self.tmp.name) / "c.json"

    def tearDown(self):
        config.CONFIG_FILE = self.old
        self.tmp.cleanup()

    def test_save_validates_and_hides_secrets(self):
        cfg = config.save_config({"profiles": {"cloud": {"provider": "bogus", "api_key": "k1",
                                                         "embed_provider": "anthropic",
                                                         "max_tokens": 10**9}},
                                  "policy": {"chat_mode": "evil"}, "rag": {"top_k": 1000}})
        self.assertEqual(cfg["profiles"]["cloud"]["provider"], "anthropic")  # 不正値は無視
        self.assertEqual(cfg["profiles"]["cloud"]["embed_provider"], "openai")
        self.assertEqual(cfg["profiles"]["cloud"]["max_tokens"], 128000)
        self.assertEqual(cfg["policy"]["chat_mode"], "auto")
        self.assertEqual(cfg["rag"]["top_k"], 30)
        cfg = config.save_config({"profiles": {"cloud": {"api_key": ""}}})   # 空は変更なし
        self.assertEqual(cfg["profiles"]["cloud"]["api_key"], "k1")
        pub = config.public_config(cfg)
        self.assertNotIn("k1", json.dumps(pub))
        self.assertNotIn("api_key", pub["profiles"]["cloud"])
        self.assertTrue(pub["profiles"]["cloud"]["has_api_key"])
        cfg = config.save_config({"profiles": {"cloud": {"clear_api_key": True}}})
        self.assertEqual(cfg["profiles"]["cloud"]["api_key"], "")

    def test_profile_choice_by_sensitivity(self):
        cfg = config._merge(config.DEFAULT_CONFIG, {})
        cfg["profiles"]["local"]["model"] = ""            # ローカル未設定
        self.assertIsNone(config.ingest_profile_for(cfg, "internal"))   # 社外秘はクラウドに回さない
        self.assertEqual(config.ingest_profile_for(cfg, "public"), "cloud")
        cfg["profiles"]["local"]["model"] = "m"
        cfg["policy"]["public_ingest_profile"] = "local"
        self.assertEqual(config.ingest_profile_for(cfg, "public"), "local")


class TestRouteAndProfiles(unittest.TestCase):
    def test_heuristic_route(self):
        self.assertFalse(rag.heuristic_route("ありがとう")["retrieval"])
        self.assertEqual(rag.heuristic_route("資料全体の要約を")["type"], "summary")
        self.assertEqual(rag.heuristic_route("AとBの違いは？")["type"], "compare")
        self.assertEqual(rag.heuristic_route("北浜電機と仙台工場の関係")["type"], "relation")
        self.assertEqual(rag.heuristic_route("保証期間は？")["type"], "fact")
        self.assertFalse(rag.heuristic_route("もっと短く")["retrieval"])

    def test_ctx_profile_rules(self):
        cfg = config._merge(config.DEFAULT_CONFIG, {})
        cfg["profiles"]["local"].update(model="m")
        cfg["profiles"]["cloud"].update(model="c")
        ctx = rag.Ctx({"mode": "auto"}, cfg)
        self.assertEqual(ctx.profile("answer", "internal"), "local")
        self.assertEqual(ctx.profile("answer", "public"), "cloud")
        self.assertEqual(ctx.profile("helper", "public"), "local")
        self.assertEqual(rag.Ctx({"mode": "local"}, cfg).profile("answer", "public"), "local")
        # 社外秘を含むデータは、どのモードでもローカルにしか送らない
        self.assertEqual(rag.Ctx({"mode": "cloud"}, cfg).profile("answer", "internal"), "local")
        cfg["profiles"]["local"]["model"] = ""
        self.assertIsNone(rag.Ctx({"mode": "auto"}, cfg).profile("answer", "internal"))


class TestGraph(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        db.set_data_dir(Path(self.tmp.name))

    def tearDown(self):
        db.close()
        self.tmp.cleanup()

    def test_clean_extraction_infers_endpoints(self):
        data = graph._clean_extraction({"entities": [{"name": "株式会社北浜電機", "type": "組織"}],
                                        "relations": [{"source": "北浜電機", "target": "仙台工場",
                                                       "strength": "高"}],
                                        "keywords": "品質, 製造"})
        names = {e["name"] for e in data["entities"]}
        self.assertIn("仙台工場", names)                          # 端点の補完
        self.assertEqual(data["relations"][0]["strength"], 7)
        self.assertEqual(data["keywords"], ["品質", "製造"])
        self.assertEqual(graph.norm_key("株式会社北浜電機"), graph.norm_key("北浜電機 "))

    def test_graph_ppr_and_communities(self):
        db.write("INSERT INTO documents(id,title,filename,ext,sha256,sensitivity,created_at,updated_at)"
                 " VALUES('d1','t','t.md','.md','x','public',0,0)")
        db.write("INSERT INTO documents(id,title,filename,ext,sha256,sensitivity,created_at,updated_at)"
                 " VALUES('d2','t2','t2.md','.md','y','internal',0,0)")
        graph.save_extraction("d1", "d1#0", graph._clean_extraction({
            "entities": [{"name": "A社"}, {"name": "B製品"}],
            "relations": [{"source": "A社", "target": "B製品", "strength": 8}]}))
        graph.save_extraction("d2", "d2#0", graph._clean_extraction({
            "entities": [{"name": "B製品"}, {"name": "C工場"}],
            "relations": [{"source": "B製品", "target": "C工場", "strength": 6}]}))
        sens = {"d1": "public", "d2": "internal"}
        g = graph.KnowledgeGraph(None, sens)
        self.assertEqual(g.entities[graph.norm_key("B製品")]["sensitivity"], "internal")
        rank = g.ppr(g.seed_entities("A社について", []))
        # A社 を種にすると、2ホップ先の C工場 にもスコアが届く（多段推論）
        self.assertGreater(rank.get(graph.norm_key("C工場"), 0), 0)
        top_chunks = [cid for cid, _ in g.rank_chunks(rank)]
        self.assertEqual(set(top_chunks), {"d1#0", "d2#0"})
        coms = g.communities()
        self.assertEqual(len(coms), 1)
        self.assertEqual(coms[0]["sensitivity"], "internal")
        # 公開文書だけに絞ったグラフには社外秘由来のエンティティが出ない
        gp = graph.KnowledgeGraph({"d1"}, sens)
        self.assertNotIn(graph.norm_key("C工場"), gp.entities)
        self.assertEqual(gp.entities[graph.norm_key("B製品")]["sensitivity"], "public")
        payload = g.to_payload(limit=50)
        self.assertEqual(payload["total_nodes"], 3)
        self.assertEqual(len(payload["edges"]), 2)


if __name__ == "__main__":
    unittest.main()
