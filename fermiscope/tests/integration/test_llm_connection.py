"""生成AI接続の回帰テスト。

再現した障害: 環境変数 HTTPS_PROXY が LLM 設定に焼き込まれ、NO_PROXY を無視して
localhost のローカルLLMまでプロキシ経由になり接続不能だった。ここでは実際の
ローカルHTTPサーバ(外部ネットワークなし)で接続フロー全体を検証する。
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from fermiscope.llm.settings_store import LLMSettingsStore


class _OpenAICompatHandler(BaseHTTPRequestHandler):
    """OpenAI互換のモックサーバ。挙動はクラス属性で切替える。"""

    mode = "ok"  # ok | reject_response_format | error_404

    def do_POST(self):  # noqa: N802 — http.server の規約
        n = int(self.headers.get("content-length", 0))
        body = json.loads(self.rfile.read(n) or b"{}")
        if self.mode == "error_404":
            self._send(404, {"error": {"message": "model 'nonexistent' not found"}})
            return
        if self.mode == "reject_response_format" and "response_format" in body:
            self._send(400, {"error": {"message": "response_format is not supported"}})
            return
        self._send(
            200,
            {"choices": [{"message": {"content": json.dumps({"subject": "テスト"})}}]},
        )

    def _send(self, status: int, payload: dict):
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):  # noqa: D102 — テストログを汚さない
        pass


@pytest.fixture()
def local_llm_server():
    """127.0.0.1 上の OpenAI 互換モックサーバ(空きポートに起動)。"""
    server = HTTPServer(("127.0.0.1", 0), _OpenAICompatHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()


def _store(tmp_path: Path) -> LLMSettingsStore:
    return LLMSettingsStore(tmp_path / "llm.json", env={})


async def _configure(store: LLMSettingsStore, port: int):
    await store.update(
        {
            "provider": "openai_compatible",
            "api_base": f"http://127.0.0.1:{port}/v1",
            "model": "test-model",
        }
    )


async def test_local_llm_reachable_despite_corporate_proxy_env(
    tmp_path, local_llm_server, monkeypatch
):
    """再現ケース: HTTPS_PROXY/HTTP_PROXY 環境下でも localhost の LLM に接続できる。

    NO_PROXY に 127.0.0.1 が含まれていれば、環境変数プロキシ(到達不能なアドレス)を
    経由せず直接接続する。修正前は proxy が設定に焼き込まれて接続不能だった。
    """
    _OpenAICompatHandler.mode = "ok"
    # 到達不能なプロキシを環境に設定(修正前はこれを経由して失敗していた)
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")  # discard port
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("NO_PROXY", "localhost,127.0.0.1")
    store = _store(tmp_path)
    await _configure(store, local_llm_server.server_address[1])
    ok, msg = await store.test_connection()
    assert ok, f"ローカルLLMへ接続できるはず: {msg}"
    # 保存された設定に環境変数プロキシが焼き込まれていない
    assert store.config.proxy == ""


async def test_env_proxy_not_baked_into_saved_config(tmp_path, monkeypatch):
    """環境変数の一般プロキシは設定ファイルに保存しない(LLM_PROXY のみ保存)。"""
    env = {"HTTPS_PROXY": "http://corp:3128", "LLM_PROVIDER": "noop"}
    store = LLMSettingsStore(tmp_path / "llm.json", env=env)
    assert store.config.proxy == ""  # HTTPS_PROXY は焼き込まない
    env2 = {"LLM_PROXY": "http://llm-proxy:9", "LLM_PROVIDER": "noop"}
    store2 = LLMSettingsStore(tmp_path / "llm2.json", env=env2)
    assert store2.config.proxy == "http://llm-proxy:9"  # 明示指定のみ保存


async def test_response_format_unsupported_fallback(tmp_path, local_llm_server, monkeypatch):
    """response_format 非対応(400)のローカルLLMサーバでも、外して再試行し成功する。"""
    _OpenAICompatHandler.mode = "reject_response_format"
    monkeypatch.setenv("NO_PROXY", "localhost,127.0.0.1")
    store = _store(tmp_path)
    await _configure(store, local_llm_server.server_address[1])
    ok, msg = await store.test_connection()
    assert ok, f"response_format フォールバックで成功するはず: {msg}"


async def test_connection_failure_shows_status_and_route(tmp_path, local_llm_server, monkeypatch):
    """失敗時は HTTP ステータスと接続先が表示される(原因を隠さない)。"""
    _OpenAICompatHandler.mode = "error_404"
    monkeypatch.setenv("NO_PROXY", "localhost,127.0.0.1")
    store = _store(tmp_path)
    await _configure(store, local_llm_server.server_address[1])
    ok, msg = await store.test_connection()
    assert not ok
    assert "HTTP 404" in msg  # ステータスが見える
    assert "接続先" in msg  # どこへ繋ごうとしたかが見える
    assert "not found" in msg  # サーバのエラーメッセージ(モデル不在)が見える


async def test_no_secret_leak_in_diagnostics(tmp_path, monkeypatch):
    """診断メッセージにプロキシ資格情報・APIキーが漏れない。"""
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    store = _store(tmp_path)
    await store.update(
        {
            "provider": "openai_compatible",
            "api_base": "http://198.51.100.1:9/v1",  # 到達不能(TEST-NET)
            "model": "m",
            "api_key": "sk-VERY-SECRET",
            "proxy": "http://user:proxypass@127.0.0.1:9",
            "timeout_seconds": 1.0,
        }
    )
    ok, msg = await store.test_connection()
    assert not ok
    assert "proxypass" not in msg and "sk-VERY-SECRET" not in msg
    # プロキシは資格情報を伏せた形で表示される
    assert "127.0.0.1:9" in msg


async def test_provider_connection_info_masks_credentials(monkeypatch):
    """connection_info はプロキシの資格情報を含まない。"""
    from fermiscope.llm.openai_compat import OpenAICompatProvider

    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    p = OpenAICompatProvider(
        api_base="https://api.example.com/v1",
        api_key="k",
        model="m",
        proxy="http://user:secret@proxy.corp:3128",
    )
    info = p.connection_info()
    assert info["proxy"] == "http://proxy.corp:3128"
    assert "secret" not in str(info)
