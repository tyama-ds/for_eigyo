"""GraphRAG の構築方式（抽出: 標準 / 高速 / 語の共起）とコミュニティ要約（構築時 / 質問時 / なし）のテスト。

「GraphRAG が遅い」への対応の回帰テスト:
  - 超高速（terms）は構築時に LLM を1回も呼ばず、local / global の質問に答えられる
  - 高速（fast）は説明文・強さを書かせない短いプロンプトでグラフができる
  - 高速・超高速では、構築時にエンティティ・コミュニティごとの LLM 呼び出しをしない
  - 質問時に要約（lazy）は、質問に関連の高いコミュニティだけを上限まで要約し、保存して再利用する
  - 要約しない（none）は、コミュニティの要約に LLM を使わない
  - 方式は stats に記録され、記録の無い古いインデックスは 標準 + 構築時要約 として動く
  - ragcore/terms.py は doc-vault の docvault/terms.py と同じ内容
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
from ragcore.engines.base import EngineContext  # noqa: E402
from ragcore.engines.graphrag import (FAST_STRENGTH, LAZY_SUMMARIES_PER_QUERY,  # noqa: E402
                                      MAX_MAPPED_COMMUNITIES, RANKED_MAPPED_COMMUNITIES,
                                      GraphRAGEngine, resolve_modes)

BASE_CFG = {"base_url": "http://mock/v1", "api_key": "", "model": "mock",
            "embed_model": "", "embed_base_url": "", "embed_api_key": "",
            "context_window": 8192, "request_timeout": 30.0, "max_tokens": 800,
            "use_proxy": False, "proxy_url": ""}

SMALL_CORPUS = [
    {"id": "d1", "title": "アルファ社について",
     "text": "アルファ社はベータ製品を開発している。アルファ社はガンマ研究所と共同研究を行う。"},
    {"id": "d2", "title": "ベータ製品の詳細",
     "text": "ベータ製品はアルファ社の主力製品である。ガンマ研究所が評価を担当した。"},
]


def sample_corpus() -> list[dict]:
    docs = []
    for p in sorted((APP_DIR / "sample_docs").glob("*.md")):
        text = p.read_text(encoding="utf-8")
        docs.append({"id": p.stem, "title": text.splitlines()[0].lstrip("# "), "text": text})
    return docs


def cfg(**modes) -> dict:
    return dict(BASE_CFG, **{f"graphrag_{k}": v for k, v in modes.items()})


class RecordingLLM(FakeLLMClient):
    """呼ばれたプロンプトを [TASK:...] ごとに記録するフェイク。transform で応答を加工できる。"""

    def __init__(self, transform=None, **kw):
        kw.setdefault("with_embeddings", False)
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

    @property
    def tasks(self) -> Counter:
        return Counter(m.group(1) for p in self.prompts
                       if (m := re.search(r"\[TASK:([a-z0-9_]+)\]", p)))


def build(corpus, **modes):
    llm = RecordingLLM()
    index = GraphRAGEngine().ingest(corpus, EngineContext(llm=llm, cfg=cfg(**modes)))
    return json.loads(json.dumps(index, ensure_ascii=False)), llm   # 保存→読み込みと同じ形にする


def ask(index, question, mode):
    llm = RecordingLLM()
    ctx = EngineContext(llm=llm, cfg=cfg())
    return GraphRAGEngine().query(index, question, mode, ctx), llm, ctx


class TestTermsSync(unittest.TestCase):
    def test_same_as_doc_vault(self):
        """ragcore/terms.py は doc-vault の docvault/terms.py と同じ内容であること（片方だけ直さない）。"""
        ours = (APP_DIR / "ragcore" / "terms.py").read_bytes()
        theirs = (APP_DIR.parent / "doc-vault" / "docvault" / "terms.py").read_bytes()
        self.assertEqual(ours, theirs, "ragcore/terms.py が doc-vault/docvault/terms.py と違う")


class TestResolveModes(unittest.TestCase):
    def test_defaults_and_fast_paths_never_summarize_at_ingest(self):
        self.assertEqual(resolve_modes({}), ("standard", "ingest"))
        self.assertEqual(resolve_modes({"graphrag_extract": "fast"}), ("fast", "lazy"))
        self.assertEqual(resolve_modes({"graphrag_extract": "terms", "graphrag_summaries": "ingest"}),
                         ("terms", "lazy"))
        self.assertEqual(resolve_modes({"graphrag_extract": "fast", "graphrag_summaries": "none"}),
                         ("fast", "none"))
        self.assertEqual(resolve_modes({"graphrag_extract": "standard", "graphrag_summaries": "lazy"}),
                         ("standard", "lazy"))
        self.assertEqual(resolve_modes({"graphrag_extract": "???", "graphrag_summaries": "???"}),
                         ("standard", "ingest"))

    def test_config_keys_are_validated_and_persisted(self):
        saved = config.CONFIG_FILE
        with tempfile.TemporaryDirectory() as tmp:
            config.CONFIG_FILE = Path(tmp) / "config.json"
            try:
                self.assertEqual(config.load_config()["graphrag_extract"], "standard")
                self.assertEqual(config.load_config()["graphrag_summaries"], "ingest")
                out = config.save_config({"graphrag_extract": "fast", "graphrag_summaries": "none"})
                self.assertEqual((out["graphrag_extract"], out["graphrag_summaries"]), ("fast", "none"))
                config.save_config({"graphrag_extract": "bogus"})            # 範囲外は無視
                self.assertEqual(config.load_config()["graphrag_extract"], "fast")
                config.save_config({"base_url": "http://x/v1"})              # ほかの設定の保存で消えない
                self.assertEqual(config.load_config()["graphrag_summaries"], "none")
                # ファイルを手で書き換えて範囲外の値になっても既定値に戻る
                data = json.loads(config.CONFIG_FILE.read_text(encoding="utf-8"))
                data["graphrag_extract"] = "turbo"
                config.CONFIG_FILE.write_text(json.dumps(data), encoding="utf-8")
                self.assertEqual(config.load_config()["graphrag_extract"], "standard")
                self.assertIn("graphrag_extract", config.public_config(config.load_config()))
            finally:
                config.CONFIG_FILE = saved


class TestTermsMode(unittest.TestCase):
    """超高速（語の共起・LLM なし）。"""

    @classmethod
    def setUpClass(cls):
        cls.index, cls.llm = build(sample_corpus(), extract="terms")

    def test_no_llm_calls_at_ingest(self):
        self.assertEqual(self.llm.prompts, [])
        stats = self.index["stats"]
        self.assertEqual(stats["llm_calls"], 0)
        self.assertEqual((stats["extract_mode"], stats["summary_mode"]), ("terms", "lazy"))
        self.assertEqual(stats["summarized_communities"], 0)
        self.assertIn("seconds", stats)
        self.assertGreater(stats["entities"], 5)
        self.assertGreater(stats["relations"], 0)
        self.assertTrue(all(c["summary"] and c["summary_source"] == "auto"
                            for c in self.index["communities"]))
        names = {e["name"] for e in self.index["entities"]}
        self.assertIn("北浜電機", names)

    def test_local_query(self):
        result, _, _ = ask(self.index, "北浜電機との関係は？", "local")
        self.assertEqual(result["mode"], "local")
        self.assertTrue(result["answer"])
        self.assertTrue(any(c["type"] == "chunk" for c in result["citations"]))
        self.assertEqual(result["variant"]["kind"], "terms")
        self.assertIn("LLM なし", result["variant"]["label"])

    def test_global_query(self):
        result, llm, _ = ask(copy.deepcopy(self.index), "資料全体の要点をまとめてください", "global")
        self.assertEqual(result["mode"], "global")
        self.assertIn("統合回答", result["answer"])
        self.assertTrue(any(c["type"] == "community" for c in result["citations"]))
        self.assertLessEqual(result["stats"]["mapped_communities"], RANKED_MAPPED_COMMUNITIES)
        self.assertEqual(llm.tasks["global_map"], result["stats"]["mapped_communities"])


class TestFastMode(unittest.TestCase):
    """高速（名前・種別と関係の組だけを LLM に書かせる）。"""

    def test_short_prompt_builds_graph(self):
        index, llm = build(SMALL_CORPUS, extract="fast", summaries="ingest")
        extract_prompts = [p for p in llm.prompts if "[TASK:graph_extract" in p]
        self.assertEqual(len(extract_prompts), index["stats"]["chunks"])
        for p in extract_prompts:
            self.assertIn("[TASK:graph_extract_fast]", p)
            head = p.split("# テキスト", 1)[0]
            self.assertNotIn("description", head)
            self.assertNotIn("strength", head)
            self.assertIn("最大 12 件", head)
            self.assertIn("最大 15 件", head)
        # 「構築時に要約」を選んでいても、高速ではコミュニティごとの LLM 呼び出しをしない
        self.assertEqual(llm.tasks["community_summary"], 0)
        self.assertEqual(set(llm.tasks), {"graph_extract_fast"})
        stats = index["stats"]
        self.assertEqual((stats["extract_mode"], stats["summary_mode"]), ("fast", "lazy"))
        self.assertEqual(stats["llm_calls"], len(llm.prompts))
        names = {e["name"] for e in index["entities"]}
        self.assertLessEqual({"アルファ社", "ベータ製品", "ガンマ研究所"}, names)
        self.assertTrue(index["relations"])
        self.assertTrue(all(r["strength"] == FAST_STRENGTH for r in index["relations"]))
        # 説明は本文の文で代用する（LLM には書かせていない）
        alpha = next(e for e in index["entities"] if e["name"] == "アルファ社")
        self.assertIn("アルファ社", alpha["description"])
        self.assertTrue(any(r["description"] for r in index["relations"]))
        result, _, _ = ask(index, "アルファ社の主力製品は？", "auto")
        self.assertEqual(result["mode"], "local")
        self.assertEqual(result["variant"]["label"], "高速")

    def test_object_and_triple_forms_are_accepted(self):
        # 指示と違う形（オブジェクト / 述語を挟んだ3つ組）で返ってもグラフにする
        def transform(prompt, text):
            if "[TASK:graph_extract_fast]" not in prompt:
                return text
            data = json.loads(text)
            ents = [{"name": n, "type": t} for n, t in data["entities"]]
            rels = [[a, "関連する", b] for a, b in data["relations"]]
            return json.dumps({"entities": ents, "relations": rels}, ensure_ascii=False)
        llm = RecordingLLM(transform)
        index = GraphRAGEngine().ingest(SMALL_CORPUS, EngineContext(llm=llm, cfg=cfg(extract="fast")))
        keys = {e["key"] for e in index["entities"]}
        self.assertNotIn("関連する", keys)               # 述語がエンティティにならない
        self.assertTrue(index["relations"])
        self.assertTrue(all(r["description"] == "関連する" for r in index["relations"]))


class TestSummaryModes(unittest.TestCase):
    def test_standard_ingest_summarizes_at_ingest(self):
        index, llm = build(SMALL_CORPUS)
        self.assertGreater(llm.tasks["community_summary"], 0)
        stats = index["stats"]
        self.assertEqual((stats["extract_mode"], stats["summary_mode"]), ("standard", "ingest"))
        self.assertEqual(stats["llm_calls"], len(llm.prompts))
        self.assertGreaterEqual(stats["summarized_communities"], 1)
        result, llm2, ctx = ask(index, "資料全体の要点をまとめてください", "global")
        self.assertEqual(llm2.tasks["community_summary"], 0)
        self.assertEqual(ctx.index_patches, [])
        self.assertEqual(result["variant"]["label"], "標準")

    def test_lazy_summarizes_only_top_communities_at_query_time_and_reuses(self):
        index, llm = build(sample_corpus(), extract="terms", summaries="lazy")
        self.assertEqual(llm.tasks["community_summary"], 0)              # 構築時は要約しない
        eligible = [c["id"] for c in index["communities"] if c["size"] >= 2]
        self.assertGreater(len(eligible), LAZY_SUMMARIES_PER_QUERY)      # 上限が効くだけの数がある

        question = "SkyEdge の販売と北浜電機との提携について全体像は？"
        stored = copy.deepcopy(index)                                    # 保存済みのインデックスの代わり
        result, llm1, ctx = ask(copy.deepcopy(stored), question, "global")
        first = llm1.tasks["community_summary"]
        self.assertGreater(first, 0)
        self.assertLessEqual(first, LAZY_SUMMARIES_PER_QUERY)
        self.assertEqual(result["stats"]["lazy_summarized"], first)
        self.assertEqual(result["stats"]["lazy_reused"], 0)
        self.assertEqual(len(ctx.index_patches), 1)
        for patch in ctx.index_patches:
            patch(stored)
        cached = {c["id"] for c in stored["communities"] if c["summary_source"] == "llm"}
        self.assertEqual(len(cached), first)
        self.assertEqual(stored["stats"]["summarized_communities"], first)
        # 要約したのは質問に関連の高いコミュニティ（map の上位）
        self.assertTrue(any(c["ref"] in cached for c in result["citations"] if c["type"] == "community"))

        # 2回目: 保存した要約を再利用し、同じコミュニティを要約し直さない
        result2, llm2, ctx2 = ask(copy.deepcopy(stored), question, "global")
        self.assertEqual(result2["stats"]["lazy_reused"], first)
        for patch in ctx2.index_patches:
            patch(stored)
        cached2 = {c["id"] for c in stored["communities"] if c["summary_source"] == "llm"}
        self.assertLessEqual(cached, cached2)
        # 2回目の要約呼び出しは、まだ要約していなかったコミュニティの分だけ
        self.assertEqual(llm2.tasks["community_summary"], len(cached2 - cached))
        self.assertLessEqual(llm2.tasks["community_summary"], LAZY_SUMMARIES_PER_QUERY)
        # 何度か聞くと、map の対象はすべて要約済みになり LLM の要約呼び出しは 0 回になる
        for _ in range(3):
            result3, llm3, ctx3 = ask(copy.deepcopy(stored), question, "global")
            for patch in ctx3.index_patches:
                patch(stored)
        self.assertEqual(llm3.tasks["community_summary"], 0)
        self.assertEqual(ctx3.index_patches, [])
        self.assertEqual(result3["stats"]["lazy_summarized"], 0)
        self.assertGreaterEqual(result3["stats"]["lazy_reused"], first)

    def test_lazy_cache_is_written_to_stored_index(self):
        saved = (store.DATA_DIR, store.CORPUS_FILE)
        with tempfile.TemporaryDirectory() as tmp:
            store.DATA_DIR = Path(tmp) / "data"
            store.CORPUS_FILE = store.DATA_DIR / "corpus.json"
            try:
                index, _ = build(sample_corpus(), extract="fast")
                store.save_index("graphrag", index, 3)
                loaded = store.load_index("graphrag")
                result, _, ctx = ask(loaded, "資料全体の要点をまとめてください", "global")
                self.assertGreater(result["stats"]["lazy_summarized"], 0)
                self.assertTrue(store.patch_index("graphrag", loaded["built_at"], ctx.index_patches))
                again = store.load_index("graphrag")
                self.assertEqual(again["corpus_rev"], 3)
                n = sum(c["summary_source"] == "llm" for c in again["communities"])
                self.assertEqual(n, result["stats"]["lazy_summarized"])
                self.assertEqual(again["stats"]["summarized_communities"], n)
                result2, llm2, _ = ask(again, "資料全体の要点をまとめてください", "global")
                self.assertEqual(llm2.tasks["community_summary"], 0)
                self.assertEqual(result2["stats"]["lazy_reused"], n)
                # その間に作り直されたインデックスには書かない
                self.assertFalse(store.patch_index("graphrag", loaded["built_at"] - 1, ctx.index_patches))
            finally:
                store.DATA_DIR, store.CORPUS_FILE = saved

    def test_none_never_calls_llm_for_communities(self):
        index, llm = build(sample_corpus(), extract="fast", summaries="none")
        self.assertEqual(llm.tasks["community_summary"], 0)
        self.assertEqual(index["stats"]["summary_mode"], "none")
        self.assertTrue(all(c["summary"] and c["summary_source"] == "auto" for c in index["communities"]))
        result, llm2, ctx = ask(index, "資料全体の要点をまとめてください", "global")
        self.assertEqual(result["mode"], "global")
        self.assertEqual(llm2.tasks["community_summary"], 0)
        self.assertEqual(ctx.index_patches, [])
        self.assertNotIn("lazy_summarized", result["stats"])
        self.assertEqual(result["variant"]["label"], "高速・要約なし")

    def test_standard_with_none_has_no_community_calls(self):
        index, llm = build(SMALL_CORPUS, extract="standard", summaries="none")
        self.assertEqual(set(llm.tasks), {"graph_extract"})
        self.assertEqual(index["stats"]["llm_calls"], index["stats"]["chunks"])


class TestOldIndex(unittest.TestCase):
    def test_index_without_modes_loads_as_standard_ingest(self):
        index, _ = build(SMALL_CORPUS)
        for key in ("extract_mode", "summary_mode", "llm_calls", "seconds"):
            del index["stats"][key]
        for com in index["communities"]:
            del com["summary_source"]
        engine = GraphRAGEngine()
        self.assertEqual(engine.variant(stats=index["stats"])["kind"], "standard")
        result, llm, ctx = ask(index, "資料全体の要点をまとめてください", "global")
        self.assertEqual(result["mode"], "global")
        self.assertEqual(llm.tasks["community_summary"], 0)
        self.assertEqual(ctx.index_patches, [])
        self.assertLessEqual(result["stats"]["mapped_communities"], MAX_MAPPED_COMMUNITIES)
        self.assertEqual((result["stats"]["extract_mode"], result["stats"]["summary_mode"]),
                         ("standard", "ingest"))
        result, _, _ = ask(index, "ベータ製品とは？", "local")
        self.assertEqual(result["mode"], "local")
        self.assertEqual(result["variant"]["label"], "標準")

    def test_info_lists_modes_for_ui(self):
        info = GraphRAGEngine().info(cfg(extract="terms"))
        g = info["graph"]
        self.assertEqual((g["extract"], g["summaries"]), ("terms", "lazy"))
        self.assertEqual([m["id"] for m in g["extract_modes"]], ["standard", "fast", "terms"])
        self.assertEqual([m["id"] for m in g["summary_modes"]], ["ingest", "lazy", "none"])
        self.assertTrue(all(m["label"] and m["desc"] and m["calls"] for m in g["extract_modes"]))
        self.assertEqual(g["variant"]["kind"], "terms")


if __name__ == "__main__":
    unittest.main()
