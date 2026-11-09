"""組み合わせ RAG（エンジン combo）と、各エンジンの retrieve()（検索だけ）のテスト。

- 全文（BM25）だけなら質問時の LLM 呼び出しは回答の 1 回だけ（LLM 未設定なら 0 回で抜粋）
- 各エンジンの構築済みインデックスを読み込んで使い、作り直さない（抽出・要約の LLM を呼ばない）
- 未構築・古い・チャンクの区切りが違う・埋め込みの次元が違う方式は飛ばして理由を出し、残りで答える
- 出典に、統合後の順位・RRF スコアと方式ごとの順位・スコアが付く
- 統合の順位は、方式ごとの順位からの重み付き RRF と一致する
- 再順位付け（LLM で並べ替え）
- 設定（combo_components / combo_rerank）の検証と保存、使える条件、エンジンタブ用の情報
- retrieve() に分けた後も、各エンジンの query() の出典は retrieve() の順位と同じ
"""
from __future__ import annotations

import copy
import json
import re
import sys
import tempfile
import unittest
from collections import Counter
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import ragcore.config as config  # noqa: E402
import ragcore.store as store  # noqa: E402
from mock_llm import FakeLLMClient, mock_chat_response  # noqa: E402
from ragcore.engines import get_engine  # noqa: E402
from ragcore.engines.base import EngineContext  # noqa: E402
from ragcore.engines.bm25 import BM25Engine  # noqa: E402
from ragcore.engines.combo import (CANDIDATES, RRF_K, TOP_K, ComboEngine,  # noqa: E402
                                   combo_settings, parse_order)
from ragcore.engines.decision import DecisionRAGEngine  # noqa: E402
from ragcore.engines.graphrag import GraphRAGEngine  # noqa: E402
from ragcore.engines.vector import VectorEngine  # noqa: E402

REV = 5
CFG = {"base_url": "http://mock/v1", "api_key": "", "model": "mock",
       "embed_model": "", "embed_base_url": "", "embed_api_key": "",
       "context_window": 8192, "request_timeout": 30.0, "max_tokens": 800,
       "use_proxy": False, "proxy_url": "", "decision_judge": "llm",
       "decision_beam": 4, "decision_budget": 8}
NO_LLM = dict(CFG, base_url="", model="")

EXTRA_DOCS = [
    {"id": "x1", "title": "アルファ社の沿革", "text": "アルファ社は 1990 年に創業した。ベータ製品はアルファ社の主力製品である。"},
    {"id": "x2", "title": "ガンマ研究所との共同研究",
     "text": "ガンマ研究所はアルファ社と共同研究を行っている。研究テーマは振動解析である。"},
    {"id": "x3", "title": "ベータ製品の価格", "text": "ベータ製品の標準価格は 1 台 12 万円。保守契約は年 2 万円。"},
    {"id": "x4", "title": "社員食堂のお知らせ", "text": "社員食堂は 11 時から 14 時まで営業する。金曜日はカレーの日。"},
    {"id": "x5", "title": "防災訓練", "text": "防災訓練を 9 月に実施する。避難経路を事前に確認すること。"},
]


def corpus() -> list[dict]:
    docs = []
    for p in sorted((APP_DIR / "sample_docs").glob("*.md")):
        text = p.read_text(encoding="utf-8")
        docs.append({"id": p.stem, "title": text.splitlines()[0].lstrip("# "), "text": text})
    return docs + copy.deepcopy(EXTRA_DOCS)


class CountingLLM(FakeLLMClient):
    """呼ばれたプロンプトを記録するフェイク（判定の logprobs 呼び出しも）。transform で応答を加工できる。"""

    def __init__(self, transform=None, **kw):
        super().__init__(**kw)
        self.prompts: list[str] = []
        self.transform = transform

    def chat(self, prompt, *, system="", max_tokens=None, temperature=0.0, want_think=False):
        self.stats["chat_calls"] += 1
        self.prompts.append(prompt)
        text = mock_chat_response(prompt)
        if self.transform:
            text = self.transform(prompt, text)
        return (text, "（テスト思考）") if want_think else text

    def chat_logprobs(self, prompt, *, system="", max_tokens=None):
        self.prompts.append(prompt)
        return super().chat_logprobs(prompt, system=system, max_tokens=max_tokens)

    @property
    def tasks(self) -> Counter:
        return Counter(m.group(1) for p in self.prompts
                       if (m := re.search(r"\[TASK:([a-z0-9_]+)\]", p)))


