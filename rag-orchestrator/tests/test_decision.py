"""DecisionRAG エンジン（arXiv:2610.35868）のテスト。

- 階層の構築（文書 → 節 → 段落、文書が多いときの文書群への分割）
- logprobs からの確率化（採点の数字・選択式の記号、思考ブロックは無視）
- ナビゲーション（検索器を使わずに目的の段落へ着く・判定回数の上限）
- 判定モデル Jev（OpenRouter Decisions API）の要求形式と、キー未設定時の LLM へのフォールバック
- API 経由の構築と質問
"""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import mock_llm  # noqa: E402
from mock_llm import FakeLLMClient, start_mock_llm  # noqa: E402
from ragcore import jev  # noqa: E402
from ragcore.engines.base import EngineContext  # noqa: E402
from ragcore.engines.decision import (DecisionRAGEngine, LLMJudge, build_tree,  # noqa: E402
                                      choice_probs, navigate, soft_scores)
from ragcore.llm import LLMError  # noqa: E402

CFG = {"base_url": "http://mock", "model": "m", "max_tokens": 4096}


def sample_corpus() -> list[dict]:
    docs = []
    for p in sorted((APP_DIR / "sample_docs").glob("*.md")):
        text = p.read_text(encoding="utf-8")
        docs.append({"id": p.stem, "title": text.splitlines()[0].lstrip("# "), "text": text})
    return docs


class TestScores(unittest.TestCase):
    def test_soft_scores_skip_think_and_read_distribution(self):
        lp = [{"token": "<think>1=0</think>"}, {"token": "1"}, {"token": "="},
              {"token": "8", "logprob": -0.2, "top_logprobs": [{"token": "8", "logprob": -0.2},
                                                              {"token": "2", "logprob": -1.7}]},
              {"token": "\n"}, {"token": "2"}, {"token": "= 0", "logprob": 0}]
        out = soft_scores(lp)
        self.assertGreater(out[1], 0.6)
        self.assertLess(out[1], 8 / 9)                    # 2 の確率も混ざった期待値
        self.assertEqual(out[2], 0.0)

    def test_choice_probs(self):
        lp = [{"token": "C", "logprob": -0.1, "top_logprobs": [{"token": "C", "logprob": -0.1},
                                                              {"token": "A", "logprob": -2.5}]}]
        probs = choice_probs(lp, 3)
        self.assertGreater(probs[2], probs[0])
        self.assertAlmostEqual(sum(probs.values()), 1.0)


class TestTree(unittest.TestCase):
    def test_sections_from_markdown_headings(self):
        parts = []
        for h in range(4):
            parts.append(f"## 第{h + 1}章 テーマ{h + 1}\n" + "。".join(f"本文{h}-{i}の記述" for i in range(300)))
        doc = {"id": "long", "title": "長い報告書", "text": "\n\n".join(parts)}
        from ragcore.engines.base import build_chunks
        chunks = build_chunks([doc])
        self.assertGreater(len(chunks), 8)
        tree = build_tree([doc], chunks, {})
        d = tree["d:long"]
        kinds = {tree[c]["kind"] for c in d["children"]}
        self.assertEqual(kinds, {"section"})
        titles = [tree[c]["title"] for c in d["children"]]
        self.assertIn("第1章 テーマ1", titles)
        # すべての段落がどこかの節に入る
        under = {g for s in d["children"] for g in tree[s]["children"]}
        self.assertEqual(len(under), len(chunks))

    def test_many_docs_grouped(self):
        docs = [{"id": f"d{i}", "title": f"文書{i}", "text": f"本文{i}。"} for i in range(30)]
        from ragcore.engines.base import build_chunks
        chunks = build_chunks(docs)
        tree = build_tree(docs, chunks, {}, max_children=8)
        top = tree["root"]["children"]
        self.assertLessEqual(len(top), 8)
        self.assertEqual({tree[g]["kind"] for g in top}, {"group"})
        self.assertIn("含まれるもの", tree[top[0]]["desc"])
        # 埋め込みがあれば k-means で分ける（偏りすぎたら並び順）
        vecs = {f"d{i}": [1.0, 0.0] if i < 15 else [0.0, 1.0] for i in range(30)}
        tree2 = build_tree(docs, chunks, {}, vecs, max_children=16)
        sizes = sorted(len(tree2[g]["children"]) for g in tree2["root"]["children"])
        self.assertEqual(sizes, [15, 15])


