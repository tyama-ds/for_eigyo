"""ローカル LLM でグラフ抽出が途中で終わってしまう問題の回帰テスト。

以前は1回の LLM エラー（相談の回答中の順番待ちでタイムアウト・モデルのプロセスが落ちて HTTP 500・
受信の途中で止まる）で抽出全体を止めていたため、24 チャンクの文書でも 2 チャンク分しかグラフに
ならなかった。失敗の型ごとに偽 LLM で再現し、最後まで抽出できること・中断しても続きから進むことを確かめる。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP_DIR))
os.environ.setdefault("DOCVAULT_QUIET", "1")

from docvault import config, db, indexer, readings  # noqa: E402
from docvault.indexer import INDEXER, register_upload  # noqa: E402
from docvault.retrieval import INDEX  # noqa: E402

PARA = ("青嶺製作所は仙台工場で産業用ワイヤレスセンサー SkyEdge S-2 を製造している。S-2 は振動・温度・電流を計測する。"
        "2026 年 8 月、北浜電機から筐体固定ネジの緩みについて 3 件の指摘を受けた。原因は熱処理炉の温度ばらつき。")
GOOD = json.dumps({"entities": [{"name": "青嶺製作所", "type": "組織", "description": "会社。"},
                                {"name": "SkyEdge S-2", "type": "製品", "description": "センサー。"}],
                   "relations": [{"source": "青嶺製作所", "target": "SkyEdge S-2", "description": "製造", "strength": 8}],
                   "keywords": ["センサー"]}, ensure_ascii=False)


class FakeLLM(BaseHTTPRequestHandler):
    """graph_extract の n 回目（1 始まり）に、plan[n] の失敗を起こす偽 OpenAI 互換サーバ。"""
    protocol_version = "HTTP/1.1"
    plan: dict[int, str] = {}
    default = "ok"
    calls: list[str] = []
    lock = threading.Lock()

    def do_POST(self):  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        prompt = body["messages"][-1]["content"]
        graph_call = "graph_extract" in prompt
        with FakeLLM.lock:
            if graph_call:
                FakeLLM.calls.append(prompt.split("# テキスト", 1)[-1][:40])
                n = len(FakeLLM.calls)
                how = FakeLLM.plan.get(n, FakeLLM.default)
            else:
                how = "ok"
        if how in ("http500", "http400", "http404"):
            code = int(how[4:])
            data = json.dumps({"error": {"message": f"fake {code}"}}).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if how == "slow":
            time.sleep(1.5)                          # タイムアウト（テストでは 0.5 秒）より長く待たせる
        text = GOOD if graph_call else '{"summary": "要約。", "keywords": ["品質"]}'
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            for i in range(0, len(text), 16):
                if how == "stall" and i >= 32:
                    time.sleep(1.5)
                piece = json.dumps({"choices": [{"delta": {"content": text[i:i + 16]}}]}, ensure_ascii=False)
                self.wfile.write(f"data: {piece}\n\n".encode())
                self.wfile.flush()
            self.wfile.write(b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n')
        except OSError:
            pass

    def log_message(self, *args):
        pass


class TestIngestResilience(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeLLM)
        cls.server.daemon_threads = True
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}/v1"
        cls.saved = (indexer.RETRY_WAITS, indexer.AUTO_RESUME_SEC, indexer.CHAT_WAIT_MAX)
        indexer.RETRY_WAITS = (0.05, 0.05, 0.05)
        indexer.AUTO_RESUME_SEC = 3600.0

    @classmethod
    def tearDownClass(cls):
        indexer.RETRY_WAITS, indexer.AUTO_RESUME_SEC, indexer.CHAT_WAIT_MAX = cls.saved
        INDEXER.cancel_timers()
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        tmp = Path(self.tmp.name)
        config.CONFIG_FILE = tmp / "config.json"
        db.set_data_dir(tmp / "data")
        INDEX.loaded_for = None
        self.use_llm(self.base)
        FakeLLM.plan, FakeLLM.default, FakeLLM.calls = {}, "ok", []

    def tearDown(self):
        INDEXER.wait_idle(30)
        INDEXER.cancel_timers()
        db.close()
        self.tmp.cleanup()

    def use_llm(self, base):
        config.save_config({"profiles": {"local": {"provider": "openai", "base_url": base, "model": "qwen3:14b"}},
                            "rag": {"doc_summary": False, "chunk_size": 900}})
        # 画面からは 5 秒未満にできないので、ファイルを直接書いてタイムアウトを短くする（偽 LLM は 1.5 秒待つ）
        data = json.loads(config.CONFIG_FILE.read_text(encoding="utf-8"))
        data["profiles"]["local"]["timeout"] = 0.5
        config.CONFIG_FILE.write_text(json.dumps(data), encoding="utf-8")

    def ingest(self, sections=12):
        text = "\n\n".join(f"## 第{i + 1}節\n" + PARA * 4 for i in range(sections))
        doc, _ = register_upload(text.encode("utf-8"), "品質報告.md", sensitivity="internal")
        self.assertTrue(INDEXER.wait_idle(60))
        return db.get_doc(doc["id"])

    def graph_reading(self, doc_id):
        return next(r for r in readings.list_for(doc_id) if r["stage"] == "graph")

    def chunk_total(self, doc_id):
        return db.one("SELECT COUNT(*) n FROM chunks WHERE doc_id=?", (doc_id,))["n"]

    # ------------------------------------------------------------ 失敗の型ごと
    def check_survives(self, how):
        FakeLLM.plan = {3: how}
        d = self.ingest()
        r = self.graph_reading(d["id"])
        n = self.chunk_total(d["id"])
        self.assertGreater(n, 6)
        self.assertEqual(r["chunks"].get("done"), n, (how, r["warnings"]))
        self.assertEqual(r["status"], "done")
        self.assertEqual(d["flags"]["graph"], "local")
        return d

    def test_http500_once_does_not_stop_extraction(self):      # モデルのプロセスが落ちた
        d = self.check_survives("http500")
        self.assertTrue(any("やり直しました" in w for w in d["warnings"]), d["warnings"])

    def test_timeout_once_does_not_stop_extraction(self):      # 相談の回答中で順番待ち
        d = self.check_survives("slow")
        self.assertTrue(any("やり直しました" in w for w in d["warnings"]), d["warnings"])

    def test_stall_while_receiving_does_not_stop_extraction(self):   # 受信の途中で止まった
        d = self.check_survives("stall")
        r = self.graph_reading(d["id"])
        log = readings.chunk_log(r["id"])
        # 止まるまでに受け取った分から JSON を取り出すか、やり直して取り出す
        self.assertTrue(any(x["reason"] for x in log) or r["data"].get("retries"), log)

    def test_request_error_skips_only_that_chunk(self):
        FakeLLM.plan = {2: "http400"}
        d = self.ingest()
        r = self.graph_reading(d["id"])
        n = self.chunk_total(d["id"])
        self.assertEqual(r["chunks"].get("done"), n - 1)
        self.assertEqual(r["chunks"].get("failed"), 1)
        self.assertEqual(r["status"], "done")
        self.assertTrue(any("失敗したチャンクをやり直す" in w for w in d["warnings"]), d["warnings"])
        # 失敗したチャンクだけやり直す
        FakeLLM.calls = []
        INDEXER.enqueue(d["id"], ("graph",), {"policy": "retry_failed"})
        INDEXER.wait_idle(30)
        self.assertEqual(len(FakeLLM.calls), 1)
        self.assertEqual(self.graph_reading(d["id"])["chunks"].get("done"), n)

    def test_config_error_stops_with_reason(self):
        FakeLLM.default = "http404"
        d = self.ingest()
        r = self.graph_reading(d["id"])
        self.assertEqual(r["status"], "error")
        self.assertEqual(len(FakeLLM.calls), 1)                  # 設定の誤りは続けても通らない
        self.assertTrue(any("設定を確認" in w for w in d["warnings"]), d["warnings"])

    def test_llm_down_pauses_and_resumes_from_where_it_stopped(self):
        FakeLLM.plan = {n: "http500" for n in range(5, 40)}      # 4 チャンク済んだ所で LLM が落ちる
        d = self.ingest()
        r = self.graph_reading(d["id"])
        self.assertEqual(r["status"], "paused")
        self.assertEqual(r["chunks"].get("done"), 4)
        self.assertTrue(any("一時停止" in w for w in d["warnings"]), d["warnings"])
        # LLM が戻ったら続きから（済んだ 4 チャンクは呼ばない）
        FakeLLM.plan, FakeLLM.calls = {}, []
        INDEXER.enqueue(d["id"], ("graph",), {"policy": "resume"})
        INDEXER.wait_idle(30)
        n = self.chunk_total(d["id"])
        r = self.graph_reading(d["id"])
        self.assertEqual(r["chunks"].get("done"), n)
        self.assertEqual(r["status"], "done")
        self.assertEqual(len(FakeLLM.calls), n - 4)

    def test_restart_resumes_unfinished_graph(self):
        d = self.ingest()
        n = self.chunk_total(d["id"])
        rid = self.graph_reading(d["id"])["id"]
        # 途中で終了した状態を作る（後ろ半分の記録と言及を消し、処理中のまま）
        late = [r["id"] for r in db.query("SELECT id FROM chunks WHERE doc_id=? ORDER BY ord", (d["id"],))][n // 2:]
        for cid in late:
            db.write("DELETE FROM graph_chunks WHERE reading_id=? AND chunk_id=?", (rid, cid))
            db.write("DELETE FROM entity_mentions WHERE reading_id=? AND chunk_id=?", (rid, cid))
        db.write("UPDATE readings SET status='running' WHERE id=?", (rid,))
        FakeLLM.calls = []
        self.assertGreaterEqual(indexer.resume_pending(), 1)
        INDEXER.wait_idle(30)
        self.assertEqual(len(FakeLLM.calls), len(late))         # 残りだけ
        r = self.graph_reading(d["id"])
        self.assertEqual(r["chunks"].get("done"), n)
        self.assertEqual(r["status"], "done")

    def test_waits_while_chat_is_answering(self):
        indexer.CHAT_WAIT_MAX = 0.6
        notes = []
        with INDEXER.chat_session():
            started = time.time()
            INDEXER._yield_to_chat(lambda: notes.append(1), lambda: None)
            waited = time.time() - started
        self.assertGreaterEqual(waited, 0.5)
        self.assertTrue(notes)
        started = time.time()
        INDEXER._yield_to_chat(lambda: None, lambda: None)       # 相談が無ければ待たない
        self.assertLess(time.time() - started, 0.1)

    def test_terms_mode_needs_no_llm(self):
        # ローカル LLM が未設定でも、語の共起グラフは作れる（社外秘文書も外へ出ない）
        config.save_config({"profiles": {"local": {"base_url": "", "model": ""}},
                            "rag": {"graph_mode": "terms", "doc_summary": False}})
        d = self.ingest(sections=4)
        r = self.graph_reading(d["id"])
        self.assertEqual(r["mode"], "terms")
        self.assertEqual(r["status"], "done")
        self.assertEqual(FakeLLM.calls, [])
        names = {x["name"] for x in db.query("SELECT name FROM entity_mentions WHERE reading_id=?", (r["id"],))}
        self.assertIn("青嶺製作所", names)
        self.assertIn("熱処理炉", names)
        self.assertEqual(d["flags"]["graph"], "terms")


if __name__ == "__main__":
    unittest.main()