def build_components() -> dict[str, dict]:
    """各エンジンのインデックスを1回だけ作る（保存→読み込みと同じ形にする）。"""
    docs = corpus()
    out = {}
    for engine in (VectorEngine(), GraphRAGEngine(), DecisionRAGEngine(), BM25Engine()):
        index = engine.ingest(docs, EngineContext(llm=CountingLLM(), cfg=CFG))
        out[engine.id] = json.loads(json.dumps(index, ensure_ascii=False))
    return out


class StoreCase(unittest.TestCase):
    """一時ディレクトリの data/ に、コーパスと各エンジンの構築済みインデックスを置いて試す。"""

    built: dict[str, dict] = {}

    @classmethod
    def setUpClass(cls):
        if not StoreCase.built:
            StoreCase.built = build_components()

    def setUp(self):
        self.saved = (store.DATA_DIR, store.CORPUS_FILE)
        self.tmp = tempfile.TemporaryDirectory()
        store.DATA_DIR = Path(self.tmp.name) / "data"
        store.CORPUS_FILE = store.DATA_DIR / "corpus.json"
        store.save_corpus(corpus(), REV)
        for eid in ("vector", "graphrag", "decision"):
            store.save_index(eid, self.built[eid], REV)

    def tearDown(self):
        store.DATA_DIR, store.CORPUS_FILE = self.saved
        self.tmp.cleanup()

    def combo_index(self) -> dict:
        index = ComboEngine().ingest(corpus(), EngineContext(llm=CountingLLM(), cfg=CFG))
        store.save_index("combo", index, REV)
        return store.load_index("combo")

    def ask(self, question, components, *, rerank=False, cfg=CFG, llm=None):
        llm = llm or CountingLLM()
        ctx = EngineContext(llm=llm, cfg=dict(cfg, combo_components=components, combo_rerank=rerank))
        result = ComboEngine().query(self.combo_index(), question, "auto", ctx)
        json.dumps(result, ensure_ascii=False)          # ジョブの結果として JSON で返せる
        return result, llm, ctx


