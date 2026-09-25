"""HTTP API と LLM 連携のテスト（モック LLM を使い、実 LLM は不要）。"""
from __future__ import annotations

import http.client
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "tests"))

import server  # noqa: E402
from mock_llm import start_mock_llm  # noqa: E402
from mycelcore.app import MycelApp  # noqa: E402
from mycelcore.llm import LLMClient, LLMError, strip_think  # noqa: E402


class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        cls.llm, cls.llm_url, cls.llm_requests = start_mock_llm()
        cls.app = MycelApp(config_path=root / "cfg.json", vault_override=str(root / "vault"))
        cls.httpd = server.make_server(cls.app, 0)
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.llm.shutdown()
        cls.app.plugins.unload()
        cls.app.index.close()
        cls.tmp.cleanup()

    def call(self, method, path, body=None, headers=None, raw=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        h = {"Content-Type": "application/json"} if body is not None else {}
        h.update(headers or {})
        data = raw if raw is not None else (json.dumps(body).encode("utf-8") if body is not None else None)
        conn.request(method, path, body=data, headers=h)
        res = conn.getresponse()
        payload = res.read()
        conn.close()
        try:
            return res.status, json.loads(payload)
        except ValueError:
            return res.status, payload

    def get(self, _path, **params):
        from urllib.parse import urlencode
        return self.call("GET", _path + ("?" + urlencode(params) if params else ""))

    def post(self, path, body=None, **kw):
        return self.call("POST", path, body if body is not None else {}, **kw)

    def configure_llm(self, **extra):
        body = {"provider": "openai", "base_url": self.llm_url + "/v1", "model": "mock", "embed_model": ""}
        body.update(extra)
        st, cfg = self.post("/api/config", body)
        self.assertEqual(st, 200)
        return cfg

    # ------------------------------------------------------------ 基本
    def test_static_and_state(self):
        st, body = self.call("GET", "/")
        self.assertEqual(st, 200)
        self.assertIn(b"<title>Mycel</title>", body)
        st, s = self.get("/api/state")
        self.assertEqual(st, 200)
        self.assertTrue(s["fts"])
        st, _ = self.call("GET", "/../server.py")
        self.assertEqual(st, 404)

    def test_guard(self):
        st, _ = self.post("/api/daily", {}, headers={"Origin": "http://evil.example"})
        self.assertEqual(st, 403)
        st, _ = self.call("POST", "/api/daily", raw=b"{}", headers={"Content-Type": "text/plain"})
        self.assertEqual(st, 415)
        st, _ = self.call("GET", "/api/state", headers={"Host": "attacker.example"})
        self.assertEqual(st, 403)
        st, _ = self.get("/api/note", path="../../etc/passwd")
        self.assertEqual(st, 400)

    def test_note_roundtrip_and_conflict(self):
        st, r = self.post("/api/note/create", {"title": "API テスト", "folder": "tmp"})
        self.assertEqual(st, 200)
        path = r["path"]
        st, n = self.get("/api/note", path=path)
        st, s = self.post("/api/note/save", {"path": path, "text": "# 1\n[[ホーム]]", "base_version": n["version"]})
        self.assertEqual(st, 200)
        st, c = self.post("/api/note/save", {"path": path, "text": "# 古い版から", "base_version": n["version"]})
        self.assertEqual(st, 409)
        self.assertTrue(c["conflict"])
        self.assertEqual(c["current_version"], s["version"])
        st, links = self.get("/api/links", path="ホーム.md")
        self.assertIn(path, [b["path"] for b in links["backlinks"]])
        st, r2 = self.post("/api/note/rename", {"path": path, "new_path": "tmp/改名"})
        self.assertEqual(r2["path"], "tmp/改名.md")
        st, _ = self.post("/api/note/delete", {"path": "tmp/改名"})
        self.assertEqual(st, 200)
        st, _ = self.get("/api/note", path="tmp/改名")
        self.assertEqual(st, 404)

    def test_search_tags_graph_templates(self):
        st, r = self.get("/api/search", q="稼働率")
        self.assertTrue(r["results"])
        st, t = self.get("/api/tags")
        self.assertIn("顧客", [x["tag"] for x in t["tags"]])
        st, tn = self.get("/api/tag", name="顧客")
        self.assertEqual(len(tn["notes"]), 2)
        st, g = self.get("/api/graph", path="ホーム.md", depth="1")
        self.assertIn("ホーム.md", [n["id"] for n in g["nodes"]])
        st, tp = self.get("/api/templates")
        self.assertIn("商談メモ", [x["title"] for x in tp["templates"]])
        st, rr = self.post("/api/template/render", {"template": "商談メモ", "title": "X"})
        self.assertIn("# X", rr["text"])

    def test_plugins_api(self):
        st, p = self.get("/api/plugins")
        ids = {x["id"]: x for x in p["plugins"]}
        self.assertTrue(ids["change_journal"]["enabled"])
        self.assertFalse(ids["shared_vault"]["enabled"])
        self.post("/api/note/create", {"title": "ジャーナル確認"})
        st, rec = self.get("/api/plugins/change_journal/recent")
        self.assertEqual(st, 200)
        self.assertIn("ジャーナル確認.md", [e["path"] for e in rec["events"]])
        st, _ = self.get("/api/plugins/shared_vault/anything")
        self.assertEqual(st, 404)

    # ------------------------------------------------------------ LLM
    def test_config_hides_keys(self):
        cfg = self.configure_llm(api_key="sk-secret")
        self.assertNotIn("api_key", cfg)
        self.assertTrue(cfg["has_api_key"])
        cfg = self.configure_llm(api_key="")           # 空は「変更なし」
        self.assertTrue(cfg["has_api_key"])
        cfg = self.configure_llm(clear_api_key=True)
        self.assertFalse(cfg["has_api_key"])

    def test_ask_without_llm_returns_sources(self):
        self.post("/api/config", {"base_url": "", "model": ""})
        st, r = self.post("/api/ai/ask", {"question": "決裁者は稼働率を気にしている？"})
        self.assertEqual(st, 200)
        self.assertFalse(r["llm"])
        self.assertTrue(r["sources"])

    def test_ask_summarize_transform_with_llm(self):
        self.configure_llm(api_key="sk-test")
        self.llm_requests.clear()
        st, r = self.post("/api/ai/ask", {"question": "A社の決裁者の関心は？", "path": "ホーム.md",
                                          "history": [{"role": "user", "content": "前の質問"},
                                                      {"role": "assistant", "content": "前の答え"}]})
        self.assertEqual(st, 200, r)
        self.assertTrue(r["llm"])
        self.assertNotIn("<think>", r["answer"])
        self.assertIn("[[", r["answer"])
        req = self.llm_requests[-1]
        self.assertTrue(req["path"].endswith("/v1/chat/completions"))
        self.assertEqual(req["headers"].get("authorization"), "Bearer sk-test")
        roles = [m["role"] for m in req["body"]["messages"]]
        self.assertEqual(roles, ["system", "user", "assistant", "user"])
        # テンプレートは根拠に使わない
        self.assertFalse(any(s["path"].startswith("テンプレート/") for s in r["sources"]))

        st, s = self.post("/api/ai/summarize", {"path": "顧客/A社 生産管理システム更改.md"})
        self.assertEqual(st, 200, s)
        self.assertIn("稼働率", s["summary"])
        self.assertEqual(s["tags"], ["顧客", "要フォロー"])
        st, t = self.post("/api/ai/transform", {"text": "えーと、あした見積", "preset": "tidy"})
        self.assertEqual(t["text"], "- 整えた文章")
        st, e = self.post("/api/ai/transform", {"text": "", "preset": "tidy"})
        self.assertEqual(st, 502)

    def test_embeddings_and_suggest(self):
        self.configure_llm(embed_model="emb")
        st, j = self.post("/api/ai/reindex")
        self.assertEqual(st, 200)
        for _ in range(50):
            st, s = self.get("/api/ai/status")
            if s["job"]["state"] != "running":
                break
            time.sleep(0.1)
        self.assertEqual(s["job"]["state"], "done", s)
        self.assertEqual(s["embedded"], s["chunks"])
        self.assertEqual(s["retrieval"], "hybrid")
        st, sg = self.get("/api/ai/suggest", path="顧客/B社 物流DX.md")
        paths = [x["path"] for x in sg["suggestions"]]
        self.assertTrue(paths)
        self.assertNotIn("顧客/B社 物流DX.md", paths)
        self.assertNotIn("ナレッジ/過去提案 C社 在庫最適化.md", paths)   # 既にリンク済み
        self.configure_llm(embed_model="")

    def test_config_test_endpoint(self):
        self.configure_llm()
        st, r = self.post("/api/config/test")
        self.assertTrue(r["chat"]["ok"])
        self.assertIsNone(r["embed"]["ok"])


class LLMClientTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv, cls.url, cls.reqs = start_mock_llm()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def test_azure_format(self):
        cfg = {"provider": "azure", "base_url": self.url + "/", "api_key": "az-key", "model": "gpt4o-deploy",
               "api_version": "2024-10-21", "embed_model": "emb-deploy", "request_timeout": 5}
        c = LLMClient(cfg)
        self.assertEqual(c.chat("[TASK:transform] x"), "- 整えた文章")
        req = self.reqs[-1]
        self.assertEqual(req["path"], "/openai/deployments/gpt4o-deploy/chat/completions?api-version=2024-10-21")
        self.assertEqual(req["headers"].get("api-key"), "az-key")
        self.assertNotIn("authorization", req["headers"])
        self.assertNotIn("model", req["body"])
        self.assertEqual(len(c.embed(["a", "b"])), 2)
        self.assertTrue(self.reqs[-1]["path"].startswith("/openai/deployments/emb-deploy/embeddings"))

    def test_errors(self):
        with self.assertRaises(LLMError):
            LLMClient({"provider": "openai", "base_url": "", "model": ""}).chat("x")
        with self.assertRaises(LLMError):
            LLMClient({"provider": "openai", "base_url": "http://127.0.0.1:9/v1", "model": "m",
                       "request_timeout": 2}).chat("x")
        with self.assertRaises(LLMError) as cm:
            LLMClient({"provider": "openai", "base_url": self.url + "/nope", "model": "m"}).chat("x")
        self.assertIn("404", str(cm.exception))

    def test_strip_think(self):
        self.assertEqual(strip_think("<think>a</think>答え"), "答え")
        self.assertEqual(strip_think("思考だけ</think>答え"), "答え")
        self.assertEqual(strip_think("答え<think>切れた"), "答え")


if __name__ == "__main__":
    unittest.main()