class TestEngine(unittest.TestCase):
    def setUp(self):
        self.engine = DecisionRAGEngine()
        self.ctx = EngineContext(llm=FakeLLMClient(), cfg=dict(CFG))
        self.index = self.engine.ingest(sample_corpus(), self.ctx)

    def test_ingest_and_query(self):
        stats = self.index["stats"]
        self.assertEqual(stats["summarized_docs"], stats["docs"])
        self.assertIn("要約:", self.index["tree"]["d:02_product"]["desc"])
        r = self.engine.query(self.index, "SkyEdge S-2 はどんな製品？", "auto", self.ctx)
        self.assertEqual(r["mode"], "decision(llm)")
        self.assertTrue(r["stats"]["soft"])                  # logprobs から確率で採点
        self.assertEqual(r["citations"][0]["title"], "SkyEdge シリーズ 製品資料")
        self.assertIn("decision_answer", r["answer"])
        json.dumps(self.index, ensure_ascii=False)            # インデックスは JSON 化できる

    def test_budget_and_no_retriever(self):
        calls = []

        class CountingJudge(LLMJudge):
            def binary(self, question, descs):
                calls.append(len(descs))
                return super().binary(question, descs)
        res = navigate(self.index["tree"], "北浜電機との提携", CountingJudge(FakeLLMClient()),
                       budget=1)
        self.assertEqual(res["calls"], 1)
        self.assertEqual(calls, [len(self.index["tree"]["root"]["children"])])  # 1段目を1回で判定

    def test_jev_missing_key_falls_back_to_llm(self):
        ctx = EngineContext(llm=FakeLLMClient(), cfg=dict(CFG, decision_judge="jev"))
        r = self.engine.query(self.index, "SkyEdge S-2 はどんな製品？", "auto", ctx)
        self.assertEqual(r["stats"]["judge"], "llm")
        self.assertIn("Jev", r["stats"]["note"])