class TestComboQuery(StoreCase):
    def test_bm25_only_calls_llm_only_for_the_answer(self):
        result, llm, _ = self.ask("ベータ製品の価格は？", ["bm25"])
        self.assertEqual(llm.tasks, Counter({"combo_answer": 1}))
        self.assertEqual(llm.stats["embed_calls"], 0)
        c = result["combo"]
        self.assertEqual(c["llm_calls"], {"total": 1, "judge": 0, "rerank": 0, "answer": 1})
        self.assertEqual(c["embed_calls"], 0)
        self.assertEqual(result["stats"]["llm_calls"], 1)
        self.assertEqual(result["mode"], "combo(RRF)")
        self.assertIn("combo_answer による回答", result["answer"])
        # 全文だけの統合は BM25 エンジンの検索と同じ順位
        expected = BM25Engine().retrieve(self.built["bm25"], "ベータ製品の価格は？", None, CANDIDATES)
        self.assertEqual([r for r, _ in c["fused"]], [cid for cid, _ in expected])
        self.assertEqual(result["citations"][0]["ref"], "x3#0")
        self.assertEqual(result["variant"]["label"], "全文")
        self.assertEqual(c["components"][0]["source"], "combo")       # 構築不要（チャンクの一覧から）

    def test_without_chat_llm_answers_extractively_with_zero_calls(self):
        result, llm, _ = self.ask("ベータ製品の価格は？", ["bm25"], cfg=NO_LLM)
        self.assertEqual(llm.stats["chat_calls"], 0)
        self.assertEqual(result["mode"], "combo(抽出)")
        self.assertIn("LLM 未設定", result["answer"])
        self.assertTrue(result["citations"])

    def test_reuses_built_indexes_without_rebuilding(self):
        before = {eid: store.load_index(eid)["built_at"] for eid in ("vector", "graphrag", "decision")}
        mtimes = {eid: store._index_file(eid).stat().st_mtime_ns for eid in before}
        q = "アルファ社とガンマ研究所の関係は？"
        result, llm, _ = self.ask(q, ["bm25", "vector", "graphrag", "decision"])
        c = result["combo"]
        self.assertEqual(c["used"], ["bm25", "vector", "graphrag", "decision"])
        self.assertEqual(c["skipped"], [])
        # 構築時の LLM 呼び出し（抽出・要約・文書の説明文）を一切しない
        for task in ("graph_extract", "graph_extract_fast", "community_summary", "decision_doc_summary"):
            self.assertEqual(llm.tasks[task], 0, task)
        # 質問時の LLM: DecisionRAG の判定（＋選択式）と回答の 1 回だけ
        judge = llm.tasks["decide"] + llm.tasks["decide_choice"]
        self.assertGreater(judge, 0)
        self.assertLessEqual(judge, CFG["decision_budget"])
        self.assertEqual(llm.tasks["combo_answer"], 1)
        self.assertEqual(c["llm_calls"], {"total": judge + 1, "judge": judge, "rerank": 0, "answer": 1})
        self.assertEqual(sum(llm.tasks.values()), judge + 1)
        # 質問の埋め込みは 1 回（ベクトルと GraphRAG で共用）。チャンクを埋め込み直さない
        self.assertEqual(llm.stats["embed_calls"], 1)
        self.assertEqual(llm.stats["embed_texts"], 1)
        self.assertEqual(c["embed_calls"], 1)
        by_id = {r["id"]: r for r in c["components"]}
        self.assertEqual(by_id["vector"]["source"], "vector")
        self.assertEqual(by_id["graphrag"]["source"], "graphrag")
        self.assertEqual(by_id["decision"]["source"], "decision")
        self.assertEqual(by_id["decision"]["llm_calls"], judge)
        self.assertEqual(by_id["bm25"]["llm_calls"] + by_id["graphrag"]["llm_calls"], 0)
        self.assertTrue(by_id["graphrag"]["note"].startswith("たどったエンティティ: "))
        self.assertIn("判定", by_id["decision"]["note"])
        for r in c["components"]:
            self.assertGreaterEqual(r["seconds"], 0.0)
            self.assertTrue(r["hits"], r["id"])
        # 保存済みのインデックスはそのまま（作り直していない）
        for eid in before:
            self.assertEqual(store.load_index(eid)["built_at"], before[eid])
            self.assertEqual(store._index_file(eid).stat().st_mtime_ns, mtimes[eid])
        self.assertEqual(result["variant"]["label"], "全文＋埋め込み＋GraphRAG＋DecisionRAG")
        self.assertEqual(result["stats"]["components"], "全文＋埋め込み＋GraphRAG＋DecisionRAG")
        self.assertNotIn("skipped", result["stats"])

    def test_missing_component_is_skipped_with_reason(self):
        store._index_file("graphrag").unlink()
        result, llm, ctx = self.ask("アルファ社の主力製品は？", ["bm25", "vector", "graphrag"])
        c = result["combo"]
        self.assertEqual(c["used"], ["bm25", "vector"])
        self.assertEqual([s["id"] for s in c["skipped"]], ["graphrag"])
        reason = c["skipped"][0]["reason"]
        self.assertEqual(reason, "GraphRAG のインデックスが未構築のため使いませんでした（エンジンタブで先に構築）")
        self.assertIn(reason, result["warnings"])
        self.assertIn(reason, ctx.logs)
        self.assertEqual(result["stats"]["skipped"], "GraphRAG")
        self.assertEqual(result["variant"]["label"], "全文＋埋め込み")
        self.assertIn("使えなかった方式: GraphRAG", result["variant"]["title"])
        self.assertTrue(result["answer"])
        self.assertTrue(result["citations"])
        self.assertEqual(llm.tasks["combo_answer"], 1)

    def test_stale_component_is_skipped(self):
        store.save_index("decision", self.built["decision"], REV - 1)    # コーパスを変える前に構築した
        result, llm, _ = self.ask("SkyEdge の機種は？", ["bm25", "decision"])
        c = result["combo"]
        self.assertEqual(c["used"], ["bm25"])
        self.assertIn("古い", c["skipped"][0]["reason"])
        self.assertIn("構築し直す", c["skipped"][0]["reason"])
        self.assertEqual(llm.tasks["decide"], 0)                       # 古いインデックスで判定しない
        self.assertEqual(c["llm_calls"]["judge"], 0)

    def test_vector_falls_back_to_hybrid_index(self):
        store._index_file("vector").unlink()
        hybrid = dict(self.built["vector"], engine="hybrid")
        store.save_index("hybrid", hybrid, REV)
        result, _, _ = self.ask("ベータ製品の価格は？", ["vector"])
        self.assertEqual(result["combo"]["components"][0]["source"], "hybrid")
        self.assertEqual(result["combo"]["used"], ["vector"])
        store._index_file("hybrid").unlink()
        result, _, _ = self.ask("ベータ製品の価格は？", ["vector"])
        self.assertEqual(result["combo"]["skipped"][0]["reason"],
                         "埋め込み: Vector RAG のインデックスが未構築のため使いませんでした"
                         "（エンジンタブで先に構築。Hybrid RAG のインデックスでも可）")
        self.assertEqual(result["citations"], [])
        self.assertIn("見つかりませんでした", result["answer"])

    def test_misaligned_chunks_are_skipped_or_filtered(self):
        bad = copy.deepcopy(self.built["vector"])
        for c in bad["chunks"]:
            c["id"] = c["id"].replace("#", "@")                         # 区切り（id）がまったく違う
        store.save_index("vector", bad, REV)
        result, _, _ = self.ask("ベータ製品の価格は？", ["bm25", "vector"])
        self.assertEqual(result["combo"]["used"], ["bm25"])
        self.assertIn("チャンクの区切り", result["combo"]["skipped"][0]["reason"])

        part = copy.deepcopy(self.built["vector"])
        changed = next(c for c in part["chunks"] if c["id"] == "x3#0")
        changed["text"] = "別の本文"                                     # 1 件だけ本文が違う
        store.save_index("vector", part, REV)
        result, _, _ = self.ask("ベータ製品の価格は？", ["vector"])
        vec = result["combo"]["components"][0]
        self.assertEqual(result["combo"]["used"], ["vector"])
        self.assertNotIn("x3#0", [cid for cid, _ in vec["hits"]])
        self.assertIn("一致するチャンクだけ", vec["note"])
        self.assertTrue(any("一致するチャンクだけ" in w for w in result["warnings"]))

    def test_embedding_dimension_mismatch_is_skipped(self):
        bad = copy.deepcopy(self.built["vector"])
        bad["chunk_vecs"] = [v[:16] for v in bad["chunk_vecs"]]
        store.save_index("vector", bad, REV)
        result, _, _ = self.ask("ベータ製品の価格は？", ["bm25", "vector"])
        self.assertEqual(result["combo"]["used"], ["bm25"])
        self.assertIn("次元", result["combo"]["skipped"][0]["reason"])
        self.assertIn("16 ≠ 32", result["combo"]["skipped"][0]["reason"])

    def test_failed_component_does_not_stop_the_others(self):
        class NoEmbed(CountingLLM):
            def embed(self, texts):
                from ragcore.llm import LLMError
                raise LLMError("埋め込みサーバに接続できません（テスト）")
        result, _, _ = self.ask("ベータ製品の価格は？", ["bm25", "vector"], llm=NoEmbed())
        self.assertEqual(result["combo"]["used"], ["bm25"])
        self.assertIn("検索に失敗", result["combo"]["skipped"][0]["reason"])
        self.assertIn("接続できません", result["combo"]["skipped"][0]["reason"])
        self.assertTrue(result["citations"])

    def test_citations_carry_component_ranks_and_scores(self):
        result, _, _ = self.ask("アルファ社とガンマ研究所の関係は？", ["bm25", "vector", "graphrag"])
        cites = result["citations"]
        self.assertEqual(len(cites), min(TOP_K, len(result["combo"]["fused"])))
        hits = {r["id"]: {cid: (i, s) for i, (cid, s) in enumerate(r["hits"], 1)}
                for r in result["combo"]["components"]}
        for rank, cite in enumerate(cites, 1):
            self.assertEqual(cite["type"], "chunk")
            self.assertEqual(cite["rank"], rank)
            self.assertEqual(cite["fused_rank"], rank)               # 並べ替えなし
            self.assertGreater(cite["score"], 0)
            self.assertTrue(cite["why"])
            for w in cite["why"]:
                self.assertEqual((w["rank"], w["score"]), hits[w["component"]][cite["ref"]])
                self.assertIn(w["label"], ("全文（BM25）", "埋め込み（ベクトル）", "GraphRAG（エンティティからたどる）"))
                self.assertEqual(w["weight"], 1.5 if w["component"] == "graphrag" else 1.0)
            # 出典に挙がったのに why に無い方式は、その方式の候補に入っていない
            listed = {w["component"] for w in cite["why"]}
            for comp, ranked in hits.items():
                self.assertEqual(comp in listed, cite["ref"] in ranked)
        self.assertTrue(any(len(c["why"]) >= 2 for c in cites))       # 複数の方式が同じチャンクを挙げる

    def test_fused_order_is_weighted_rrf_of_component_rankings(self):
        q = "アルファ社とガンマ研究所の関係は？"
        result, _, _ = self.ask(q, ["bm25", "vector", "graphrag"])
        c = result["combo"]
        weights = {"bm25": 1.0, "vector": 1.0, "graphrag": 1.5}
        self.assertEqual(c["fusion"]["weights"], weights)
        score: dict[str, float] = {}
        for r in c["components"]:
            for rank, (cid, _) in enumerate(r["hits"], 1):
                score[cid] = score.get(cid, 0.0) + weights[r["id"]] / (RRF_K + rank)
        position = {ch["id"]: i for i, ch in enumerate(store.load_index("combo")["chunks"])}
        expected = sorted(score, key=lambda cid: (-round(score[cid], 12), position[cid]))
        self.assertEqual([cid for cid, _ in c["fused"]], expected)
        for cid, s in c["fused"]:
            self.assertAlmostEqual(s, score[cid], places=5)          # 結果は小数 6 桁に丸めてある
        self.assertEqual([x["ref"] for x in result["citations"]], expected[:TOP_K])
        # 各方式の順位は、そのエンジンの retrieve() の順位そのもの
        ctx = EngineContext(llm=CountingLLM(), cfg=CFG)
        by_id = {r["id"]: [cid for cid, _ in r["hits"]] for r in c["components"]}
        self.assertEqual(by_id["vector"], [cid for cid, _ in VectorEngine().retrieve(
            self.built["vector"], q, ctx, CANDIDATES)])
        self.assertEqual(by_id["graphrag"], [cid for cid, _ in GraphRAGEngine().retrieve(
            self.built["graphrag"], q, ctx, CANDIDATES)])

    def test_rerank_reorders_top_candidates_with_one_llm_call(self):
        q = "アルファ社とガンマ研究所の関係は？"
        plain, _, _ = self.ask(q, ["bm25", "vector"])
        fused = [cid for cid, _ in plain["combo"]["fused"]]

        def reverse(prompt, text):          # LLM が候補を逆順に並べる
            if "[TASK:combo_rerank]" not in prompt:
                return text
            n = len(re.findall(r"^\[\d+\] ", prompt.split("# 候補", 1)[-1], re.MULTILINE))
            return json.dumps({"order": list(range(n, 0, -1))})
        result, llm, _ = self.ask(q, ["bm25", "vector"], rerank=True, llm=CountingLLM(reverse))
        self.assertEqual(llm.tasks["combo_rerank"], 1)
        self.assertEqual(result["combo"]["llm_calls"], {"total": 2, "judge": 0, "rerank": 1, "answer": 1})
        self.assertTrue(result["combo"]["rerank"]["used"])
        self.assertEqual(result["mode"], "combo(RRF＋並べ替え)")
        head = fused[:12]
        expected = (list(reversed(head)) + fused[12:])[:TOP_K]
        self.assertEqual([c["ref"] for c in result["citations"]], expected)
        self.assertEqual([c["rank"] for c in result["citations"]], list(range(1, len(expected) + 1)))
        self.assertEqual(result["citations"][0]["fused_rank"], len(head))     # RRF では最下位だった
        self.assertEqual(result["variant"]["label"], "全文＋埋め込み・LLM並べ替え")

        # 読めない出力なら RRF の順のまま（理由を出す）
        result, llm, _ = self.ask(q, ["bm25", "vector"], rerank=True,
                                  llm=CountingLLM(lambda p, t: "わかりません" if "combo_rerank" in p else t))
        self.assertFalse(result["combo"]["rerank"]["used"])
        self.assertIn("RRF の順のまま", result["combo"]["rerank"]["note"])
        self.assertEqual([c["ref"] for c in result["citations"]], fused[:TOP_K])
        self.assertEqual(result["mode"], "combo(RRF)")

    def test_mock_rerank_through_default_handler(self):
        result, llm, _ = self.ask("ベータ製品の価格は？", ["bm25", "vector"], rerank=True)
        self.assertEqual(llm.tasks["combo_rerank"], 1)
        self.assertTrue(result["combo"]["rerank"]["used"])

    def test_no_components_selected(self):
        with self.assertRaises(ValueError):
            self.ask("質問", [])


