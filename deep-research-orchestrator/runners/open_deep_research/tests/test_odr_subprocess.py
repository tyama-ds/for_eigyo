"""ODR EngineのJSONL workerループ/検索なしモードのテスト (スタブworker使用)。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from odr_engine import OpenDeepResearchEngine, parse_worker_line, run_jsonl_worker
from runner_core.engine import CancelledByUser, RunContext
from runner_core.models import SearchRunConfig

from test_odr_config import API_KEY, make_request

STUB_OK = """\
import json, sys
payload = json.load(sys.stdin)
conf = payload["configurable"]
assert conf["allow_clarification"] is False
assert conf["search_api"] == "none"
print(json.dumps({"kind": "event", "type": "stage", "payload": {"stage": "supervisor"}}))
print(json.dumps({
    "kind": "result",
    "output_kind": "report",
    "report_markdown": "# R\\n\\n### Sources\\n[1] T: https://example.org/s",
    "sources": [{"url": "https://example.org/s", "title": "T"}],
    "metrics": {"sources": 1, "search_api_cost_usd": 0.0, "infra_cost": "not_measured"},
    "warnings": [],
    "raw": {"engine": "stub"},
}))
"""

STUB_HANG = """\
import json, sys, time
json.load(sys.stdin)
print(json.dumps({"kind": "event", "type": "log", "payload": {"message": "working"}}), flush=True)
time.sleep(60)
"""


def write_stub(tmp_path: Path, code: str) -> Path:
    path = tmp_path / "stub_odr_worker.py"
    path.write_text(code)
    return path


def run(coro):
    return asyncio.run(coro)


class TestEngineWithStubWorker:
    def test_search_none_mode_runs_without_mcp(self, tmp_path):
        """search_api="none" 明示時はMCPサーバーを起動せずworkerのみ実行する。"""
        stub = write_stub(tmp_path, STUB_OK)
        engine = OpenDeepResearchEngine(
            worker_path=stub, mcp_server_path=tmp_path / "does-not-exist.py"
        )
        req = make_request(
            options={"search_api": "none"}, search=SearchRunConfig(provider="disabled")
        )
        ctx = RunContext("run-x", req)
        result = run(engine.run(ctx))
        assert result.output_kind == "report"
        assert result.sources[0].url == "https://example.org/s"
        assert any("検索なし" in w for w in result.warnings)
        assert any(e.type == "stage" for e in ctx.events)

    def test_fails_fast_without_llm(self, tmp_path):
        engine = OpenDeepResearchEngine(worker_path=write_stub(tmp_path, STUB_OK))
        ctx = RunContext("run-x", make_request(llm=None))
        with pytest.raises(ValueError, match="LLM profileが未設定"):
            run(engine.run(ctx))

    def test_fails_fast_with_hosted_search_api(self, tmp_path):
        engine = OpenDeepResearchEngine(worker_path=write_stub(tmp_path, STUB_OK))
        ctx = RunContext("run-x", make_request(options={"search_api": "tavily"}))
        with pytest.raises(ValueError, match="有料/hosted検索API"):
            run(engine.run(ctx))

    def test_cancellation(self, tmp_path):
        stub = write_stub(tmp_path, STUB_HANG)
        req = make_request(
            options={"search_api": "none"}, search=SearchRunConfig(provider="disabled")
        )
        ctx = RunContext("run-x", req)
        import sys as _sys

        async def scenario():
            task = asyncio.ensure_future(
                run_jsonl_worker(
                    ctx,
                    argv=[_sys.executable, "-u", str(stub)],
                    env={"PATH": "/usr/bin:/bin"},
                    payload={"configurable": {}},
                    cwd=str(tmp_path),
                    secrets=[API_KEY],
                )
            )
            await asyncio.sleep(1.0)
            ctx.request_cancel()
            with pytest.raises(CancelledByUser):
                await asyncio.wait_for(task, timeout=10)

        run(scenario())


class TestJsonlParsing:
    def test_noise_ignored(self):
        assert parse_worker_line("Error loading MCP tools: ...") is None
        assert parse_worker_line('{"kind":"event","type":"log","payload":{}}') is not None
