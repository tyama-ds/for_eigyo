"""JSONL workerサブプロセス実行ループのテスト (スタブworker使用、gpt_researcher不要)。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from gptr_engine import GptResearcherEngine, run_jsonl_worker
from runner_core.engine import CancelledByUser, RunContext
from runner_core.models import LlmRunConfig, RunInput, RunRequest, SearchRunConfig

from test_gptr_env import API_KEY, make_request

STUB_OK = """\
import json, sys
payload = json.load(sys.stdin)
print(json.dumps({"kind": "event", "type": "stage", "payload": {"stage": "conduct_research"}}))
print("stray library output that must be ignored")
print(json.dumps({"kind": "event", "type": "source_found", "payload": {"url": "https://example.org/a"}}))
print(json.dumps({
    "kind": "result",
    "output_kind": "report",
    "report_markdown": "# report for " + payload["query"][:10],
    "sources": [{"url": "https://example.org/a", "title": "A"}],
    "metrics": {"sources": 1, "llm_cost_usd": 0.01, "llm_cost_is_estimate": True,
                "search_api_cost_usd": 0.0, "infra_cost": "not_measured"},
    "warnings": ["w"],
    "raw": {"engine": "stub"},
}))
"""

STUB_FATAL = """\
import json, sys, os
json.load(sys.stdin)
print(json.dumps({"kind": "fatal", "error": "boom: " + os.environ.get("SECRET_IN_ERROR", "")}))
sys.exit(1)
"""

STUB_HANG = """\
import json, sys, time
json.load(sys.stdin)
print(json.dumps({"kind": "event", "type": "log", "payload": {"message": "started"}}), flush=True)
time.sleep(60)
"""

STUB_CRASH = """\
import sys
sys.stderr.write("traceback with sk-secret-test-key-123456 inside\\n")
sys.exit(2)
"""


def write_stub(tmp_path: Path, code: str) -> Path:
    path = tmp_path / "stub_worker.py"
    path.write_text(code)
    return path


def make_ctx(request: RunRequest | None = None) -> RunContext:
    return RunContext("run-1", request or make_request())


def run(coro):
    return asyncio.run(coro)


class TestRunJsonlWorker:
    def test_events_and_result(self, tmp_path):
        ctx = make_ctx()
        stub = write_stub(tmp_path, STUB_OK)
        import sys as _sys

        result = run(
            run_jsonl_worker(
                ctx,
                argv=[_sys.executable, "-u", str(stub)],
                env={"PATH": "/usr/bin:/bin"},
                payload={"query": "topic-xyz-123"},
                cwd=str(tmp_path),
                secrets=[API_KEY],
            )
        )
        assert result["kind"] == "result"
        assert "topic-xyz-" in result["report_markdown"]
        types = [e.type for e in ctx.events]
        assert "stage" in types and "source_found" in types

    def test_fatal_raises_runtime_error(self, tmp_path):
        ctx = make_ctx()
        stub = write_stub(tmp_path, STUB_FATAL)
        import sys as _sys

        with pytest.raises(RuntimeError, match="boom"):
            run(
                run_jsonl_worker(
                    ctx,
                    argv=[_sys.executable, "-u", str(stub)],
                    env={"PATH": "/usr/bin:/bin", "SECRET_IN_ERROR": API_KEY},
                    payload={},
                    cwd=str(tmp_path),
                    secrets=[API_KEY],
                )
            )
        # 例外メッセージにAPIキーが混入しない
        try:
            run(
                run_jsonl_worker(
                    ctx,
                    argv=[_sys.executable, "-u", str(stub)],
                    env={"PATH": "/usr/bin:/bin", "SECRET_IN_ERROR": API_KEY},
                    payload={},
                    cwd=str(tmp_path),
                    secrets=[API_KEY],
                )
            )
        except RuntimeError as e:
            assert API_KEY not in str(e)

    def test_crash_without_result_includes_redacted_stderr(self, tmp_path):
        ctx = make_ctx()
        stub = write_stub(tmp_path, STUB_CRASH)
        import sys as _sys

        with pytest.raises(RuntimeError) as excinfo:
            run(
                run_jsonl_worker(
                    ctx,
                    argv=[_sys.executable, "-u", str(stub)],
                    env={"PATH": "/usr/bin:/bin"},
                    payload={},
                    cwd=str(tmp_path),
                    secrets=[API_KEY],
                )
            )
        message = str(excinfo.value)
        assert "結果を返さず" in message
        assert API_KEY not in message
        assert "***" in message

    def test_cancellation_terminates_worker(self, tmp_path):
        ctx = make_ctx()
        stub = write_stub(tmp_path, STUB_HANG)
        import sys as _sys

        async def scenario():
            task = asyncio.ensure_future(
                run_jsonl_worker(
                    ctx,
                    argv=[_sys.executable, "-u", str(stub)],
                    env={"PATH": "/usr/bin:/bin"},
                    payload={},
                    cwd=str(tmp_path),
                    secrets=[],
                )
            )
            await asyncio.sleep(1.0)  # workerの起動とlogイベントを待つ
            ctx.request_cancel()
            with pytest.raises(CancelledByUser):
                await asyncio.wait_for(task, timeout=10)

        run(scenario())
        assert any(e.type == "log" for e in ctx.events)


class TestEngineWithStubWorker:
    def test_engine_run_end_to_end_with_stub(self, tmp_path):
        stub = write_stub(tmp_path, STUB_OK)
        engine = GptResearcherEngine(worker_path=stub)
        ctx = make_ctx()
        result = run(engine.run(ctx))
        assert result.output_kind == "report"
        assert result.sources[0].url == "https://example.org/a"
        assert result.metrics.llm_cost_is_estimate is True
        assert "w" in result.warnings

    def test_engine_fails_fast_without_llm(self, tmp_path):
        engine = GptResearcherEngine(worker_path=write_stub(tmp_path, STUB_OK))
        ctx = make_ctx(make_request(llm=None))
        with pytest.raises(ValueError, match="LLM profileが未設定"):
            run(engine.run(ctx))

    def test_capabilities(self):
        caps = GptResearcherEngine().capabilities()
        assert caps.engine_id == "gpt-researcher"
        assert caps.health == "available"
        assert caps.required_config == ["llm", "search:searxng"]
        assert caps.token_usage is False
        assert caps.cost is True