class TestRetrieveAPI(StoreCase):
    """retrieve() に分けた後も、各エンジンの query() は同じ検索を使う。"""

    def test_query_citations_follow_retrieve(self):
        q = "SkyEdge S-2 はどんな製品？"
        for engine in (BM25Engine(), VectorEngine()):
            index = self.built["vector"] if engine.id == "vector" else self.built["bm25"]
            ctx = EngineContext(llm=CountingLLM(), cfg=CFG)
            hits = engine.retrieve(index, q, ctx, 6)
            result = engine.query(index, q, "auto", EngineContext(llm=CountingLLM(), cfg=CFG))
            self.assertEqual([c["ref"] for c in result["citations"]], [cid for cid, _ in hits], engine.id)
            self.assertEqual([c["score"] for c in result["citations"]], [round(s, 4) for _, s in hits])
            self.assertEqual(ctx.llm.stats["chat_calls"], 0)            # 検索だけ（回答は作らない）

    def test_decision_retrieve_matches_query_and_counts_judge_calls(self):
        q = "SkyEdge S-2 はどんな製品？"
        llm = CountingLLM()
        detail: dict = {}
        hits = DecisionRAGEngine().retrieve(self.built["decision"], q, EngineContext(llm=llm, cfg=CFG), 6,
                                            detail=detail)
        self.assertTrue(hits)
        self.assertEqual(detail["calls"], llm.stats["chat_calls"])
        self.assertEqual(llm.tasks["decision_answer"], 0)
        result = DecisionRAGEngine().query(self.built["decision"], q, "auto",
                                           EngineContext(llm=CountingLLM(), cfg=CFG))
        self.assertEqual([c["ref"] for c in result["citations"]], [cid for cid, _ in hits])
        self.assertEqual(result["stats"]["calls"], detail["calls"])
        self.assertEqual(result["mode"], "decision(llm)")

    def test_graphrag_retrieve_is_entity_centred_and_llm_free(self):
        # エンティティは字句一致（BM25）で選ばせる（モックの埋め込みは hash() の値がプロセスごとに変わるため）
        index = dict(self.built["graphrag"], entity_vecs=None, chunk_vecs=None)
        llm = CountingLLM()
        detail: dict = {}
        hits = GraphRAGEngine().retrieve(index, "ガンマ研究所との共同研究は？",
                                         EngineContext(llm=llm, cfg=CFG), 12, detail=detail)
        self.assertEqual(llm.stats["chat_calls"], 0)
        self.assertTrue(hits)
        self.assertEqual(detail["how"], "bm25")
        self.assertEqual(detail["entities"][0], "ガンマ研究所")
        # 候補はエンティティ（とその関係の先）に結びついたチャンクだけ
        linked = set()
        for e in index["entities"]:
            linked.update(e["chunks"])
        self.assertTrue({cid for cid, _ in hits} <= linked)
        self.assertNotIn("x4#0", [cid for cid, _ in hits])             # 社員食堂（エンティティなし）
        self.assertEqual([s for _, s in hits], sorted((s for _, s in hits), reverse=True))
        # 質問に関係するエンティティが無ければ空（関係の無いチャンクを混ぜない）
        self.assertEqual(GraphRAGEngine().retrieve(index, "社員食堂の営業時間", EngineContext(
            llm=CountingLLM(), cfg=CFG), 12), [])

    def test_graphrag_local_embeds_the_question_once(self):
        llm = CountingLLM()
        result = GraphRAGEngine().query(self.built["graphrag"], "アルファ社の主力製品は？", "local",
                                        EngineContext(llm=llm, cfg=CFG))
        self.assertEqual(result["mode"], "local")
        self.assertEqual(llm.stats["embed_calls"], 1)                  # エンティティ検索とチャンク検索で共用


