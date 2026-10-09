"""GraphRAG（任意モード）のテスト。既定は標準 RAG のまま、選んだときだけ索引を作って使う。"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "tests"))

from mock_llm import start_mock_llm  # noqa: E402
from mycelcore.app import MycelApp  # noqa: E402
from mycelcore.graphrag import MODES, GraphRAG  # noqa: E402

NOTES = {
    "A社 生産管理システム更改.md": "# A社 生産管理システム更改\n\nA社の田中部長が決裁者。生産管理システムの更改で稼働率を重視している。\n\n鈴木さんが窓口で、B社の物流DXも比較対象。\n",
    "B社 物流DX.md": "# B社 物流DX\n\nB社の山本さんと物流DXについて打ち合わせ。生産管理システムとの連携が論点。\n",
    "メモ.md": "# メモ\n\n特に固有名詞のない雑記。\n",
}


class GraphRAGTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        vault = root / "vault"
        vault.mkdir()
        for name, body in NOTES.items():
            (vault / name).write_text(body, encoding="utf-8")
        self.llm, self.llm_url, self.reqs = start_mock_llm()
        self.app = MycelApp(config_path=root / "cfg.json", vault_override=str(vault), initial_load="sync")

    def tearDown(self):
        self.llm.shutdown()
        self.app.close()
        self.tmp.cleanup()

    def prompts(self, start=0):
        return [json.dumps(r["body"], ensure_ascii=False) for r in self.reqs[start:]]

    def llm_on(self):
        self.app.update_config({"base_url": self.llm_url + "/v1", "model": "mock"})

    def build(self):
        self.app.graphrag_build()
        st = self.app.jobs.wait(30)
        self.assertEqual(st["state"], "done", st)
        return st

    def test_default_is_standard_and_graphrag_not_built(self):
        self.assertEqual(self.app.config()["rag_mode"], "standard")
        st = self.app.graphrag.status()
        self.assertFalse(st["ready"])
        self.assertEqual(st["pending"], 3)
        # 既定の質問は GraphRAG を触らない（モック LLM への kg 系の呼び出しが無い）
        self.llm_on()
        res = self.app.ask("A社の決裁者は？")
        self.assertEqual(res["mode"], "standard")
        self.assertFalse(any("[TASK:kg" in p for p in self.prompts()))
        self.assertFalse(self.app.graphrag.status()["ready"])
        # 索引が無いまま GraphRAG を選ぶと案内だけ返す
        res = self.app.ask("A社の決裁者は？", mode="local")
        self.assertIn("索引がまだ", res["message"])

    def test_build_with_llm_and_ask(self):
        self.llm_on()
        self.build()
        st = self.app.graphrag.status()
        self.assertTrue(st["ready"])
        self.assertEqual(st["pending"], 0)
        self.assertGreaterEqual(st["llm_docs"], 2)
        self.assertGreater(st["nodes"], 3)
        self.assertGreater(st["edges"], 0)
        self.assertGreaterEqual(st["communities"], 1)
        self.assertGreaterEqual(st["summarized"], 1)
        comms = self.app.graphrag.communities()
        self.assertEqual(comms[0]["title"], "A社 更改案件")
        # 局所：実体から辿る
        res = self.app.ask("田中部長は何を重視していますか？", mode="auto")
        self.assertEqual(res["mode"], "local")
        self.assertIn("稼働率", res["answer"])
        ents = [e["name"] for e in res["graph"]["entities"]]
        self.assertIn("田中部長", ents)
        self.assertTrue(res["graph"]["relations"])
        self.assertTrue(any(s["path"].startswith("A社") for s in res["sources"]))
        # 全体：コミュニティ要約の map/reduce
        res = self.app.ask("全体の傾向をまとめて", mode="auto")
        self.assertEqual(res["mode"], "global")
        self.assertIn("稼働率", res["answer"])
        self.assertTrue(res["graph"]["communities"])
        tasks = self.prompts()
        self.assertTrue(any("[TASK:kg_map]" in p for p in tasks))
        self.assertTrue(any("[TASK:kg_reduce]" in p for p in tasks))
        # 実体の詳細
        key = res["graph"]["communities"][0]["members"][0]["key"] if res["graph"]["communities"][0].get("members") else None
        ent = self.app.graphrag.entity(key or "a社")
        self.assertIn("name", ent)

    def test_incremental_rebuild(self):
        self.llm_on()
        self.build()
        n_before = len(self.reqs)
        self.build()
        # 変更が無ければ抽出し直さない（要約も再利用）
        self.assertEqual(len([p for p in self.prompts(n_before) if "[TASK:kg]" in p]), 0)
        # 1 文書だけ変更 → その文書だけ再抽出、削除は索引から消える
        vault = Path(self.app.vault.root)
        (vault / "メモ.md").write_text("# メモ\n\nC社の佐藤さんと会った。\n", encoding="utf-8")
        (vault / "B社 物流DX.md").unlink()
        self.app.update_index()
        self.app.jobs.wait(30)
        st = self.app.graphrag.status()
        self.assertEqual(st["documents"], 2)
        self.assertEqual(st["pending"], 1)
        n_before = len(self.reqs)
        self.build()
        self.assertEqual(len([p for p in self.prompts(n_before) if "[TASK:kg]" in p]), 1)
        self.assertEqual(self.app.graphrag.status()["pending"], 0)
        self.assertEqual(self.app.graphrag.entity("山本"), {})

    def test_rule_fallback_without_llm(self):
        self.build()
        st = self.app.graphrag.status()
        self.assertTrue(st["ready"])
        self.assertEqual(st["llm_docs"], 0)
        self.assertFalse(st["llm"])
        res = self.app.ask("A社について", mode="local")
        self.assertEqual(res["mode"], "local")
        self.assertFalse(res["llm"])
        self.assertTrue(res["sources"])

    def test_config_mode_and_modes(self):
        self.assertEqual(set(MODES), {"standard", "auto", "local", "global"})
        self.app.update_config({"rag_mode": "bogus"})
        self.assertEqual(self.app.config()["rag_mode"], "standard")
        self.app.update_config({"rag_mode": "auto"})
        self.assertEqual(self.app.config()["rag_mode"], "auto")
        self.llm_on()
        self.build()
        res = self.app.ask("田中部長は？")
        self.assertEqual(res["mode"], "local")
        # 明示的に標準を選べば標準
        self.assertEqual(self.app.ask("田中部長は？", mode="standard")["mode"], "standard")

    def test_reopen_closes_cleanly(self):
        self.llm_on()
        self.build()
        self.assertIsInstance(self.app.graphrag, GraphRAG)
        self.app.open_vault()
        self.assertTrue(self.app.graphrag.status()["ready"])
