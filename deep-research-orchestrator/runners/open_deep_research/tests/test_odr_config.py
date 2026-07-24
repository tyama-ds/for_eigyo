"""open-deep-research Runnerの設定/環境変数ロジックのテスト (エンジン本体不要 / py3.11可)。"""

from __future__ import annotations

import json

import pytest
from odr_engine import (
    MCP_SEARCH_TOOL,
    EngineConfigError,
    OpenDeepResearchEngine,
    build_configurable,
    build_mcp_env,
    build_prompt,
    build_worker_env,
    build_worker_payload,
    resolve_max_react_tool_calls,
    validate_request,
)
from runner_core.models import LlmRunConfig, RunInput, RunRequest, SearchRunConfig

API_KEY = "sk-odr-secret-key-987654"


def make_request(**overrides) -> RunRequest:
    base = dict(
        client_run_id="run-2",
        engine_id="open-deep-research",
        input=RunInput(topic="水素製鉄の最新動向", objective="技術比較", language="ja"),
        options={},
        llm=LlmRunConfig(
            profile_id="p1",
            api="openai-compatible",
            endpoint="http://llm.internal:8000/v1",
            model="qwen3-32b",
            api_key=API_KEY,
        ),
        search=SearchRunConfig(
            provider="searxng", endpoint="http://searxng:8080", timeout_seconds=15, max_results=8
        ),
        proxy_env={},
    )
    base.update(overrides)
    return RunRequest(**base)


class TestValidation:
    def test_llm_none_fails_fast_japanese(self):
        with pytest.raises(EngineConfigError, match="LLM profileが未設定"):
            validate_request(make_request(llm=None))

    @pytest.mark.parametrize("api", ["tavily", "openai", "anthropic"])
    def test_hosted_search_apis_rejected(self, api):
        with pytest.raises(EngineConfigError, match="有料/hosted検索API"):
            validate_request(make_request(options={"search_api": api}))

    def test_unknown_search_api_rejected(self):
        with pytest.raises(EngineConfigError, match="未対応"):
            validate_request(make_request(options={"search_api": "duckduckgo"}))

    def test_search_api_none_allowed_without_searxng(self):
        validate_request(
            make_request(options={"search_api": "none"}, search=SearchRunConfig(provider="disabled"))
        )

    def test_search_disabled_without_explicit_none_rejected(self):
        with pytest.raises(EngineConfigError, match="SearXNGが無効"):
            validate_request(make_request(search=SearchRunConfig(provider="disabled")))

    def test_search_none_rejected(self):
        with pytest.raises(EngineConfigError, match="SearXNG"):
            validate_request(make_request(search=None))

    def test_missing_endpoint_rejected(self):
        req = make_request(search=SearchRunConfig(provider="searxng", endpoint=None))
        with pytest.raises(EngineConfigError, match="endpoint"):
            validate_request(req)

    def test_valid_passes(self):
        validate_request(make_request())


