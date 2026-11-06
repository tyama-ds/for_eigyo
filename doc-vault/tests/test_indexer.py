"""取り込みの順番: 遅いローカル LLM でも、取り込んだ文書はすぐ検索・相談に使えること。

以前は1本のワーカーが1文書ずつ「抽出 → 埋め込み → グラフ → 要約」を最後まで処理していたため、
ローカル LLM のグラフ抽出が続く間、後ろの文書は抽出すらされず「待機中」のままで、相談の検索対象にも
入らなかった（接続テストは通るのに RAG につながっていないように見える）。

ここでは模擬 LLM のグラフ抽出を止めておき、その間に全文書の抽出・要約が済むことを確かめる。
"""
from __future__ import annotations

import os
import re
import sys
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

APP_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import mock_llm  # noqa: E402
from docvault import config, db, rag  # noqa: E402
from docvault.indexer import INDEXER, register_upload  # noqa: E402
from docvault.retrieval import INDEX  # noqa: E402

GRAPH_GATE = threading.Event()        # セットするまでグラフ抽出の応答を返さない
TASKS: list[str] = []                 # 届いた LLM 呼び出しの種類（順番）


class GatedHandler(mock_llm.MockLLMHandler):
    def do_POST(self):  # noqa: N802
        if self.path.endswith("/chat/completions"):
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length)
            m = re.search(r"\[TASK:([a-z_]+)\]", raw.decode("utf-8", "replace"))
            task = m.group(1) if m else ""
            TASKS.append(task)
            if task == "graph_extract":
                GRAPH_GATE.wait(60)
            self.rfile = __import__("io").BytesIO(raw)          # 本体の処理に読み直させる
            self.headers.replace_header("Content-Length", str(len(raw)))
        super().do_POST()


def doc_text(topic: str, n: int = 6) -> str:
    return "\n\n".join(f"## {topic} {i}\n{topic}の第{i}節。" + "記録と対策を記す。" * 40 for i in range(n))


class TestIngestOrder(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        tmp = Path(self.tmp.name)
        self.saved_cfg = config.CONFIG_FILE
        config.CONFIG_FILE = tmp / "c.json"
        db.set_data_dir(tmp / "data")
        INDEX.loaded_for = None
        # クラウドは初期値のまま（Claude・キーなし）。認証情報が見つからない状態にする
        env = {k: v for k, v in os.environ.items() if not k.startswith("ANTHROPIC_")}
        env["ANTHROPIC_CONFIG_DIR"] = str(tmp / "no-anthropic")
        self.env = mock.patch.dict(os.environ, env, clear=True)
        self.env.start()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), GatedHandler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{self.server.server_address[1]}/v1"
        config.save_config({"profiles": {"local": {"provider": "openai", "base_url": base,
                                                   "model": "qwen3:14b"}}})
        GRAPH_GATE.clear()
        TASKS.clear()

    def tearDown(self):
        GRAPH_GATE.set()
        INDEXER.wait_idle(60)
        self.server.shutdown()
        self.env.stop()
        config.CONFIG_FILE = self.saved_cfg
        db.close()
        self.tmp.cleanup()

    def searchable(self):
        allowed, _ = rag.allowed_docs({"mode": "auto", "scope": {}}, "local")
        return allowed

    def test_documents_become_searchable_while_llm_stages_wait(self):
        ids = [register_upload(doc_text(t).encode("utf-8"), f"{t}.md", folder="テスト", sensitivity=s)[0]["id"]
               for t, s in (("温度ばらつき", "internal"), ("設備投資", "internal"), ("給電方式", "public"))]
        deadline = time.time() + 20
        while time.time() < deadline and self.searchable() != set(ids):
            time.sleep(0.05)
        # グラフ抽出（1件目の文書）が止まっている間に、3件とも抽出済みで検索できる
        self.assertEqual(self.searchable(), set(ids))
        self.assertFalse(GRAPH_GATE.is_set())
        for i in ids:
            d = db.get_doc(i)
            self.assertEqual(d["status"], "ready")
            self.assertGreater(d["chars"], 0)
        # 後処理は文書をまたいで 要約 → グラフ の順。グラフ抽出が止まる前に 3 件とも要約が済んでいる
        deadline = time.time() + 20
        while time.time() < deadline and "graph_extract" not in TASKS:
            time.sleep(0.05)
        first_graph = TASKS.index("graph_extract")
        self.assertEqual(TASKS[:first_graph].count("doc_summary"), 3, TASKS)
        self.assertTrue(all(db.get_doc(i)["summary"] for i in ids))
        self.assertTrue(INDEXER.status()["busy"] >= 1)          # まだ取り込み中（グラフ待ち）
        GRAPH_GATE.set()
        self.assertTrue(INDEXER.wait_idle(60))
        for i in ids:
            d = db.get_doc(i)
            self.assertEqual((d["status"], d["stage"]), ("ready", ""))
            self.assertEqual(d["flags"].get("graph"), "local")
            # 公開文書も、キーの無い Claude ではなくローカル LLM で処理される
            self.assertEqual(d["flags"].get("summary"), "local")
            self.assertFalse([w for w in d["warnings"] if "Claude" in w], d["warnings"])

    def test_reextract_drops_stale_later_stages(self):
        doc_id = register_upload(doc_text("熱処理炉").encode("utf-8"), "a.md", sensitivity="internal")[0]["id"]
        deadline = time.time() + 20
        while time.time() < deadline and "graph_extract" not in TASKS:
            time.sleep(0.05)
        INDEXER.enqueue(doc_id)                 # グラフ抽出の途中で抽出し直す
        GRAPH_GATE.set()
        self.assertTrue(INDEXER.wait_idle(60))
        d = db.get_doc(doc_id)
        self.assertEqual((d["status"], d["stage"]), ("ready", ""))
        self.assertEqual(TASKS.count("doc_summary"), 2)        # 抽出し直した後に要約も作り直す
        self.assertEqual(d["flags"].get("graph"), "local")


if __name__ == "__main__":
    unittest.main()