class TestComboSettings(unittest.TestCase):
    def test_config_keys_are_validated_and_persisted(self):
        saved = config.CONFIG_FILE
        with tempfile.TemporaryDirectory() as tmp:
            config.CONFIG_FILE = Path(tmp) / "config.json"
            try:
                cfg = config.load_config()
                self.assertEqual(cfg["combo_components"], ["bm25", "vector", "graphrag"])
                self.assertFalse(cfg["combo_rerank"])
                out = config.save_config({"combo_components": ["decision", "bm25", "bm25", "bogus"],
                                          "combo_rerank": True})
                self.assertEqual(out["combo_components"], ["bm25", "decision"])   # 決まった順・重複なし
                self.assertTrue(out["combo_rerank"])
                config.save_config({"combo_components": ["bogus"], "combo_rerank": "yes"})   # 範囲外は無視
                cfg = config.load_config()
                self.assertEqual(cfg["combo_components"], ["bm25", "decision"])
                self.assertTrue(cfg["combo_rerank"])
                config.save_config({"combo_components": "bm25"})                    # リストでない
                self.assertEqual(config.load_config()["combo_components"], ["bm25", "decision"])
                config.save_config({"base_url": "http://x/v1"})                     # ほかの保存で消えない
                self.assertEqual(config.load_config()["combo_components"], ["bm25", "decision"])
                config.save_config({"combo_components": []})                        # 全部外すのは可
                self.assertEqual(config.load_config()["combo_components"], [])
                # ファイルを手で書き換えて範囲外の値になっても既定値に戻る
                data = json.loads(config.CONFIG_FILE.read_text(encoding="utf-8"))
                data["combo_components"] = ["turbo", 3]
                data["combo_rerank"] = "on"
                config.CONFIG_FILE.write_text(json.dumps(data), encoding="utf-8")
                cfg = config.load_config()
                self.assertEqual(cfg["combo_components"], ["bm25", "vector", "graphrag"])
                self.assertFalse(cfg["combo_rerank"])
                self.assertIn("combo_components", config.public_config(cfg))
            finally:
                config.CONFIG_FILE = saved

    def test_settings_fall_back_to_defaults(self):
        self.assertEqual(combo_settings({}), (["bm25", "vector", "graphrag"], False))
        self.assertEqual(combo_settings({"combo_components": ["graphrag", "vector"], "combo_rerank": True}),
                         (["vector", "graphrag"], True))

    def test_availability(self):
        eng = ComboEngine()
        both = dict(CFG)
        self.assertEqual(eng.availability(dict(both, combo_components=["bm25"])), (True, ""))
        self.assertTrue(eng.availability(dict(NO_LLM, combo_components=["bm25"]))[0])   # 抜粋で動く
        ok, reason = eng.availability(dict(both, combo_components=[]))
        self.assertFalse(ok)
        self.assertIn("1つも", reason)
        ok, reason = eng.availability(dict(NO_LLM, combo_components=["bm25", "vector"]))
        self.assertFalse(ok)
        self.assertIn("埋め込み", reason)
        ok, reason = eng.availability(dict(NO_LLM, embed_base_url="http://e/v1", embed_model="e",
                                           combo_components=["bm25", "vector"]))
        self.assertTrue(ok, reason)                                     # 埋め込みだけあれば抜粋で動く
        ok, reason = eng.availability(dict(NO_LLM, combo_components=["decision"]))
        self.assertFalse(ok)
        self.assertIn("DecisionRAG", reason)
        api = {"provider": "openai", "timeout": 60.0,
               "openai": {"endpoint": "", "api_key": "k", "model": "", "use_proxy": False, "proxy_url": ""}}
        ok, _ = eng.availability(dict(NO_LLM, combo_components=["decision"], decision_judge="api", decision_api=api))
        self.assertTrue(ok)                                             # 判定 API があれば LLM なしで判定できる
        ok, reason = eng.availability(dict(NO_LLM, combo_components=["bm25"], combo_rerank=True))
        self.assertFalse(ok)
        self.assertIn("再順位付け", reason)

    def test_info_and_variant_for_ui(self):
        eng = get_engine("combo")
        self.assertIsInstance(eng, ComboEngine)
        info = eng.info(dict(CFG, combo_components=["bm25", "vector", "graphrag", "decision"], combo_rerank=True))
        self.assertTrue(info["auto_index"])
        self.assertEqual(info["requires"], {"chat": True, "embed": True})
        c = info["combo"]
        self.assertEqual([m["id"] for m in c["components"]], ["bm25", "vector", "graphrag", "decision"])
        self.assertTrue(all(m["label"] and m["desc"] and m["cost"] for m in c["components"]))
        dec = next(m for m in c["components"] if m["id"] == "decision")
        self.assertIn("最大 8 回", dec["cost"])
        self.assertEqual([s["id"] for s in next(m for m in c["components"] if m["id"] == "vector")["sources"]],
                         ["vector", "hybrid"])
        self.assertEqual(c["estimate"]["llm_max"], 10)                  # 回答 1 + 判定 8 + 並べ替え 1
        self.assertIn("LLM 最大 10 回", c["estimate"]["text"])
        self.assertIn("埋め込み 1 回", c["estimate"]["text"])
        self.assertEqual(c["variant"]["label"], "全文＋埋め込み＋GraphRAG＋DecisionRAG・LLM並べ替え")
        small = eng.info(dict(CFG, combo_components=["bm25"]))
        self.assertEqual(small["requires"], {"chat": False, "embed": False})
        self.assertEqual(small["combo"]["estimate"]["text"], "1 問あたり LLM 1 回（回答 1）")
        self.assertIn("回答は抜粋", eng.info(dict(NO_LLM, combo_components=["bm25"]))["combo"]["estimate"]["text"])
        # 方式は質問時の設定で決まる（構築済みインデックスの stats には無い）
        self.assertEqual(eng.variant(stats={"chunks": 3}, cfg=dict(CFG, combo_components=["vector"]))["label"],
                         "埋め込み")
        self.assertIsNone(eng.variant(stats={"chunks": 3}))

    def test_parse_order(self):
        self.assertEqual(parse_order({"order": [3, "1", "[2]", 9, 3]}, 3), [3, 1, 2])
        self.assertEqual(parse_order([2, "S1"], 2), [2, 1])
        self.assertEqual(parse_order(None, 3), [])
        self.assertEqual(parse_order({"order": "3,1"}, 3), [])


if __name__ == "__main__":
    unittest.main()