class TestWorkerEnv:
    def test_openai_compatible_env(self):
        env, _ = build_worker_env(make_request(), base_env={"PATH": "/usr/bin"})
        assert env["OPENAI_API_KEY"] == API_KEY
        assert env["OPENAI_BASE_URL"] == "http://llm.internal:8000/v1"
        assert env["OPENAI_API_BASE"] == "http://llm.internal:8000/v1"
        assert "ANTHROPIC_API_KEY" not in env

    def test_anthropic_env(self):
        req = make_request()
        req.llm.api = "anthropic"
        env, _ = build_worker_env(req, base_env={})
        assert env["ANTHROPIC_API_KEY"] == API_KEY
        assert env["ANTHROPIC_BASE_URL"] == "http://llm.internal:8000/v1"
        assert "OPENAI_API_KEY" not in env

    def test_no_search_provider_keys_and_no_config_overrides(self):
        req = make_request(
            proxy_env={
                "HTTPS_PROXY": "http://proxy:3128",
                "TAVILY_API_KEY": "injected",
                "SEARCH_API": "tavily",
                "ALLOW_CLARIFICATION": "true",
            }
        )
        env, warnings = build_worker_env(req, base_env={"TAVILY_API_KEY": "leak2"})
        assert "TAVILY_API_KEY" not in env
        assert "SEARCH_API" not in env
        assert "ALLOW_CLARIFICATION" not in env
        assert env["HTTPS_PROXY"] == "http://proxy:3128"
        assert len(warnings) == 3

    def test_no_proxy_includes_localhost_when_proxy_set(self):
        req = make_request(proxy_env={"HTTPS_PROXY": "http://proxy:3128"})
        env, _ = build_worker_env(req, base_env={})
        assert "127.0.0.1" in env["NO_PROXY"]
        assert "localhost" in env["no_proxy"]

    def test_no_proxy_vars_absent_when_no_proxy_configured(self):
        env, _ = build_worker_env(make_request(), base_env={})
        assert "NO_PROXY" not in env

    def test_mcp_env_has_searxng_but_no_secrets(self):
        env = build_mcp_env(make_request(), port=12345, base_env={"PATH": "/usr/bin"})
        assert env["SEARXNG_ENDPOINT"] == "http://searxng:8080"
        assert env["SEARXNG_TIMEOUT"] == "15"
        assert env["SEARXNG_MAX_RESULTS"] == "8"
        assert env["MCP_PORT"] == "12345"
        assert API_KEY not in json.dumps(env)


class TestConfigurable:
    def test_headless_and_search_none(self):
        conf = build_configurable(make_request(), "http://127.0.0.1:9999")
        assert conf["allow_clarification"] is False
        assert conf["search_api"] == "none"

    def test_all_four_models_set(self):
        conf = build_configurable(make_request(), None)
        for field in (
            "research_model",
            "summarization_model",
            "compression_model",
            "final_report_model",
        ):
            assert conf[field] == "openai:qwen3-32b"

    def test_anthropic_prefix(self):
        req = make_request()
        req.llm.api = "anthropic"
        req.llm.model = "claude-x"
        conf = build_configurable(req, None)
        assert conf["research_model"] == "anthropic:claude-x"

    def test_mcp_config_wired(self):
        conf = build_configurable(make_request(), "http://127.0.0.1:9999")
        assert conf["mcp_config"] == {
            "url": "http://127.0.0.1:9999",
            "tools": [MCP_SEARCH_TOOL],
            "auth_required": False,
        }
        assert MCP_SEARCH_TOOL in conf["mcp_prompt"]

    def test_no_mcp_when_url_none(self):
        conf = build_configurable(make_request(), None)
        assert "mcp_config" not in conf and "mcp_prompt" not in conf

    def test_max_react_tool_calls_capped_by_max_searches(self):
        assert resolve_max_react_tool_calls(make_request(max_searches=2)) == 2
        assert resolve_max_react_tool_calls(make_request(max_searches=100)) == 5
        assert (
            resolve_max_react_tool_calls(
                make_request(options={"max_react_tool_calls": 8})
            )
            == 8
        )

    def test_payload_has_no_api_key(self):
        payload, _ = build_worker_payload(make_request(), "http://127.0.0.1:9999")
        assert API_KEY not in json.dumps(payload)

    def test_prompt_language_instruction(self):
        prompt = build_prompt(RunInput(topic="T", language="ja"))
        assert "Japanese" in prompt
        payload, _ = build_worker_payload(make_request(), None)
        assert "水素製鉄" in payload["prompt"]

    def test_search_none_option_warns(self):
        req = make_request(
            options={"search_api": "none"}, search=SearchRunConfig(provider="disabled")
        )
        _, warnings = build_worker_payload(req, None)
        assert any("検索なし" in w for w in warnings)


class TestCapabilities:
    def test_capabilities(self):
        caps = OpenDeepResearchEngine().capabilities()
        assert caps.engine_id == "open-deep-research"
        assert caps.health == "available"
        assert caps.required_config == ["llm", "search:searxng"]
        assert caps.cost is False
        assert caps.token_usage is True
