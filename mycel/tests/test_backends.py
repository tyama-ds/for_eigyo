"""ローカル LLM と外部 API（OpenAI など）の分離: 設定の検証、接続先の解決、プロキシ必須、送信の記録。"""
from __future__ import annotations

import http.client
import json
import os
import re
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

    def test_switch_hidden_by_default(self):
        cfg = load_config(self.path)
        self.assertFalse(cfg["ext_show_switch"])
        self.assertTrue(save_config({"ext_show_switch": True}, self.path)["ext_show_switch"])

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
        # Windows は環境変数名の大文字小文字を区別しないので、HTTPS_PROXY を小文字の空値で上書きしないようにする
        env = {k: v for k, v in NO_PROXY_ENV.items() if k.lower() != "https_proxy"}
        with mock.patch.dict(os.environ, {**env, "HTTPS_PROXY": self.url}):
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
        self.assertFalse(bk["show_switch"])                                   # 切り替えの表示は既定で非表示
        st, cfg = self.call("POST", "/api/config", {"ext_show_switch": True})
        self.assertTrue(cfg["ext_show_switch"])
        st, bk = self.call("GET", "/api/ai/backends")
        self.assertTrue(bk["show_switch"])
        st, cfg = self.call("POST", "/api/config", {"ext_show_switch": False})
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


class PayloadTest(unittest.TestCase):
    """モデルごとの送信パラメータ（max_tokens / max_completion_tokens / temperature / reasoning_effort）と自動修正。"""

    def setUp(self):
        self.srv, self.url, self.reqs = start_mock_llm()
        self.cfg = load_config(Path(tempfile.mkdtemp()) / "none.json")
        self.cfg.update({"base_url": self.url + "/v1", "model": "mock", "request_timeout": 5})

    def tearDown(self):
        self.srv.shutdown()

    def test_payload_style_by_model_and_host(self):
        from mycelcore.llm import is_reasoning_model, payload_style
        for m in ("gpt-5", "gpt-5-mini", "gpt-5-nano", "o1", "o3-mini", "o4-mini", "o3", "gpt-oss:20b"):
            self.assertTrue(is_reasoning_model(m), m)
        for m in ("gpt-4o", "gpt-4o-mini", "gpt-4.1", "gpt-5-chat-latest", "qwen2.5:7b", "llama3", "mock"):
            self.assertFalse(is_reasoning_model(m), m)
        openai = {"base_url": "https://api.openai.com/v1", "provider": "openai", "reasoning_effort": "low"}
        s = payload_style(openai, "gpt-5-mini")
        self.assertEqual((s["tokens_key"], s["temperature"], s["reasoning_effort"]), ("max_completion_tokens", False, "low"))
        s = payload_style(openai, "gpt-4o-mini")
        self.assertEqual((s["tokens_key"], s["temperature"], s["reasoning_effort"]), ("max_completion_tokens", True, ""))
        s = payload_style({**openai, "reasoning_effort": "auto"}, "o3")
        self.assertEqual((s["tokens_key"], s["temperature"], s["reasoning_effort"]), ("max_completion_tokens", False, ""))
        s = payload_style({"base_url": "https://my.openai.azure.com", "provider": "azure"}, "gpt-4o")
        self.assertEqual((s["tokens_key"], s["temperature"]), ("max_completion_tokens", True))
        s = payload_style({"base_url": "http://127.0.0.1:11434/v1", "provider": "openai"}, "qwen2.5:7b")
        self.assertEqual((s["tokens_key"], s["temperature"]), ("max_tokens", True))         # ローカル互換サーバは従来どおり
        s = payload_style({"base_url": "http://127.0.0.1:11434/v1", "provider": "openai"}, "gpt-oss:20b")
        self.assertEqual((s["tokens_key"], s["temperature"]), ("max_completion_tokens", False))

    def test_local_model_sends_max_tokens_and_temperature(self):
        c = LLMClient(self.cfg, "local")
        c.chat("[TASK:ask] こんにちは", temperature=0.2)
        body = self.reqs[-1]["body"]
        self.assertEqual((body["max_tokens"], body["temperature"], body["model"]), (2048, 0.2, "mock"))
        self.assertNotIn("max_completion_tokens", body)
        self.assertNotIn("reasoning_effort", body)

    def test_reasoning_model_payload_and_self_correction(self):
        # gpt-5 系の名前 → 最初から max_completion_tokens・temperature なし・reasoning_effort あり
        c = LLMClient({**self.cfg, "model": "gpt-5-mini", "reasoning_effort": "minimal"}, "local")
        c.chat("[TASK:ask] こんにちは")
        body = self.reqs[-1]["body"]
        self.assertEqual((body["max_completion_tokens"], body["reasoning_effort"]), (4096, "minimal"))
        self.assertNotIn("max_tokens", body)
        self.assertNotIn("temperature", body)
        # 名前から分からないモデルが拒否したら、API の返事を見て直してやり直す（max_tokens → max_completion_tokens、temperature を外す）
        n = len(self.reqs)
        c = LLMClient({**self.cfg, "model": "strict-x"}, "local")
        text = c.chat("[TASK:ask] こんにちは", temperature=0.2)
        self.assertTrue(text)
        tries = [r["body"] for r in self.reqs[n:]]
        self.assertEqual(len(tries), 3)
        self.assertIn("max_tokens", tries[0])
        self.assertIn("max_completion_tokens", tries[1]); self.assertIn("temperature", tries[1])
        self.assertNotIn("temperature", tries[2]); self.assertEqual(tries[2]["max_completion_tokens"], 2048)
        self.assertEqual(c.last_payload_keys, ["max_completion_tokens", "model"])
        # reasoning_effort を知らないモデルなら外して続ける
        n = len(self.reqs)
        c = LLMClient({**self.cfg, "model": "strict-noeffort", "reasoning_effort": "low"}, "local")
        self.assertTrue(c.chat("[TASK:ask] こんにちは", temperature=1))
        last = self.reqs[-1]["body"]
        self.assertNotIn("reasoning_effort", last)
        self.assertIn("max_completion_tokens", last)


