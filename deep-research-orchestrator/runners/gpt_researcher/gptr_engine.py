"""gpt-researcher Runner — Engine実装と純ロジック。

このモジュールは gpt_researcher パッケージを import しない (親プロセスはPython 3.11でも
動作し、gpt-researcher本体はPython 3.12のworkerサブプロセス内でのみimportされる)。

設計:
- gpt-researcherの設定はプロセスグローバルな環境変数のため、runごとに worker.py を
  サブプロセスとして起動し、制御された環境変数dictを渡す。
- workerはstdoutにJSONL ({"kind":"event"|"result"|"fatal", ...}) を出力し、
  親はそれをパースして ctx.emit / RunResult に変換する。
- APIキーは環境変数経由でのみ渡す (argv・ディスク・ログに書かない)。
"""

from __future__ import annotations

import asyncio
import json
import sys
from collections import deque
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from runner_core.engine import CancelledByUser, Engine, RunContext
from runner_core.models import (
    EngineCapabilities,
    RunInput,
    RunRequest,
    RunResult,
)

ENGINE_ID = "gpt-researcher"
ENGINE_VERSION = "0.16.0"  # pinned gpt-researcher package version

# 検索プロバイダのAPIキー類は絶対にworker環境へ持ち込まない (tavily等へのfallback防止)
FORBIDDEN_ENV_KEYS = frozenset(
    {
        "TAVILY_API_KEY",
        "EXA_API_KEY",
        "BING_API_KEY",
        "SERPER_API_KEY",
        "SERPAPI_API_KEY",
        "SEARCHAPI_API_KEY",
        "GOOGLE_API_KEY",
        "GOOGLE_CX_KEY",
        "BRAVE_API_KEY",
        "RETRIEVER",  # proxy_env等から上書きされないよう、必ずこちらで設定する
        "SEARX_URL",
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "ANTHROPIC_API_KEY",
    }
)

_LANGUAGE_MAP = {
    "ja": "japanese",
    "en": "english",
    "zh": "chinese",
    "ko": "korean",
    "de": "german",
    "fr": "french",
    "es": "spanish",
}

_ALLOWED_REPORT_TYPES = {"research_report", "detailed_report"}

_STDOUT_LIMIT = 2**25  # 32MiB: result行 (レポート全文+sources) がstream limitを超えないように


class EngineConfigError(ValueError):
    """RunRequestがこのエンジンの前提を満たさない場合のfail-fastエラー。"""


def validate_request(request: RunRequest) -> None:
    """run開始時の設定検証。満たさなければ日本語メッセージでfail-fastする。"""
    if request.llm is None:
        raise EngineConfigError(
            "LLM profileが未設定のためgpt-researcherを実行できません。"
            "設定画面でこのエンジンにLLM profileを割り当ててください。"
        )
    if request.llm.api != "openai-compatible":
        raise EngineConfigError(
            "gpt-researcher RunnerはOpenAI互換API (openai-compatible) のみ対応しています。"
            f"指定されたAPI種別 '{request.llm.api}' は未対応です。"
        )
    if not request.llm.endpoint:
        raise EngineConfigError("LLM endpointが未設定のためgpt-researcherを実行できません。")
    if not request.llm.embedding_model:
        raise EngineConfigError(
            "embedding modelが未設定のためgpt-researcherを実行できません。"
            "gpt-researcherはコンテキスト圧縮にembeddingを必須とします。"
            "LLM profileにembedding_modelを設定してください。"
        )
    if request.search is None or request.search.provider != "searxng":
        raise EngineConfigError(
            "SearXNGが無効のためgpt-researcherを実行できません。"
            "このエンジンにはオフラインモードがなく、検索プロバイダはSearXNGのみ対応です。"
        )
    if not request.search.endpoint:
        raise EngineConfigError(
            "SearXNGのendpointが未設定のためgpt-researcherを実行できません。"
        )
    report_type = request.options.get("report_type", "research_report")
    if report_type not in _ALLOWED_REPORT_TYPES:
        raise EngineConfigError(
            f"report_type '{report_type}' は未対応です。"
            f"対応値: {sorted(_ALLOWED_REPORT_TYPES)}"
        )


def resolve_max_iterations(request: RunRequest) -> int:
    """検索イテレーション数。options.max_iterations (既定3) をmax_searchesで上限する。"""
    try:
        max_iterations = int(request.options.get("max_iterations", 3))
    except (TypeError, ValueError):
        max_iterations = 3
    max_iterations = max(1, max_iterations)
    if request.max_searches is not None:
        max_iterations = max(1, min(max_iterations, int(request.max_searches)))
    return max_iterations


