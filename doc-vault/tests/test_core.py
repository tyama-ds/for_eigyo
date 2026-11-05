"""コア部品のユニットテスト（テキスト処理・設定・LLM ポリシー・Claude 呼び出し形・グラフ・ルーター）。"""
from __future__ import annotations

import json
import re
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


class TestDecisionRAG(unittest.TestCase):
    """DecisionRAG（arXiv:2610.35868）: 階層の構築・確率での採点・ナビゲーション。"""

    def setUp(self):
        from docvault import decision
        self.decision = decision
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        db.set_data_dir(Path(self.tmp.name))

    def tearDown(self):
        db.close()
        self.tmp.cleanup()

    def add_doc(self, did, title, folder, texts, sens="internal", heading=""):
        db.write("INSERT INTO documents(id,title,filename,ext,sha256,folder,sensitivity,status,chars,"
                 "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,0,0)",
                 (did, title, f"{did}.md", ".md", did, folder, sens, "ready", sum(map(len, texts))))
        db.executemany("INSERT INTO chunks(id,doc_id,ord,locator,heading,page,text) VALUES(?,?,?,?,?,?,?)",
                       [(f"{did}#{i}", did, i, f"p.{i + 1}", heading, i + 1, t)
                        for i, t in enumerate(texts)])
        db.bump_revision()

    def test_soft_scores_variants(self):
        lp = [{"token": "<think>", "logprob": 0}, {"token": "1=3", "logprob": 0},
              {"token": "</think>", "logprob": 0},
              {"token": "1", "logprob": 0}, {"token": "=", "logprob": 0},
              {"token": "9", "logprob": -0.1, "top_logprobs": [{"token": "9", "logprob": -0.1},
                                                              {"token": "0", "logprob": -2.4}]},
              {"token": "\n", "logprob": 0}, {"token": "2", "logprob": 0},
              {"token": "= 4", "logprob": 0, "top_logprobs": []}]
        out = self.decision.soft_scores(lp)
        self.assertGreater(out[1], 0.85)                      # think 内の 1=3 は無視
        self.assertAlmostEqual(out[2], 4 / 9)
        self.assertEqual(self.decision.hard_scores("<think>1=0</think>\n1=9\n[2]: 3"),
                         {1: 1.0, 2: 3 / 9})

    def test_tree_structure_and_split(self):
        self.add_doc("a", "品質会議 議事録", "社内/品質", ["熱処理炉の温度を交換した。"] * 3)
        self.add_doc("b", "不具合集計", "社内/品質", ["ネジ緩みの件数。"])
        self.add_doc("c", "会社概要", "公開", ["創業は1987年。"], sens="public")
        long = [f"第{i}節の本文。熱処理炉と硬度の記述。" for i in range(30)]
        self.add_doc("d", "長い報告書", "", long)
        tree = self.decision.build_tree(max_children=4)
        root = tree["root"]
        kinds = {tree[c]["kind"] for c in root["children"]}
        self.assertIn("group", kinds)                         # フォルダ
        self.assertIn("doc", kinds)                           # フォルダなしの文書は直下
        self.assertEqual(root["docs"], {"a", "b", "c", "d"})
        self.assertEqual(tree["d:c"]["sens"], "public")
        self.assertEqual(root["sens"], "internal")
        # 「社内」は子が「品質」1つだけ → 飛ばして文書が直接並ぶ
        shanai = tree["f:社内"]
        self.assertEqual({tree[c]["kind"] for c in shanai["children"]}, {"doc"})
        # 長い文書は節に分かれ、節が多すぎれば中間ノードにまとめる
        d = tree["d:d"]
        self.assertLessEqual(len(d["children"]), 4)
        self.assertEqual({tree[c]["kind"] for c in d["children"]}, {"group"})

    def test_navigate_finds_passage_within_budget(self):
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import mock_llm

        class FakeLLM:
            def __init__(self):
                self.calls = []

            def chat_logprobs(self, profile, prompt, *, sensitivity, **kw):
                self.calls.append((profile, sensitivity))
                text = mock_llm.decide(prompt)
                return text, mock_llm.decide_logprobs(text)

        class FakeCtx:
            touched = False

            def __init__(self, ready=True):
                self.llm = FakeLLM()
                self.ready = ready

            def profile(self, kind, sens):
                if not self.ready:
                    return None
                return "local" if sens != "public" else "cloud"

        self.add_doc("a", "品質会議 議事録", "社内/品質",
                     ["出席者の一覧。", "熱処理炉の温度センサーを9月10日に交換した。", "次回の予定。"])
        db.update_doc("a", summary="熱処理炉の温度ばらつき対策（温度センサーの交換）を報告した議事録。",
                      keywords=["熱処理炉", "温度センサー"])
        self.add_doc("b", "会社概要", "公開", ["創業は1987年。代表は早瀬凛。"], sens="public")
        for i in range(6):
            self.add_doc(f"x{i}", f"無関係な資料{i}", "その他", [f"営業日報その{i}。"])
        ctx = FakeCtx()
        res = self.decision.navigate(ctx, "熱処理炉の温度センサーはいつ交換した？",
                                     {"a", "b", "x0", "x1", "x2", "x3", "x4", "x5"},
                                     k=4, beam=2, budget=5)
        self.assertIsNone(res["error"])
        self.assertTrue(res["soft"])
        self.assertEqual(res["hits"][0][0], "a#1")
        self.assertLessEqual(res["calls"], 5)
        self.assertGreaterEqual(len(res["path"]), 2)
        # 社外秘を含む候補の判定はローカルだけ
        self.assertTrue(all(p == "local" for p, s in ctx.llm.calls if s == "internal"))
        # 許可されていない文書へは降りない
        res2 = self.decision.navigate(ctx, "熱処理炉の温度センサー", {"b"}, k=4)
        self.assertFalse(any(cid.startswith("a#") for cid, _ in res2["hits"]))
        # LLM が無ければ判定せず理由を返す
        res3 = self.decision.navigate(FakeCtx(ready=False), "熱処理炉", {"a"}, k=4)
        self.assertEqual(res3["hits"], [])
        self.assertIn("LLM", res3["error"])

    def test_chat_strategy_migration(self):
        import sqlite3
        path = Path(self.tmp.name) / "old"
        path.mkdir()
        c = sqlite3.connect(str(path / "docvault.sqlite3"))
        c.execute("CREATE TABLE chats (id TEXT PRIMARY KEY, title TEXT NOT NULL DEFAULT '', owner TEXT "
                  "NOT NULL DEFAULT 'local', mode TEXT NOT NULL DEFAULT 'auto', scope TEXT NOT NULL "
                  "DEFAULT '{}', touched_internal INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL,"
                  " updated_at REAL NOT NULL)")
        c.execute("INSERT INTO chats(id,created_at,updated_at) VALUES('old',0,0)")
        c.commit()
        c.close()
        db.set_data_dir(path)
        self.assertEqual(db.get_chat("old")["strategy"], "auto")       # 古い DB に列を追加
        self.assertEqual(db.get_chat("old")["decision_beam"], 0)
        self.assertEqual(db.create_chat(strategy="decision")["strategy"], "decision")