class Gpt5Test(unittest.TestCase):
    """gpt-5 系の実運用で起きること: 推論トークンで上限を使い切る、system を受け付けないモデル、verbosity、Azure の api-version。"""

    def setUp(self):
        self.srv, self.url, self.reqs = start_mock_llm()
        self.cfg = load_config(Path(tempfile.mkdtemp()) / "none.json")
        self.cfg.update({"base_url": self.url + "/v1", "model": "mock", "request_timeout": 5})

    def tearDown(self):
        self.srv.shutdown()

    def test_empty_answer_at_length_retries_with_bigger_budget(self):
        # strict-length は max_completion_tokens < 8192 だと空のまま finish_reason=length を返す（推論トークンで使い切った状態）。
        # 推論モデルの判定は名前で行うので、テスト用に strict-length も推論モデル扱いにする
        from mycelcore import llm as LLM
        orig = LLM._REASONING_RE
        LLM._REASONING_RE = re.compile(r"^(o[1-9](?:[.-]|$)|gpt-5(?!-chat)|gpt-oss|strict-length)", re.IGNORECASE)
        try:
            n = len(self.reqs)
            c = LLMClient({**self.cfg, "model": "strict-length"}, "local")
            self.assertTrue(c.chat("[TASK:ask] こんにちは"))
            tries = [r["body"] for r in self.reqs[n:]]
            self.assertEqual([t.get("max_completion_tokens") for t in tries], [4096, 16384])
            self.assertEqual(c.last_adjustments, ["max_completion_tokens=16384"])
        finally:
            LLM._REASONING_RE = orig

    def test_system_role_fallback_and_verbosity(self):
        n = len(self.reqs)
        c = LLMClient({**self.cfg, "model": "strict-nosystem"}, "local")
        self.assertTrue(c.chat("[TASK:ask] こんにちは", system="あなたは助手です", temperature=1))
        tries = [r["body"] for r in self.reqs[n:]]
        self.assertEqual(tries[0]["messages"][0]["role"], "system")
        last = tries[-1]["messages"]
        self.assertEqual(last[0]["role"], "user")
        self.assertTrue(last[0]["content"].startswith("あなたは助手です"))
        self.assertIn("system", c.last_adjustments)
        # verbosity は gpt-5 系だけに送り、知らないモデルには API の返事を見て外す
        from mycelcore.llm import payload_style
        self.assertEqual(payload_style({"base_url": "https://api.openai.com/v1", "verbosity": "low"}, "gpt-5-mini")["verbosity"], "low")
        self.assertEqual(payload_style({"base_url": "https://api.openai.com/v1", "verbosity": "low"}, "o3")["verbosity"], "")
        self.assertEqual(payload_style({"base_url": "https://api.openai.com/v1", "verbosity": "low"}, "gpt-4o")["verbosity"], "")
        c = LLMClient({**self.cfg, "model": "gpt-5-mini", "verbosity": "high", "reasoning_effort": "minimal"}, "local")
        c.chat("x")
        body = self.reqs[-1]["body"]
        self.assertEqual((body["verbosity"], body["reasoning_effort"], body["max_completion_tokens"]), ("high", "minimal", 4096))
        self.assertNotIn("temperature", body)

    def test_azure_reasoning_uses_newer_api_version(self):
        from mycelcore.llm import LLMClient as C
        cfg = {**self.cfg, "provider": "azure", "base_url": "http://127.0.0.1:1/", "model": "gpt-5-mini", "api_version": "2024-10-21", "_backend": "local"}
        url, _, _ = C(cfg)._endpoint("chat")
        self.assertIn("api-version=2025-04-01-preview", url)
        cfg["model"] = "gpt-4o"
        url, _, _ = C(cfg)._endpoint("chat")
        self.assertIn("api-version=2024-10-21", url)
        cfg["model"] = "o3-mini"; cfg["api_version"] = "2025-01-01-preview"
        url, _, _ = C(cfg)._endpoint("chat")
        self.assertIn("api-version=2025-01-01-preview", url)
        self.assertEqual(load_config(Path(tempfile.mkdtemp()) / "none.json")["ext_api_version"], "2025-04-01-preview")