def build_worker_env(
    request: RunRequest, *, base_env: Mapping[str, str] | None = None
) -> tuple[dict[str, str], list[str]]:
    """workerサブプロセス用の環境変数dictを最小構成で組み立てる。

    - base_env (通常os.environ) からはPATH/HOME等の最小限のみ引き継ぐ
    - proxy_env をマージ (ただしFORBIDDEN_ENV_KEYSは除去)
    - gpt-researcher設定 (RETRIEVER=searx等) は最後に設定し、上書き不能にする
    戻り値: (env, warnings)
    """
    validate_request(request)
    assert request.llm is not None and request.search is not None
    warnings: list[str] = []
    base = dict(base_env) if base_env is not None else {}

    env: dict[str, str] = {
        "PATH": base.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": base.get("HOME", "/tmp"),
        "LANG": base.get("LANG", "C.UTF-8"),
        "PYTHONUNBUFFERED": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    if base.get("VIRTUAL_ENV"):
        env["VIRTUAL_ENV"] = base["VIRTUAL_ENV"]

    # proxy設定のマージ (検索API keyの混入や重要キーの上書きは拒否)
    for key, value in (request.proxy_env or {}).items():
        if key.upper() in FORBIDDEN_ENV_KEYS:
            warnings.append(f"proxy_envのキー {key} は安全のため無視しました")
            continue
        env[key] = value

    llm = request.llm
    search = request.search

    # --- 検索: SearXNG固定。tavily等へのfallbackは絶対にしない ---
    env["RETRIEVER"] = "searx"
    env["SEARX_URL"] = search.endpoint or ""
    env["MAX_SEARCH_RESULTS_PER_QUERY"] = str(max(1, int(search.max_results)))

    # --- LLM: OpenAI互換endpoint ---
    env["OPENAI_BASE_URL"] = llm.endpoint
    # gpt-researcherのopenai providerはAPIキー必須のため、未設定時はダミーを渡す
    env["OPENAI_API_KEY"] = llm.api_key or "dummy-key"
    model_spec = f"openai:{llm.model}"
    env["FAST_LLM"] = model_spec
    env["SMART_LLM"] = model_spec
    env["STRATEGIC_LLM"] = model_spec

    # --- Embedding (searx利用時もコンテキスト圧縮に必須) ---
    env["EMBEDDING"] = f"openai:{llm.embedding_model}"
    if llm.embedding_endpoint and llm.embedding_endpoint != llm.endpoint:
        # gpt-researcherのopenai embedding providerはOPENAI_BASE_URLを共用するため
        # 別endpointは指定できない
        warnings.append(
            "embedding_endpointがLLM endpointと異なりますが、gpt-researcherでは"
            "embedding用に別endpointを指定できないため、LLM endpointを使用します"
        )

    # --- その他エンジン設定 ---
    env["SCRAPER"] = "bs"
    env["MAX_ITERATIONS"] = str(resolve_max_iterations(request))
    lang = request.input.language or "ja"
    env["LANGUAGE"] = _LANGUAGE_MAP.get(lang.lower(), lang)
    env["REPORT_FORMAT"] = "markdown"

    return env, warnings


def build_query(run_input: RunInput) -> str:
    """topic/objective/instructionsからgpt-researcherに渡すqueryを合成する。"""
    parts = [run_input.topic.strip()]
    if run_input.objective:
        parts.append(f"調査目的: {run_input.objective.strip()}")
    if run_input.instructions:
        parts.append(f"追加指示: {run_input.instructions.strip()}")
    return "\n\n".join(p for p in parts if p)


def build_worker_payload(request: RunRequest) -> tuple[dict[str, Any], list[str]]:
    """workerのstdinへ渡すJSON payload (秘密情報は含めない)。戻り値: (payload, warnings)"""
    warnings: list[str] = []
    if request.input.documents:
        warnings.append(
            "documents入力はgpt-researcher Runnerでは未対応のため無視しました"
        )
    payload: dict[str, Any] = {
        "query": build_query(request.input),
        "report_type": request.options.get("report_type", "research_report"),
        "language": request.input.language,
        "input_urls": list(request.input.input_urls or []),
    }
    return payload, warnings


def parse_worker_line(line: str) -> dict[str, Any] | None:
    """worker stdoutの1行をJSONLとしてパースする。プロトコル外の行はNone。"""
    line = line.strip()
    if not line.startswith("{"):
        return None
    try:
        obj = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(obj, dict) or "kind" not in obj:
        return None
    return obj


def redact_secrets(text: str, secrets: list[str | None]) -> str:
    """エラーメッセージ等に混入したAPIキーをマスクする。"""
    for secret in secrets:
        if secret and len(secret) >= 6:
            text = text.replace(secret, "***")
    return text


def result_from_worker_payload(payload: dict[str, Any]) -> RunResult:
    """workerの {"kind":"result", ...} 行からRunResultを構築する。"""
    data = {k: v for k, v in payload.items() if k != "kind"}
    return RunResult.model_validate(data)


async def run_jsonl_worker(
    ctx: RunContext,
    *,
    argv: list[str],
    env: dict[str, str],
    payload: dict[str, Any],
    cwd: str,
    secrets: list[str | None],
    term_grace_seconds: float = 5.0,
) -> dict[str, Any]:
    """JSONLプロトコルのworkerサブプロセスを実行し、result payloadを返す。

    - eventはctx.emitに転送
    - キャンセル要求でSIGTERM→SIGKILL
    - fatal/異常終了はRuntimeError (APIキーはマスク)
    """
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
        cwd=cwd,
        limit=_STDOUT_LIMIT,
    )
    stderr_tail: deque[str] = deque(maxlen=100)

    async def _drain_stderr() -> None:
        assert proc.stderr is not None
        while True:
            try:
                chunk = await proc.stderr.readline()
            except (ValueError, asyncio.LimitOverrunError):
                continue  # 長すぎるstderr行は捨てる
            if not chunk:
                return
            stderr_tail.append(chunk.decode("utf-8", errors="replace").rstrip())

    stderr_task = asyncio.create_task(_drain_stderr())
    result_payload: dict[str, Any] | None = None
    fatal_error: str | None = None
    try:
        assert proc.stdin is not None and proc.stdout is not None
        proc.stdin.write(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        await proc.stdin.drain()
        proc.stdin.close()

        while True:
            if ctx.cancel_requested:
                raise CancelledByUser()
            try:
                raw = await asyncio.wait_for(proc.stdout.readline(), timeout=0.5)
            except TimeoutError:
                continue
            if not raw:
                break  # EOF
            msg = parse_worker_line(raw.decode("utf-8", errors="replace"))
            if msg is None:
                continue
            kind = msg.get("kind")
            if kind == "event":
                event_payload = msg.get("payload")
                ctx.emit(
                    str(msg.get("type") or "log"),
                    event_payload if isinstance(event_payload, dict) else {},
                )
            elif kind == "result":
                result_payload = msg
                break
            elif kind == "fatal":
                fatal_error = str(msg.get("error") or "unknown worker error")
                break
        try:
            await asyncio.wait_for(proc.wait(), timeout=10)
        except TimeoutError:
            pass
    finally:
        if proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=term_grace_seconds)
            except TimeoutError:
                proc.kill()
                await proc.wait()
        stderr_task.cancel()
        try:
            await stderr_task
        except asyncio.CancelledError:
            pass

    if fatal_error is not None:
        raise RuntimeError(redact_secrets(f"engine worker失敗: {fatal_error}", secrets))
    if result_payload is None:
        tail = redact_secrets("\n".join(list(stderr_tail)[-20:]), secrets)
        raise RuntimeError(
            f"engine workerが結果を返さずに終了しました (exit={proc.returncode})。"
            f" stderr末尾:\n{tail}"
        )
    return result_payload