class TestDecisionAPI(unittest.TestCase):
    """判定 API（Jev / OpenAI / Azure OpenAI の Decisions API）の要求・応答・送り先・送信ポリシー。

    OpenAI / Azure の形は公式 Python SDK（openai 3.46 の DecisionCreateParams / Decision）に合わせている。
    """

    def setUp(self):
        from docvault import decision_api
        self.da = decision_api

    def eff(self, provider, **sub):
        return self.da.effective({"decision_api": {"provider": provider, provider: sub}})

    def test_openrouter_request_shapes_and_parsing(self):
        da = self.da
        body = da.build_binary_request("openrouter", "typesafe/jev-1.14", "質問", ["候補A", "候補B", "候補C"])
        self.assertEqual(set(body["questions"]), {"c1", "c2", "c3"})
        self.assertIn("1. 候補A\n2. 候補B", body["state"]["candidates"])     # 共有の文脈
        q = body["questions"]["c2"]
        self.assertEqual(q["type"], "noul")
        self.assertEqual(set(q["criteria"]), {"true", "false"})
        self.assertIn("candidate 2", q["instructions"])
        ch = da.build_choice_request("openrouter", "m", "質問", ["x", "y"])
        self.assertEqual(set(ch["questions"]["most_relevant"]["criteria"]), {"c1", "c2"})
        self.assertEqual(da.parse_binary("openrouter", {"answers": {"c1": {"type": "noul", "noul": 0.9},
                                                                    "c2": {"type": "noul", "noul": 1.4}}}, 2),
                         ({1: 0.9, 2: 1.0}, 0))
        with self.assertRaises(LLMError):
            da.parse_binary("openrouter", {"answers": {"c1": {"noul": 0.5}}}, 2)
        self.assertEqual(da.parse_choice("openrouter", {"answers": {"most_relevant": {
            "choice": "c2", "probabilities": {"c1": 0.2, "c2": 0.8}}}}, 2), {1: 0.2, 2: 0.8})

    def test_openai_request_shapes(self):
        da = self.da
        for provider in ("openai", "azure"):
            body = da.build_binary_request(provider, "gpt-6-luna", "質問", ["候補A", "候補B", "候補C"])
            self.assertEqual(set(body), {"model", "input", "questions"})
            self.assertIn("Query:\n質問", body["input"])
            self.assertIn("1. 候補A\n2. 候補B\n3. 候補C", body["input"])    # 候補一覧を共有の入力に
            self.assertEqual([q["name"] for q in body["questions"]], ["c1", "c2", "c3"])
            self.assertEqual({q["type"] for q in body["questions"]}, {"predicate"})
            self.assertIn("Candidate 2", body["questions"][1]["instructions"])
            ch = da.build_choice_request(provider, "gpt-6-luna", "質問", ["x" * 900, "y"])["questions"][0]
            self.assertEqual(ch["type"], "choice")
            self.assertEqual([c["value"] for c in ch["choices"]], ["c1", "c2"])
            self.assertEqual(len(ch["choices"][0]["description"]), 600)

    def test_openai_answer_parsing(self):
        da = self.da
        resp = {"answers": [{"type": "predicate", "name": "c2", "probability": 0.25},   # 順不同でも名前で
                            {"type": "predicate", "name": "c1", "probability": 1.3},
                            {"type": "refusal", "name": "c3"}],
                "usage": {"input_tokens": 10}}
        self.assertEqual(da.parse_binary("openai", resp, 3), ({1: 1.0, 2: 0.25, 3: 0.0}, 1))
        unnamed = {"answers": [{"type": "predicate", "probability": 0.4}, {"type": "predicate", "probability": 0.6}]}
        self.assertEqual(da.parse_binary("openai", unnamed, 2)[0], {1: 0.4, 2: 0.6})
        with self.assertRaises(LLMError):                       # 全部断られたら判定に使えない
            da.parse_binary("openai", {"answers": [{"type": "refusal", "name": "c1"}]}, 1)
        with self.assertRaises(LLMError):
            da.parse_binary("openai", {"answers": [{"type": "predicate", "name": "c1"}]}, 1)
        with self.assertRaises(LLMError):
            da.parse_binary("openai", {"error": "x"}, 1)
        choice = {"answers": [{"type": "choice", "name": "most_relevant", "choice": "c2", "confidence": 0.7,
                               "probabilities": [{"value": "c1", "probability": 0.3},
                                                 {"value": "c2", "probability": 0.7}]}]}
        self.assertEqual(da.parse_choice("azure", choice, 2), {1: 0.3, 2: 0.7})
        only_choice = {"answers": [{"type": "choice", "name": "most_relevant", "choice": "c2",
                                    "probabilities": []}]}
        self.assertEqual(da.parse_choice("openai", only_choice, 2), {1: 0.0, 2: 1.0})
        with self.assertRaises(LLMError):
            da.parse_choice("openai", {"answers": [{"type": "refusal", "name": "most_relevant"}]}, 2)

    def test_urls_and_headers(self):
        da = self.da
        e = self.eff("openai", api_key="sk")
        self.assertEqual(da.request_url(e), "https://api.openai.com/v1/decisions")
        self.assertEqual(e["model"], "gpt-6-luna")
        self.assertEqual(da.request_headers(e)["Authorization"], "Bearer sk")
        self.assertEqual(da.request_url(self.eff("openrouter")), "https://openrouter.ai/api/alpha/decisions")
        # Azure: リソースの URL から組み立てる（公式 SDK の AzureOpenAI / v1 API と同じ送り先）
        cases = {
            ("https://res.openai.azure.com", ""): "https://res.openai.azure.com/openai/v1/decisions",
            ("https://res.openai.azure.com/", ""): "https://res.openai.azure.com/openai/v1/decisions",
            ("https://res.openai.azure.com/openai/v1/", ""): "https://res.openai.azure.com/openai/v1/decisions",
            ("https://res.openai.azure.com", "2026-10-01-preview"):
                "https://res.openai.azure.com/openai/decisions?api-version=2026-10-01-preview",
            ("https://gw.example.com/x/decisions", ""): "https://gw.example.com/x/decisions",
            ("https://gw.example.com/x/decisions?a=1", "v"): "https://gw.example.com/x/decisions?a=1&api-version=v",
        }
        for (endpoint, ver), url in cases.items():
            e = self.eff("azure", endpoint=endpoint, api_version=ver, api_key="az", model="luna")
            self.assertEqual(da.request_url(e), url, (endpoint, ver))
        h = da.request_headers(e)
        self.assertEqual(h["api-key"], "az")
        self.assertNotIn("Authorization", h)

    def test_ready_and_missing(self):
        da = self.da
        self.assertFalse(da.ready({"decision_api": {"provider": "openai", "openai": {}}}))
        self.assertTrue(da.ready({"decision_api": {"provider": "openai", "openai": {"api_key": "k"}}}))
        az = {"decision_api": {"provider": "azure", "azure": {"api_key": "k"}}}
        self.assertFalse(da.ready(az))                          # Azure はエンドポイントとデプロイ名が要る
        self.assertEqual(da.missing(az), "エンドポイント・デプロイ名")

    def test_policy_and_split(self):
        da = self.da
        for provider in ("openrouter", "openai", "azure"):
            client = da.DecisionAPIClient(self.eff(provider, endpoint="http://127.0.0.1:9/x", api_key="k",
                                                   model="m", use_proxy=False))
            with self.assertRaises(PolicyError):
                client.post({}, sensitivity="internal")
            sent = []

            def fake_post(body, *, sensitivity, provider=provider):
                sent.append(body)
                if provider == "openrouter":
                    return {"answers": {n: {"type": "noul", "noul": 0.5} for n in body["questions"]}}
                return {"answers": [{"type": "predicate", "name": q["name"], "probability": 0.5}
                                    for q in body["questions"]]}
            client.post = fake_post
            out = client.binary("q", [f"d{i}" for i in range(120)], sensitivity="public")
            self.assertEqual([len(b["questions"]) for b in sent], [50, 50, 20])
            self.assertEqual(len(out), 120)
            self.assertEqual(client.choice("q", ["only"], sensitivity="public"), {1: 1.0})   # 1件なら聞かない
            self.assertEqual(len(sent), 3)

    def test_azure_allow_internal(self):
        da = self.da
        off = da.DecisionAPIClient(self.eff("azure", endpoint="https://r", api_key="k", model="m"))
        on = da.DecisionAPIClient(self.eff("azure", endpoint="https://r", api_key="k", model="m",
                                           allow_internal=True))
        self.assertFalse(off.trusted)
        self.assertTrue(on.trusted)
        self.assertTrue(on.allows("internal"))
        # OpenAI / OpenRouter は設定に allow_internal があっても社外秘を送らない
        oa = da.DecisionAPIClient(self.eff("openai", api_key="k", allow_internal=True))
        self.assertFalse(oa.trusted)
        self.assertFalse(oa.allows("internal"))

    def test_pick_judge(self):
        from docvault import decision
        da = self.da

        class Ctx:
            def __init__(self, mode, touched):
                self.mode, self.touched = mode, touched

            def profile(self, purpose, sensitivity):
                return None                                    # LLM は未設定
        oa = da.DecisionAPIClient(self.eff("openai", api_key="k"))
        az = da.DecisionAPIClient(self.eff("azure", endpoint="https://r", api_key="k", model="m",
                                           allow_internal=True))
        self.assertEqual(decision.pick_judge(Ctx("auto", False), "public", oa).name, "openai")
        self.assertIsNone(decision.pick_judge(Ctx("auto", False), "internal", oa))
        self.assertIsNone(decision.pick_judge(Ctx("local", False), "public", oa))   # ローカルのみの相談
        self.assertIsNone(decision.pick_judge(Ctx("auto", True), "public", oa))     # 社外秘に触れた相談
        j = decision.pick_judge(Ctx("local", True), "internal", az)
        self.assertEqual((j.name, j.sensitivity), ("azure", "internal"))

    def test_config_migrates_old_jev_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            saved = config.CONFIG_FILE
            config.CONFIG_FILE = Path(tmp) / "c.json"
            try:
                config.CONFIG_FILE.write_text(json.dumps({
                    "jev": {"endpoint": "https://openrouter.ai/api/alpha/decisions", "api_key": "or-old",
                            "model": "typesafe/jev-1.13", "timeout": 30, "use_proxy": False},
                    "rag": {"decision_judge": "jev"}}), encoding="utf-8")
                cfg = config.load_config()
                self.assertEqual(cfg["rag"]["decision_judge"], "api")
                self.assertEqual(cfg["decision_api"]["provider"], "openrouter")
                self.assertEqual(cfg["decision_api"]["timeout"], 30.0)
                sub = cfg["decision_api"]["openrouter"]
                self.assertEqual((sub["api_key"], sub["model"], sub["use_proxy"]), ("or-old", "typesafe/jev-1.13", False))
                cfg = config.save_config({"rag": {"decision_judge": "jev"},     # 古い値も受け付ける
                                          "decision_api": {"provider": "azure", "azure": {
                                              "endpoint": " https://r ", "api_key": "az", "model": "luna",
                                              "allow_internal": True}, "openai": {"allow_internal": True}}})
                self.assertEqual(cfg["rag"]["decision_judge"], "api")
                self.assertEqual(cfg["decision_api"]["azure"]["endpoint"], "https://r")
                self.assertTrue(cfg["decision_api"]["azure"]["allow_internal"])
                self.assertNotIn("allow_internal", cfg["decision_api"]["openai"])
                saved_json = json.loads(config.CONFIG_FILE.read_text(encoding="utf-8"))
                self.assertNotIn("jev", saved_json)                 # 保存し直すと古いブロックは消える
                pub = config.public_config(cfg)
                self.assertTrue(pub["decision_api"]["azure"]["has_api_key"])
                self.assertNotIn("api_key", pub["decision_api"]["azure"])
                self.assertNotIn("api_key", pub["decision_api"]["openrouter"])
            finally:
                config.CONFIG_FILE = saved

    def test_llm_choice_probs(self):
        from docvault.decision import choice_probs
        lp = [{"token": "<think>A</think>"}, {"token": "B", "logprob": -0.2,
                                               "top_logprobs": [{"token": "B", "logprob": -0.2},
                                                                {"token": "A", "logprob": -1.8},
                                                                {"token": "Z", "logprob": -3}]}]
        probs = choice_probs(lp, 3)
        self.assertGreater(probs[1], probs[0])
        self.assertAlmostEqual(sum(probs.values()), 1.0)
        self.assertNotIn(25, probs)


