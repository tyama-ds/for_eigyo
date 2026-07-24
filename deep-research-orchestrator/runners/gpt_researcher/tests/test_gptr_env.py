"""gpt-researcher Runnerの純ロジックテスト (gpt_researcherパッケージ不要 / py3.11可)。"""

from __future__ import annotations

import json
import sys

import pytest
from gptr_engine import (
    EngineConfigError,
    GptResearcherEngine,
    build_query,
    build_worker_env,
    build_worker_payload,
    parse_worker_line,
    redact_secrets,
    resolve_max_iterations,
    result_from_worker_payload,
    validate_request,
)
from runner_core.models import LlmRunConfig, RunInput, RunRequest, SearchRunConfig

API_KEY = "sk-secret-test-key-123456"


def make_request(**overrides) -> RunRequest:
    base = dict(
        client_run_id="run-1",
        engine_id="gpt-researcher",
        input=RunInput(topic="国内鉄鋼市場の動向", objective="価格見通し", language="ja"),
        options={},
        llm=LlmRunConfig(
            profile_id="p1",
            api="openai-compatible",
            endpoint="http://llm.internal:8000/v1",
            model="qwen3-32b",
            api_key=API_KEY,
            embedding_model="bge-m3",
        ),
        search=SearchRunConfig(
            provider="searxng", endpoint="http://searxng:8080", max_results=7
        ),
        proxy_env={"HTTP_PROXY": "http://proxy:3128", "NO_PROXY": "internal"},
    )
    base.update(overrides)
    return RunRequest(**base)


class TestValidation:
    def test_llm_none_fails_fast_japanese(self):
        with pytest.raises(EngineConfigError, match="LLM profileが未設定"):
            validate_request(make_request(llm=None))

    def test_anthropic_api_rejected(self):
        req = make_request()
        req.llm.api = "anthropic"
        with pytest.raises(EngineConfigError, match="openai-compatible"):
            validate_request(req)

    def test_search_disabled_rejected(self):
        req = make_request(search=SearchRunConfig(provider="disabled"))
        with pytest.raises(EngineConfigError, match="SearXNGが無効"):
            validate_request(req)

    def test_search_none_rejected(self):
        with pytest.raises(EngineConfigError, match="SearXNG"):
            validate_request(make_request(search=None))

    def test_missing_searxng_endpoint_rejected(self):
        req = make_request(search=SearchRunConfig(provider="searxng", endpoint=None))
        with pytest.raises(EngineConfigError, match="endpoint"):
            validate_request(req)

    def test_missing_embedding_model_rejected(self):
        req = make_request()
        req.llm.embedding_model = None
        with pytest.raises(EngineConfigError, match="embedding"):
            validate_request(req)

    def test_unknown_report_type_rejected(self):
        req = make_request(options={"report_type": "deep"})
        with pytest.raises(EngineConfigError, match="report_type"):
            validate_request(req)

    def test_valid_request_passes(self):
        validate_request(make_request())


