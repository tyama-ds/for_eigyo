"""LLM接続設定(GUI経由)の統合テスト。"""

from __future__ import annotations

import httpx
import pytest

from fermiscope.api.app import create_app
from fermiscope.config import load_settings
from fermiscope.llm.anthropic_provider import AnthropicProvider
from fermiscope.llm.openai_compat import OpenAICompatProvider
from fermiscope.llm.settings_store import LLMRuntimeConfig, LLMSettingsStore, build_provider


@pytest.fixture()
def settings_client(tmp_path):
    from fastapi.testclient import TestClient

    s = load_settings()
    s.database_url = f"sqlite:///{tmp_path}/t.db"
    import os

    os.environ["FERMISCOPE_LLM_SETTINGS_PATH"] = str(tmp_path / "llm.json")
    # llm を注入しない → store 経由(編集可能)
    app = create_app(settings=s)
    with TestClient(app) as client:
        yield client


def test_get_and_switch_provider(settings_client):
    cur = settings_client.get("/api/settings/llm").json()
    assert cur["editable"] is True
    assert cur["current"]["provider"] == "noop"
    assert any(p["value"] == "anthropic" for p in cur["providers"])
    assert any(p["value"] == "openai_compatible" for p in cur["providers"])

    r = settings_client.put("/api/settings/llm", json={
        "provider": "openai_compatible", "api_base": "http://localhost:9/v1",
        "model": "gpt-test", "api_key": "sk-secret-1",
    })
    assert r.status_code == 200
    # キーは返らない(有無のみ)
    assert "sk-secret-1" not in r.text
    assert r.json()["current"]["key_set"] is True
    assert r.json()["current"]["model"] == "gpt-test"
    # config に反映
    assert settings_client.get("/api/config").json()["llm_provider"] == "openai_compatible"


def test_switch_to_anthropic_and_key_retained(settings_client):
    settings_client.put("/api/settings/llm", json={
        "provider": "anthropic", "model": "claude-sonnet-5", "api_key": "ak-1",
    })
    # 空キーで再更新 → 既存キーを維持(key_set=True のまま)
    r = settings_client.put("/api/settings/llm", json={"provider": "anthropic", "model": "claude-opus-4-8"})
    assert r.json()["current"]["key_set"] is True
    assert r.json()["current"]["model"] == "claude-opus-4-8"


def test_invalid_provider_rejected(settings_client):
    r = settings_client.put("/api/settings/llm", json={"provider": "bogus"})
    assert r.status_code == 400


def test_incomplete_openai_config_rejected(settings_client):
    # base/model 欠如で構築不能 → 400、設定は変更されない
    r = settings_client.put("/api/settings/llm", json={"provider": "openai_compatible"})
    assert r.status_code == 400
    assert settings_client.get("/api/settings/llm").json()["current"]["provider"] == "noop"


def test_injected_llm_not_editable():
    from fastapi.testclient import TestClient

    from fermiscope.llm import NoOpLLMProvider

    s = load_settings()
    app = create_app(settings=s, llm=NoOpLLMProvider())
    with TestClient(app) as client:
        assert client.get("/api/settings/llm").json()["editable"] is False
        assert client.put("/api/settings/llm", json={"provider": "anthropic"}).status_code == 409


def test_settings_persist_across_reload(tmp_path):
    path = tmp_path / "llm.json"
    store = LLMSettingsStore(path, env={})
    import asyncio

    asyncio.run(store.update({"provider": "anthropic", "model": "claude-sonnet-5", "api_key": "ak-9"}))
    # 別インスタンスで読み直しても保持される(キー含む=サーバ内保存)
    store2 = LLMSettingsStore(path, env={})
    assert store2.config.provider == "anthropic"
    assert store2.config.model == "claude-sonnet-5"
    assert store2.config.api_key == "ak-9"


# ---- プロバイダアダプタ契約(モックトランスポート)----

@pytest.mark.asyncio
async def test_openai_provider_parses_json_response():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer k1"
        return httpx.Response(200, json={
            "choices": [{"message": {"content": '{"queries_ja":["東京都 人口"],"queries_en":["tokyo population"]}'}}]
        })

    p = OpenAICompatProvider(api_base="https://api.example/v1", api_key="k1", model="m",
                             transport=httpx.MockTransport(handler))
    res = await p.propose_queries("人口", "総人口", "東京都")
    assert res is not None and res.queries_ja == ["東京都 人口"]
    await p.close()


@pytest.mark.asyncio
async def test_anthropic_provider_parses_content_blocks():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["x-api-key"] == "ak-1"
        assert request.headers["anthropic-version"]
        assert str(request.url).endswith("/v1/messages")
        return httpx.Response(200, json={
            "content": [{"type": "text", "text": '{"queries_ja":["医者 人数"],"queries_en":["doctors count"]}'}]
        })

    p = AnthropicProvider(api_key="ak-1", model="claude-sonnet-5",
                          transport=httpx.MockTransport(handler))
    res = await p.propose_queries("医者数", "医師の総数", "日本")
    assert res is not None and res.queries_ja == ["医者 人数"]
    await p.close()


@pytest.mark.asyncio
async def test_anthropic_handles_json_in_code_fence():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "content": [{"type": "text", "text": '```json\n{"text":"要約です"}\n```'}]
        })

    p = AnthropicProvider(api_key="ak-1", model="m", transport=httpx.MockTransport(handler))
    res = await p.draft_explanation("結果概要")
    assert res == "要約です"
    await p.close()


@pytest.mark.asyncio
async def test_anthropic_error_no_key_leak():
    secret = "ak-super-secret"

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    p = AnthropicProvider(api_key=secret, model="m", transport=httpx.MockTransport(handler))

    res = await p.classify_question("質問")
    assert res is None
    await p.close()


def test_build_provider_from_config():
    assert build_provider(LLMRuntimeConfig(provider="noop")).name == "noop"
    p = build_provider(LLMRuntimeConfig(provider="anthropic", model="m", api_key="k"))
    assert p.name == "anthropic"