class TestBeamSearch(unittest.TestCase):
    """ビームサーチ: 経路スコア・ビーム幅・バックトラック・判定回数の上限。"""

    @staticmethod
    def make(spec):
        """spec: {id: (kind, digit, [children])}。説明文は id。"""
        tree = {}
        for i, (nid, (kind, _, kids)) in enumerate(spec.items()):
            tree[nid] = {"kind": kind, "title": nid, "desc": nid, "children": kids}
            if kind == "chunk":
                tree[nid]["chunk"] = i
        tree["root"] = {"kind": "group", "title": "root", "desc": "",
                        "children": [n for n in spec if not any(n in v[2] for v in spec.values())]}
        digits = {nid: d for nid, (_, d, _) in spec.items()}

        class Judge:
            name, batch, soft = "fake", 30, False

            def __init__(self):
                self.calls = []

            def binary(self, question, descs):
                names = [d.split("）", 1)[1] for d in descs]
                self.calls.append(names)
                return {i: digits[n] / 9 for i, n in enumerate(names, 1)}

            def choice(self, question, descs):
                return {}
        return tree, Judge()

    def hit_names(self, tree, res):
        by_idx = {v["chunk"]: k for k, v in tree.items() if v["kind"] == "chunk"}
        return [by_idx[i] for i, _ in res["hits"]]

    def test_path_score_beats_own_probability(self):
        tree, judge = self.make({"A": ("group", 8, ["a1", "a2"]), "B": ("group", 3, ["b1"]),
                                 "a1": ("doc", 6, ["ca1"]), "a2": ("doc", 5, ["ca2"]),
                                 "b1": ("doc", 7, ["cb1"]), "ca1": ("chunk", 9, []),
                                 "ca2": ("chunk", 9, []), "cb1": ("chunk", 9, [])})
        res = navigate(tree, "q", judge, k=2, beam=2, use_choice=False)
        self.assertEqual(self.hit_names(tree, res), ["ca1", "ca2"])
        self.assertEqual(res["calls"], 3)
        self.assertEqual([x["title"] for x in res["path"][1]["kept"]], ["a1", "a2"])
        tree, judge = self.make({"A": ("group", 8, ["a1", "a2"]), "B": ("group", 3, ["b1"]),
                                 "a1": ("doc", 6, ["ca1"]), "a2": ("doc", 5, ["ca2"]),
                                 "b1": ("doc", 7, ["cb1"]), "ca1": ("chunk", 9, []),
                                 "ca2": ("chunk", 9, []), "cb1": ("chunk", 9, [])})
        res = navigate(tree, "q", judge, k=8, beam=2, use_choice=False)
        self.assertEqual(self.hit_names(tree, res), ["ca1", "ca2", "cb1"])

    def test_backtracks_and_respects_beam(self):
        tree, judge = self.make({"A": ("group", 8, ["a1"]), "B": ("group", 5, ["b1"]),
                                 "a1": ("doc", 0, ["ca1"]), "b1": ("doc", 8, ["cb1"]),
                                 "ca1": ("chunk", 9, []), "cb1": ("chunk", 9, [])})
        res = navigate(tree, "q", judge, k=2, beam=1, use_choice=False)
        self.assertEqual(self.hit_names(tree, res)[0], "cb1")
        self.assertEqual(sum(lv["backtracked"] for lv in res["path"]), 1)
        self.assertTrue(all(len(lv["kept"]) <= 1 for lv in res["path"]))
        self.assertEqual(res["beam"], 1)

    def test_budget_trims_visibly(self):
        tree, judge = self.make({f"x{i}": ("chunk", 5, []) for i in range(70)})
        res = navigate(tree, "q", judge, k=5, budget=2, use_choice=False)
        self.assertEqual(res["calls"], 2)
        self.assertEqual(res["path"][0]["candidates"], 60)
        self.assertEqual(res["path"][0]["trimmed"], 10)