class TestBeamSearch(unittest.TestCase):
    """DecisionRAG のビームサーチ: 経路スコア・ビーム幅・バックトラック・判定回数の上限。"""

    def setUp(self):
        from docvault import decision
        self.decision = decision
        self.saved = decision.get_tree

    def tearDown(self):
        self.decision.get_tree = self.saved

    def make(self, spec):
        """spec: {id: (kind, digit, [children])}。説明文は id そのもの。"""
        tree = {}
        for nid, (kind, _, kids) in spec.items():
            tree[nid] = {"id": nid, "kind": kind, "title": nid, "desc": nid, "children": kids,
                         "docs": {"d"}, "sens": "internal"}
            if kind == "chunk":
                tree[nid]["chunk_id"] = nid
        tree["root"] = {"id": "root", "kind": "group", "title": "root", "desc": "",
                        "children": [n for n in spec if not any(n in v[2] for v in spec.values())],
                        "docs": {"d"}, "sens": "internal"}
        digits = {nid: d for nid, (_, d, _) in spec.items()}
        self.decision.get_tree = lambda: tree
        test = self

        class FakeLLM:
            calls = []

            def chat_logprobs(self, profile, prompt, **kw):
                lines = re.findall(r"^\[(\d+)\] （.+?）(.*)$", prompt, re.MULTILINE)
                test.assertTrue(lines)
                self.calls.append([d for _, d in lines])
                return "\n".join(f"{i}={digits[d]}" for i, d in lines), None

        class Ctx:
            touched, mode = False, "local"
            llm = FakeLLM()

            def profile(self, kind, sens):
                return "local"
        return Ctx()

    def test_path_score_beats_own_probability(self):
        # A(0.89)>B(0.33)。B の子 b1 は単独では a1/a2 より高いが、経路スコアでは下回る
        ctx = self.make({"A": ("group", 8, ["a1", "a2"]), "B": ("group", 3, ["b1"]),
                         "a1": ("doc", 6, ["ca1"]), "a2": ("doc", 5, ["ca2"]),
                         "b1": ("doc", 7, ["cb1"]),
                         "ca1": ("chunk", 9, []), "ca2": ("chunk", 9, []), "cb1": ("chunk", 9, [])})
        res = self.decision.navigate(ctx, "q", {"d"}, k=2, beam=2, use_choice=False)
        self.assertEqual([c for c, _ in res["hits"]], ["ca1", "ca2"])   # 経路スコア順
        self.assertEqual(res["calls"], 3)       # 上位2件より良くなり得ない b1 は展開しない
        self.assertEqual([x["title"] for x in res["path"][1]["kept"]], ["a1", "a2"])
        self.assertTrue(all(len(lv["kept"]) <= 2 for lv in res["path"]))
        # k を増やすと、見送った B の枝へ戻って段落を探す
        res = self.decision.navigate(ctx, "q", {"d"}, k=8, beam=2, use_choice=False)
        self.assertEqual([c for c, _ in res["hits"]], ["ca1", "ca2", "cb1"])

    def test_backtracks_to_runner_up(self):
        # ビーム幅1。A を選んだが配下が外れ（確率 0.0x）→ 前の段の次点 B に戻る
        ctx = self.make({"A": ("group", 8, ["a1"]), "B": ("group", 5, ["b1"]),
                         "a1": ("doc", 0, ["ca1"]), "b1": ("doc", 8, ["cb1"]),
                         "ca1": ("chunk", 9, []), "cb1": ("chunk", 9, [])})
        res = self.decision.navigate(ctx, "q", {"d"}, k=2, beam=1, use_choice=False)
        self.assertEqual(res["hits"][0][0], "cb1")
        self.assertNotIn("ca1", [c for c, _ in res["hits"]])
        self.assertEqual(sum(lv["backtracked"] for lv in res["path"]), 1)
        self.assertTrue(all(len(lv["kept"]) <= 1 for lv in res["path"]))

    def test_budget_trims_visibly(self):
        spec = {f"x{i}": ("chunk", 5, []) for i in range(70)}
        ctx = self.make(spec)
        res = self.decision.navigate(ctx, "q", {"d"}, k=5, beam=4, budget=2, use_choice=False)
        self.assertEqual(res["calls"], 2)
        self.assertEqual(res["path"][0]["candidates"], 60)          # 30件×2回まで
        self.assertEqual(res["path"][0]["trimmed"], 10)             # 判定できなかった分を記録


