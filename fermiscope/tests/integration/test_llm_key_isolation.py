"""Phase 1-6: LLM接続先変更時のAPIキー漏えい防止のテスト。"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from fermiscope.llm.settings_store import LLMSettingsStore, build_provider


def test_explicit_empty_key_not_backfilled_from_env(monkeypatch):
    """接続先だけ変更し保存状態が key_set=false のとき、実HTTPクライアントに
    Authorization ヘッダーが存在しない(環境変数の旧キーを流用しない)。"""
    monkeypatch.setenv("LLM_API_KEY", "sk-OLD-ENV-KEY")
    monkeypatch.setenv("LLM_PROVIDER", "openai_compatible")
    monkeypatch.setenv("LLM_API_BASE", "http://old.example/v1")
    monkeypatch.setenv("LLM_MODEL", "m")

    with tempfile.TemporaryDirectory() as d:
        store = LLMSettingsStore(Path(d) / "llm.json")
        assert store.config.api_key == "sk-OLD-ENV-KEY"  # 起動時に一度だけ解決
        # 接続先(api_base)だけを別サーバへ、新キーなしで変更
        cfg = asyncio.run(store.update({"api_base": "http://attacker.example/v1"}))
        assert cfg.api_key == ""  # 旧キーは新接続先へ流用されない
        assert store.config.public_dict()["key_set"] is False

        # 実プロバイダに Authorization ヘッダーが無いこと(httpx Headers は大小無視)
        provider = build_provider(store.config)
        assert "authorization" not in provider._client.headers
        asyncio.run(provider.close())


def test_provider_explicit_empty_key_has_no_auth_header(monkeypatch):
    from fermiscope.llm.openai_compat import OpenAICompatProvider

    monkeypatch.setenv("LLM_API_KEY", "sk-should-not-be-used")
    # 明示的な空キー("")は環境変数で補完されない
    p = OpenAICompatProvider(api_base="http://x/v1", api_key="", model="m")
    assert "authorization" not in p._client.headers


def test_provider_none_key_uses_env(monkeypatch):
    from fermiscope.llm.openai_compat import OpenAICompatProvider

    monkeypatch.setenv("LLM_API_KEY", "sk-env")
    # None(未指定)は従来どおり環境変数から解決
    p = OpenAICompatProvider(api_base="http://x/v1", api_key=None, model="m")
    assert p._client.headers.get("authorization") == "Bearer sk-env"


def test_clear_api_key_removes_stored_key(tmp_path):
    store = LLMSettingsStore(Path(tmp_path) / "llm.json", env={})
    asyncio.run(
        store.update(
            {"provider": "openai_compatible", "api_base": "http://x/v1", "model": "m", "api_key": "k"}
        )
    )
    assert store.config.api_key == "k"
    # 明示削除
    cfg = asyncio.run(store.update({"clear_api_key": True}))
    assert cfg.api_key == ""


def test_host_guard_rejects_foreign_host(tmp_path):
    from fastapi.testclient import TestClient

    from fermiscope.api.app import create_app
    from fermiscope.config import load_settings
    from fermiscope.llm import NoOpLLMProvider

    s = load_settings(env={})
    s.database_url = f"sqlite:///{tmp_path}/t.db"
    app = create_app(settings=s, llm=NoOpLLMProvider())
    with TestClient(app) as client:
        # DNSリバインディングを模した外部ホスト → 400
        r = client.get("/api/config", headers={"host": "attacker.example"})
        assert r.status_code == 400
        # 通常の localhost は許可
        assert client.get("/api/config", headers={"host": "127.0.0.1:8720"}).status_code == 200