class TestJev(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server, cls.base = start_mock_llm()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def test_request_shape(self):
        body = jev.build_binary_request("typesafe/jev-1.14", "質問", ["A", "B"])
        self.assertEqual(body["questions"]["c2"]["type"], "noul")
        self.assertEqual(set(body["questions"]["c1"]["criteria"]), {"true", "false"})
        self.assertIn("2. B", body["state"]["candidates"])
        with self.assertRaises(LLMError):
            jev.parse_binary({"answers": {}}, 1)

    def test_engine_with_jev(self):
        cfg = dict(CFG, decision_judge="jev", jev_endpoint=self.base + "/decisions",
                   jev_api_key="or-test", jev_model="typesafe/jev-1.14", use_proxy=False)
        ctx = EngineContext(llm=FakeLLMClient(), cfg=cfg)
        engine = DecisionRAGEngine()
        index = engine.ingest(sample_corpus(), ctx)
        before = len(mock_llm.REQUESTS)
        r = engine.query(index, "SkyEdge S-2 はどんな製品？", "auto", ctx)
        self.assertEqual(r["stats"]["judge"], "jev")
        self.assertGreater(r["stats"]["jev_cost_usd"], 0)
        self.assertEqual(r["citations"][0]["title"], "SkyEdge シリーズ 製品資料")
        reqs = [x for x in mock_llm.REQUESTS[before:] if x["path"].endswith("/decisions")]
        self.assertTrue(reqs)
        self.assertTrue(all(x["body"]["model"] == "typesafe/jev-1.14" for x in reqs))


class TestDecisionAPI(unittest.TestCase):
    """設定 → サンプル → DecisionRAG の構築 → 質問（logprobs つきのモック LLM サーバ経由）。"""

    @classmethod
    def setUpClass(cls):
        import ragcore.config as config
        import ragcore.store as store
        cls.tmp = tempfile.TemporaryDirectory()
        tmp = Path(cls.tmp.name)
        cls.saved = (config.CONFIG_FILE, store.DATA_DIR, store.CORPUS_FILE)
        config.CONFIG_FILE = tmp / "config.json"
        store.DATA_DIR = tmp / "data"
        store.CORPUS_FILE = store.DATA_DIR / "corpus.json"
        cls.mock_server, cls.mock_base = start_mock_llm()
        import server as appserver
        cls.app = ThreadingHTTPServer(("127.0.0.1", 0), appserver.Handler)
        threading.Thread(target=cls.app.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.app.server_address[1]}"
        cls.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    @classmethod
    def tearDownClass(cls):
        import ragcore.config as config
        import ragcore.store as store
        cls.app.shutdown()
        cls.mock_server.shutdown()
        config.CONFIG_FILE, store.DATA_DIR, store.CORPUS_FILE = cls.saved
        cls.tmp.cleanup()

    def api(self, path, body=None):
        req = urllib.request.Request(self.base + path) if body is None else urllib.request.Request(
            self.base + path, data=json.dumps(body).encode(), method="POST",
            headers={"Content-Type": "application/json"})
        with self.opener.open(req, timeout=30) as res:
            return json.loads(res.read().decode("utf-8"))

    def wait(self, job_id):
        for _ in range(300):
            job = self.api(f"/api/jobs/{job_id}")
            if job["status"] != "running":
                return job
            time.sleep(0.1)
        raise AssertionError("timeout")

    def test_flow(self):
        cfg = self.api("/api/config", {"base_url": self.mock_base, "model": "mock",
                                       "decision_beam": 3, "decision_judge": "jev",
                                       "jev_api_key": "secret"})
        self.assertEqual(cfg["decision_beam"], 3)
        self.assertTrue(cfg["has_jev_key"])
        self.assertNotIn("jev_api_key", cfg)
        self.api("/api/config", {"decision_judge": "llm"})
        self.api("/api/corpus/sample", {})
        eng = next(e for e in self.api("/api/engines")["engines"] if e["id"] == "decision")
        self.assertTrue(eng["available"])
        job = self.wait(self.api("/api/ingest", {"engines": ["decision"]})["job_id"])
        self.assertEqual(job["engines"]["decision"]["status"], "done", job["engines"]["decision"])
        before = len(mock_llm.REQUESTS)
        job = self.wait(self.api("/api/query", {"engines": ["decision"],
                                                "question": "SkyEdge S-2 はどんな製品？"})["job_id"])
        res = job["engines"]["decision"]["result"]
        self.assertEqual(res["mode"], "decision(llm)")
        self.assertEqual(res["stats"]["beam"], 3)                    # 設定の値
        # 質問ごとにビーム幅を指定できる
        job = self.wait(self.api("/api/query", {"engines": ["decision"], "beam": 1,
                                                "question": "SkyEdge S-2 はどんな製品？"})["job_id"])
        self.assertEqual(job["options"], {"decision_beam": 1})
        self.assertEqual(job["engines"]["decision"]["result"]["stats"]["beam"], 1)
        self.assertTrue(res["stats"]["soft"])
        decide = [x["body"] for x in mock_llm.REQUESTS[before:]
                  if "[TASK:decide]" in json.dumps(x["body"], ensure_ascii=False)]
        self.assertTrue(decide and all(b.get("logprobs") for b in decide))
        self.assertNotIn("<think>", res["answer"])


if __name__ == "__main__":
    unittest.main()