class GptResearcherEngine(Engine):
    engine_id = ENGINE_ID

    def __init__(self, worker_path: str | Path | None = None):
        self._worker_path = Path(worker_path) if worker_path else Path(__file__).with_name("worker.py")

    def capabilities(self) -> EngineCapabilities:
        return EngineCapabilities(
            engine_id=self.engine_id,
            name="GPT Researcher",
            version=ENGINE_VERSION,
            output_kind="report",
            streaming=True,
            cancel=True,
            citations=False,  # レポート本文にinline引用はあるが、構造化claimは返さない
            token_usage=False,  # gpt-researcherはtoken数を公開しない
            cost=True,  # OpenAI価格表ベースの推定値のみ
            local_files=False,
            options_schema={
                "type": "object",
                "properties": {
                    "report_type": {
                        "type": "string",
                        "enum": sorted(_ALLOWED_REPORT_TYPES),
                        "default": "research_report",
                    },
                    "max_iterations": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 10,
                        "default": 3,
                        "description": "検索サブクエリのイテレーション数 (max_searchesで上限)",
                    },
                },
            },
            health="available",
            health_reason=None,
            required_config=["llm", "search:searxng"],
        )

    async def run(self, ctx: RunContext) -> RunResult:
        request = ctx.request
        validate_request(request)  # EngineConfigError (ValueError) でfail-fast
        env, env_warnings = build_worker_env(request, base_env=_os_environ())
        payload, payload_warnings = build_worker_payload(request)

        ctx.emit("stage", {"stage": "starting_worker", "engine": self.engine_id})
        secrets = [request.llm.api_key if request.llm else None]
        result_payload = await run_jsonl_worker(
            ctx,
            argv=[sys.executable, "-u", str(self._worker_path)],
            env=env,
            payload=payload,
            cwd=str(self._worker_path.parent),
            secrets=secrets,
        )
        result = result_from_worker_payload(result_payload)
        result.warnings = [*env_warnings, *payload_warnings, *result.warnings]
        return result


def _os_environ() -> Mapping[str, str]:
    import os

    return os.environ
