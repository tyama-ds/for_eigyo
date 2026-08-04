"""API 統合テスト: モック LLM サーバー + 実 HTTP サーバーで全フローを検証する。

設定 → サンプルコーパス → 並列 ingest（bm25/vector/graphrag）→ 並列 query →
統合レポート、部分失敗（未構築エンジン）、グラフ API まで。
"""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP_DIR))

import ragcore.config as config  # noqa: E402
import ragcore.store as store  # noqa: E402
from mock_llm import start_mock_llm  # noqa: E402

_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class TestAPIFlow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        tmp_path = Path(cls.tmp.name)
        # 設定・データの保存先をテスト用の一時ディレクトリへ差し替える
        config.CONFIG_FILE = tmp_path / "config.json"
        store.DATA_DIR = tmp_path / "data"
        store.CORPUS_FILE = store.DATA_DIR / "corpus.json"

        cls.mock_server, cls.mock_base = start_mock_llm()

        import server as appserver
        cls.app_server = ThreadingHTTPServer(("127.0.0.1", 0), appserver.Handler)
        threading.Thread(target=cls.app_server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.app_server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.app_server.shutdown()
        cls.mock_server.shutdown()
        cls.tmp.cleanup()

    # ------------------------------------------------------------ helpers
    @classmethod
    def api(cls, path: str, body: dict | None = None) -> dict:
        url = cls.base + path
        if body is None:
            req = urllib.request.Request(url)
        else:
            req = urllib.request.Request(
                url, data=json.dumps(body).encode("utf-8"), method="POST",
                headers={"Content-Type": "application/json"})
        with _opener.open(req, timeout=30) as res:
            return json.loads(res.read().decode("utf-8"))

    @classmethod
    def wait_job(cls, job_id: str, timeout: float = 90.0) -> dict:
        deadline = time.time() + timeout
        while time.time() < deadline:
            job = cls.api(f"/api/jobs/{job_id}")
            if job["status"] != "running":
                return job
            time.sleep(0.2)
        raise AssertionError(f"ジョブがタイムアウトしました: {job_id}")

    # ------------------------------------------------------------ tests
    def test_00_health_and_engines(self):
        health = self.api("/api/health")
        self.assertTrue(health["ok"])
        engines = self.api("/api/engines")["engines"]
        ids = {e["id"] for e in engines}
        self.assertLessEqual({"graphrag", "vector", "bm25", "hybrid"}, ids)
        # 外部エンジンは未導入の理由が表示される
        ext = next(e for e in engines if e["id"] == "nano-graphrag")
        self.assertFalse(ext["available"])
        self.assertIn("未導入", ext["reason"])

    def test_01_config(self):
        cfg = self.api("/api/config", {
            "base_url": self.mock_base, "model": "mock-model",
            "embed_model": "mock-embed", "request_timeout": 30,
        })
        self.assertEqual(cfg["base_url"], self.mock_base)
        self.assertFalse(cfg.get("has_key"))
        result = self.api("/api/config/test", {})
        self.assertTrue(result["chat"]["ok"], result["chat"])
        self.assertIn("接続OK", result["chat"]["message"])
        self.assertTrue(result["embed"]["ok"], result["embed"])

    def test_02_corpus_sample(self):
        result = self.api("/api/corpus/sample", {})
        self.assertNotIn("error", result)
        corpus = self.api("/api/corpus")
        self.assertGreaterEqual(len(corpus["docs"]), 3)
        self.assertGreaterEqual(corpus["rev"], 1)

    def test_03_ingest_parallel(self):
        r = self.api("/api/ingest", {"engines": ["bm25", "vector", "graphrag"]})
        job = self.wait_job(r["job_id"])
        self.assertEqual(job["status"], "done")
        for eid in ("bm25", "vector", "graphrag"):
            self.assertEqual(job["engines"][eid]["status"], "done",
                             job["engines"][eid].get("error"))
        # GraphRAG のインデックス統計
        stats = job["engines"]["graphrag"]["result"]["stats"]
        self.assertGreater(stats["entities"], 3)
        self.assertGreater(stats["relations"], 0)
        self.assertGreater(stats["communities"], 0)
        # エンジン一覧に構築済みが反映される
        engines = self.api("/api/engines")["engines"]
        built = {e["id"] for e in engines if e["index"]["built"]}
        self.assertLessEqual({"bm25", "vector", "graphrag"}, built)

    def test_04_graph_api(self):
        data = self.api("/api/graph?engine=graphrag")
        self.assertNotIn("error", data)
        self.assertGreater(len(data["nodes"]), 3)
        self.assertGreater(len(data["edges"]), 0)
        names = {n["name"] for n in data["nodes"]}
        self.assertIn("青嶺製作所", names)
        # 未構築エンジンはエラー
        self.assertIn("error", self.api("/api/graph?engine=hybrid"))

    def test_05_query_local_with_partial_failure(self):
        # hybrid は ingest していない → 部分失敗、他は成功
        r = self.api("/api/query", {
            "question": "青嶺製作所の主力製品は？",
            "engines": ["bm25", "vector", "graphrag", "hybrid"], "mode": "auto"})
        job = self.wait_job(r["job_id"])
        self.assertEqual(job["status"], "done")
        for eid in ("bm25", "vector", "graphrag"):
            entry = job["engines"][eid]
            self.assertEqual(entry["status"], "done", entry.get("error"))
            self.assertTrue(entry["result"]["answer"])
        self.assertEqual(job["engines"]["hybrid"]["status"], "error")
        self.assertIn("インデックス未構築", job["engines"]["hybrid"]["error"])
        # 既知エンティティを含む質問なので GraphRAG は local に自動ルーティング
        self.assertEqual(job["engines"]["graphrag"]["result"]["mode"], "local")
        # 成功エンジンが2つ以上 → 統合レポートが生成される
        self.assertEqual(job["synthesis"]["status"], "done", job["synthesis"])
        self.assertIn("統合回答", job["synthesis"]["text"])

    def test_06_query_global(self):
        r = self.api("/api/query", {
            "question": "資料全体の要点をまとめてください",
            "engines": ["graphrag"], "mode": "auto"})
        job = self.wait_job(r["job_id"])
        entry = job["engines"]["graphrag"]
        self.assertEqual(entry["status"], "done", entry.get("error"))
        self.assertEqual(entry["result"]["mode"], "global")
        self.assertTrue(any(c["type"] == "community"
                            for c in entry["result"]["citations"]))

    def test_07_corpus_change_marks_stale(self):
        self.api("/api/corpus/add", {"title": "追加文書", "text": "新しい文書です。"})
        engines = self.api("/api/engines")["engines"]
        graphrag = next(e for e in engines if e["id"] == "graphrag")
        self.assertTrue(graphrag["index"]["built"])
        self.assertTrue(graphrag["index"]["stale"])

    def test_08_validation_errors(self):
        for path, body in (("/api/ingest", {"engines": []}),
                           ("/api/ingest", {"engines": ["unknown-engine"]}),
                           ("/api/query", {"engines": ["bm25"], "question": ""})):
            with self.assertRaises(urllib.error.HTTPError) as cm:
                self.api(path, body)
            self.assertEqual(cm.exception.code, 400)


if __name__ == "__main__":
    unittest.main()
