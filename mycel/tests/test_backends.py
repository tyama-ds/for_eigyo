"""ローカル LLM と外部 API（OpenAI など）の分離: 設定の検証、接続先の解決、プロキシ必須、送信の記録。"""
from __future__ import annotations

import http.client
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "tests"))

import server  # noqa: E402
from mock_llm import start_mock_llm  # noqa: E402
from mycelcore import llm as LLM  # noqa: E402
from mycelcore.app import MycelApp  # noqa: E402
from mycelcore.config import (ConfigError, backend_of, chat_configured, effective_llm, ext_allowed,  # noqa: E402
                              is_private_url, load_config, save_config)
from mycelcore.llm import LLMClient, LLMError  # noqa: E402
from mycelcore.vault import VaultError  # noqa: E402

NO_PROXY_ENV = {k: "" for k in ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy", "HTTP_PROXY", "http_proxy")}


class ConfigTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "cfg.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_private_url_rule(self):
        for u in ("http://127.0.0.1:11434/v1", "http://localhost:1234/v1", "http://192.168.1.20:8000/v1", "http://10.0.0.5/v1",
                  "http://172.16.3.4:8080/v1", "http://llm-server:8000/v1", "http://gpu01.corp/v1", "http://ai.internal/v1", "http://[::1]:8080/v1"):
            self.assertTrue(is_private_url(u), u)
        for u in ("https://api.openai.com/v1", "http://example.com/v1", "https://my-res.openai.azure.com", "", "notaurl"):
            self.assertFalse(is_private_url(u), u)

    def test_local_url_must_be_private_and_external_must_be_https(self):
        with self.assertRaises(ConfigError):
            save_config({"base_url": "https://api.openai.com/v1"}, self.path)
        with self.assertRaises(ConfigError):
            save_config({"ext_base_url": "http://api.openai.com/v1"}, self.path)
        with self.assertRaises(ConfigError):
            save_config({"ext_base_url": "https://127.0.0.1/v1"}, self.path)
        cfg = save_config({"base_url": "http://127.0.0.1:11434/v1", "model": "qwen", "ext_base_url": "https://api.openai.com/v1",
                           "ext_model": "gpt-4o-mini", "ext_api_key": "sk-x"}, self.path)
        self.assertEqual(cfg["llm_backend"], "local")
        # 既定を外部にできるのは、有効化して URL・モデルがあるときだけ
        cfg = save_config({"llm_backend": "external"}, self.path)
        self.assertEqual(cfg["llm_backend"], "local")
        self.assertFalse(ext_allowed(cfg))
        cfg = save_config({"ext_enabled": True, "llm_backend": "external"}, self.path)
        self.assertEqual(cfg["llm_backend"], "external")
        self.assertTrue(ext_allowed(cfg))
        # 無効に戻すと既定もローカルに戻る
        cfg = save_config({"ext_enabled": False}, self.path)
        self.assertEqual(cfg["llm_backend"], "local")
        self.assertEqual(backend_of(cfg, "external"), "local")

    def test_effective_llm_swaps_fields_and_forces_proxy(self):
        cfg = save_config({"base_url": "http://127.0.0.1:11434/v1", "model": "qwen", "ext_enabled": True,
                           "ext_base_url": "https://api.openai.com/v1", "ext_model": "gpt-4o-mini", "ext_api_key": "sk-x",
                           "ext_proxy_url": "http://proxy.corp:8080", "embed_model": "bge-m3"}, self.path)
        loc = effective_llm(cfg, "local")
        self.assertEqual((loc["_backend"], loc["base_url"], loc["model"], loc["use_proxy"]), ("local", "http://127.0.0.1:11434/v1", "qwen", False))
        ext = effective_llm(cfg, "external")
        self.assertEqual((ext["_backend"], ext["base_url"], ext["model"], ext["api_key"], ext["use_proxy"], ext["proxy_url"]),
                         ("external", "https://api.openai.com/v1", "gpt-4o-mini", "sk-x", True, "http://proxy.corp:8080"))
        self.assertEqual(ext["embed_model"], "bge-m3")                         # 埋め込みはローカルのまま
        self.assertTrue(chat_configured(cfg, "local") and chat_configured(cfg, "external"))
        self.assertEqual(effective_llm(cfg)["_backend"], "local")              # 既定はローカル
        cfg2 = save_config({"llm_backend": "external"}, self.path)
        self.assertEqual(effective_llm(cfg2)["_backend"], "external")
        self.assertEqual(effective_llm(cfg2, "local")["_backend"], "local")   # 明示すれば個別に選べる


class ClientTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.audit = Path(self.tmp.name) / "ext.jsonl"
        LLM.AUDIT_PATH = self.audit
        self.srv, self.url, self.reqs = start_mock_llm()
        self.cfg = load_config(Path(self.tmp.name) / "none.json")
        self.cfg.update({"base_url": self.url + "/v1", "model": "mock", "ext_enabled": True, "ext_base_url": "https://api.openai.com/v1",
                         "ext_model": "gpt-4o-mini", "ext_api_key": "sk-x", "request_timeout": 5})

    def tearDown(self):
        self.srv.shutdown()
        LLM.AUDIT_PATH = None
        self.tmp.cleanup()

    def test_external_requires_proxy_and_is_audited(self):
        with mock.patch.dict(os.environ, NO_PROXY_ENV):
            with self.assertRaises(LLMError) as cm:
                LLMClient(self.cfg, "external")
            self.assertIn("プロキシ", str(cm.exception))
            # プロキシを指定すると、通信はプロキシ（ここではモック）へ CONNECT で向かう。直接は送らない
            c = LLMClient({**self.cfg, "ext_proxy_url": self.url}, "external")
            self.assertEqual((c.backend, c.host), ("external", "api.openai.com"))
            with self.assertRaises(LLMError):
                c.chat("[TASK:ask] こんにちは")
            connects = [r for r in self.reqs if r.get("method") == "CONNECT"]
            self.assertTrue(connects)
            self.assertTrue(connects[0]["path"].startswith("api.openai.com:443"))
            rows = [json.loads(l) for l in self.audit.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(rows), 1)
            self.assertEqual((rows[0]["kind"], rows[0]["task"], rows[0]["model"], rows[0]["host"], rows[0]["ok"]),
                             ("chat", "ask", "gpt-4o-mini", "api.openai.com", False))
            self.assertGreater(rows[0]["chars"], 10)
            # 埋め込みは外部に送らない
            with self.assertRaises(LLMError):
                c.embed(["x"])
            # ローカルは直接・記録なし
            n = len(self.reqs)
            lc = LLMClient(self.cfg, "local")
            self.assertEqual(lc.backend, "local")
            self.assertTrue(lc.chat("[TASK:ask] こんにちは"))
            self.assertEqual(len([r for r in self.reqs[n:] if r.get("method") == "CONNECT"]), 0)
            self.assertEqual(len(self.audit.read_text(encoding="utf-8").splitlines()), 1)

    def test_env_proxy_is_used_for_external(self):
        with mock.patch.dict(os.environ, {**NO_PROXY_ENV, "HTTPS_PROXY": self.url}):
            c = LLMClient(self.cfg, "external")
            with self.assertRaises(LLMError):
                c.chat("x")
            self.assertTrue(any(r.get("method") == "CONNECT" for r in self.reqs))


class AppTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        cls.llm, cls.llm_url, cls.reqs = start_mock_llm()
        cls.app = MycelApp(config_path=root / "cfg.json", vault_override=str(root / "vault"), initial_load="sync")
        cls.httpd = server.make_server(cls.app, 0)
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.llm.shutdown()
        cls.app.close()
        cls.tmp.cleanup()

    def call(self, method, path, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request(method, path, body=json.dumps(body).encode() if body is not None else None,
                     headers={"Content-Type": "application/json"} if body is not None else {})
        res = conn.getresponse()
        data = json.loads(res.read())
        conn.close()
        return res.status, data

    def test_backend_resolution_and_api(self):
        st, cfg = self.call("POST", "/api/config", {"base_url": self.llm_url + "/v1", "model": "mock"})
        self.assertEqual(st, 200)
        st, bad = self.call("POST", "/api/config", {"base_url": "https://api.openai.com/v1"})
        self.assertEqual(st, 400)
        self.assertIn("外部 API", bad["error"])
        # 外部を有効にしていない → 外部指定の質問は 403
        st, r = self.call("POST", "/api/ai/ask", {"question": "A社の決裁者は？", "backend": "external"})
        self.assertEqual(st, 403)
        st, r = self.call("POST", "/api/ai/ask", {"question": "A社の決裁者は？"})
        self.assertEqual((st, r["backend"], r["backend_label"]), (200, "local", "ローカル LLM"))
        self.assertEqual(r["model"], "mock")
        st, s = self.call("GET", "/api/state")
        bk = s["llm"]["backends"]
        self.assertEqual((bk["backend"], bk["default"], bk["local"]["configured"], bk["external"]["allowed"]), ("local", "local", True, False))
        # 外部を設定して有効化（キーは画面に返さない）
        st, cfg = self.call("POST", "/api/config", {"ext_enabled": True, "ext_base_url": "https://api.openai.com/v1", "ext_model": "gpt-4o-mini",
                                                   "ext_api_key": "sk-secret", "ext_proxy_url": self.llm_url})
        self.assertEqual(st, 200)
        self.assertTrue(cfg["has_ext_api_key"])
        self.assertNotIn("ext_api_key", cfg)
        self.assertTrue(ext_allowed(self.app.config()))
        st, bk = self.call("GET", "/api/ai/backends")
        self.assertEqual((bk["external"]["allowed"], bk["external"]["host"], bk["external"]["proxy"]), (True, "api.openai.com", True))
        # 外部指定の質問 → プロキシ（モック）へ向かい失敗、記録が残る。既定（無指定）はローカルのまま
        n = len(self.reqs)
        st, r = self.call("POST", "/api/ai/ask", {"question": "A社の決裁者は？", "backend": "external"})
        self.assertEqual(st, 502, r)
        self.assertTrue(any(x.get("method") == "CONNECT" for x in self.reqs[n:]))
        st, log = self.call("GET", "/api/ai/external_log")
        self.assertEqual(log["total"], 1)
        self.assertEqual(log["entries"][0]["host"], "api.openai.com")
        st, r = self.call("POST", "/api/ai/ask", {"question": "A社の決裁者は？"})
        self.assertEqual((st, r["backend"]), (200, "local"))
        # 接続テストも接続先を選べる
        st, t = self.call("POST", "/api/config/test", {"backend": "external"})
        self.assertEqual((t["backend"], t["host"], t["chat"]["ok"]), ("external", "api.openai.com", False))
        st, t = self.call("POST", "/api/config/test", {"backend": "local"})
        self.assertTrue(t["chat"]["ok"])
        # 既定を外部にすると、何も指定しない質問も外部へ
        st, cfg = self.call("POST", "/api/config", {"llm_backend": "external"})
        self.assertEqual(cfg["llm_backend"], "external")
        st, r = self.call("POST", "/api/ai/ask", {"question": "A社の決裁者は？"})
        self.assertEqual(st, 502)
        st, r = self.call("POST", "/api/ai/ask", {"question": "A社の決裁者は？", "backend": "local"})
        self.assertEqual((st, r["backend"]), (200, "local"))
        self.call("POST", "/api/config", {"llm_backend": "local"})
        with self.assertRaises(VaultError):
            self.call("POST", "/api/config", {"ext_enabled": False})
            self.app.resolve_backend("external")