class TestWorkerEnv:
    def test_searx_retriever_always_set(self):
        env, _ = build_worker_env(make_request(), base_env={"PATH": "/usr/bin"})
        assert env["RETRIEVER"] == "searx"
        assert env["SEARX_URL"] == "http://searxng:8080"
        assert env["SCRAPER"] == "bs"
        assert env["MAX_SEARCH_RESULTS_PER_QUERY"] == "7"

    def test_llm_env(self):
        env, _ = build_worker_env(make_request(), base_env={})
        assert env["OPENAI_BASE_URL"] == "http://llm.internal:8000/v1"
        assert env["OPENAI_API_KEY"] == API_KEY
        assert env["FAST_LLM"] == "openai:qwen3-32b"
        assert env["SMART_LLM"] == "openai:qwen3-32b"
        assert env["STRATEGIC_LLM"] == "openai:qwen3-32b"
        assert env["EMBEDDING"] == "openai:bge-m3"

    def test_no_tavily_or_other_search_keys(self):
        env, _ = build_worker_env(
            make_request(), base_env={"TAVILY_API_KEY": "leak", "PATH": "/usr/bin"}
        )
        for key in env:
            assert "TAVILY" not in key
            assert "EXA" not in key
            assert "SERP" not in key

    def test_proxy_env_merged_but_forbidden_keys_dropped(self):
        req = make_request(
            proxy_env={
                "HTTPS_PROXY": "http://proxy:3128",
                "TAVILY_API_KEY": "injected",
                "RETRIEVER": "tavily",
                "OPENAI_API_KEY": "injected",
            }
        )
        env, warnings = build_worker_env(req, base_env={})
        assert env["HTTPS_PROXY"] == "http://proxy:3128"
        assert "TAVILY_API_KEY" not in env
        assert env["RETRIEVER"] == "searx"  # 上書き不可
        assert env["OPENAI_API_KEY"] == API_KEY  # 上書き不可
        assert len(warnings) == 3

    def test_missing_api_key_uses_dummy(self):
        req = make_request()
        req.llm.api_key = None
        env, _ = build_worker_env(req, base_env={})
        assert env["OPENAI_API_KEY"] == "dummy-key"

    def test_language_mapped(self):
        env, _ = build_worker_env(make_request(), base_env={})
        assert env["LANGUAGE"] == "japanese"

    def test_embedding_endpoint_mismatch_warns(self):
        req = make_request()
        req.llm.embedding_endpoint = "http://other:9999/v1"
        _, warnings = build_worker_env(req, base_env={})
        assert any("embedding_endpoint" in w for w in warnings)

    def test_max_iterations_capped_by_max_searches(self):
        assert resolve_max_iterations(make_request(max_searches=2)) == 2
        assert resolve_max_iterations(make_request(max_searches=100)) == 3
        assert (
            resolve_max_iterations(
                make_request(options={"max_iterations": 5}, max_searches=None)
            )
            == 5
        )
        env, _ = build_worker_env(make_request(max_searches=1), base_env={})
        assert env["MAX_ITERATIONS"] == "1"


class TestWorkerPayload:
    def test_api_key_never_in_payload_or_argv(self):
        req = make_request()
        payload, _ = build_worker_payload(req)
        assert API_KEY not in json.dumps(payload)
        engine = GptResearcherEngine()
        argv = [sys.executable, "-u", str(engine._worker_path)]
        assert all(API_KEY not in a for a in argv)

    def test_query_composition(self):
        q = build_query(
            RunInput(topic="topic-A", objective="obj-B", instructions="inst-C")
        )
        assert "topic-A" in q and "obj-B" in q and "inst-C" in q

    def test_documents_ignored_with_warning(self):
        req = make_request()
        req.input.documents = [{"name": "a.txt", "text": "x"}]
        _, warnings = build_worker_payload(req)
        assert any("documents" in w for w in warnings)

    def test_defaults(self):
        payload, _ = build_worker_payload(make_request())
        assert payload["report_type"] == "research_report"
        assert payload["input_urls"] == []


class TestJsonlParsing:
    def test_event_line(self):
        msg = parse_worker_line(
            '{"kind":"event","type":"log","payload":{"message":"hi"}}\n'
        )
        assert msg == {"kind": "event", "type": "log", "payload": {"message": "hi"}}

    def test_non_json_and_noise_lines_ignored(self):
        assert parse_worker_line("some library print output") is None
        assert parse_worker_line("") is None
        assert parse_worker_line('{"no_kind": 1}') is None
        assert parse_worker_line('{"broken json') is None
        assert parse_worker_line("[1,2,3]") is None

    def test_result_mapping(self):
        result = result_from_worker_payload(
            {
                "kind": "result",
                "output_kind": "report",
                "report_markdown": "# R",
                "sources": [{"url": "https://example.org", "title": "t"}],
                "metrics": {"sources": 1, "llm_cost_usd": 0.5, "llm_cost_is_estimate": True},
                "warnings": ["w1"],
            }
        )
        assert result.output_kind == "report"
        assert result.claims == []
        assert result.sources[0].url == "https://example.org"
        assert result.metrics.llm_cost_is_estimate is True
        assert result.metrics.prompt_tokens is None

    def test_redact_secrets(self):
        assert API_KEY not in redact_secrets(f"error with {API_KEY}", [API_KEY])
        assert redact_secrets("no secret", [None, "short"]) == "no secret"