class TestProxyRouting(unittest.TestCase):
    """Claude SDK の HTTP クライアントをプロキシ設定どおりに作る（SDK は環境変数のプロキシを常に組み込むため）。

    実 SDK（anthropic 2.5）と記録用プロキシでの通信確認は手元で実施済み。ここでは CI 用に偽の SDK で
    組み立て方だけを確認する。
    """

    def setUp(self):
        import urllib.request
        from docvault import llm as llm_mod
        self.llm_mod = llm_mod
        self.saved_getproxies = urllib.request.getproxies
        self.saved_modules = {k: sys.modules.get(k) for k in ("httpx2",)}
        self.calls = []
        test = self

        class FakeTransport:
            def __init__(self, **kw):
                self.kw = kw

        fake_hx = types.ModuleType("httpx2")
        fake_hx.HTTPTransport = FakeTransport
        sys.modules["httpx2"] = fake_hx
        self.fake_anthropic = types.SimpleNamespace(
            DefaultHttpxClient=lambda **kw: test.calls.append(kw) or kw)

    def tearDown(self):
        import urllib.request
        urllib.request.getproxies = self.saved_getproxies
        for k, v in self.saved_modules.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v

    def build(self, **p):
        return self.llm_mod.anthropic_http_client(self.fake_anthropic, p)

    def test_off_overrides_environment_proxies(self):
        import ssl
        kw = self.build(use_proxy=False)
        self.assertEqual(kw["mounts"], {"http://": None, "https://": None, "all://": None})
        self.assertIsInstance(kw["verify"], ssl.SSLContext)     # OS の証明書ストアも信頼

    def test_explicit_proxy(self):
        kw = self.build(use_proxy=True, proxy_url="http://proxy.example:8080")
        self.assertEqual(set(kw["mounts"]), {"http://", "https://", "all://"})
        self.assertTrue(all(t.kw["proxy"] == "http://proxy.example:8080" for t in kw["mounts"].values()))

    def test_system_proxy_when_blank(self):
        import urllib.request
        urllib.request.getproxies = lambda: {"https": "http://winproxy:3128"}   # Windows の設定を再現
        kw = self.build(use_proxy=True, proxy_url="")
        self.assertTrue(all(t.kw["proxy"] == "http://winproxy:3128" for t in kw["mounts"].values()))
        urllib.request.getproxies = lambda: {}
        kw = self.build(use_proxy=True, proxy_url="")
        self.assertNotIn("mounts", kw)                            # 何も無ければ SDK の既定
