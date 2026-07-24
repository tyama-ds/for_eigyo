"""open_deep_research Runner — Engine実装と純ロジック。

このモジュールは open_deep_research / langchain / mcp パッケージをimportしない
(親サービスプロセスはPython 3.11でも動作し、エンジン本体はworkerサブプロセス内でのみ動く)。

設計:
- open_deep_researchはsearxngをネイティブ対応しないため、search_api="none" とし、
  同梱のFastMCPサーバー (searxng_mcp.py) を run ごとに子プロセスで起動して
  mcp_config (streamable HTTP) 経由で `searxng_web_search` ツールを提供する。
- LLM設定は環境変数 (OPENAI_API_BASE等) + configurable dictで渡すため、
  runごとに worker.py をサブプロセス起動して汚染を避ける。
- workerはstdoutにJSONL ({"kind":"event"|"result"|"fatal"}) を出力する。
- APIキーは環境変数経由でのみ渡す (argv・ディスク・ログに書かない)。
"""

from __future__ import annotations

import asyncio
import json
import re
import socket
import sys
from collections import deque
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from runner_core.engine import CancelledByUser, Engine, RunContext
from runner_core.models import EngineCapabilities, RunInput, RunRequest, RunResult

ENGINE_ID = "open-deep-research"
ENGINE_VERSION = "0.0.16"  # pinned open-deep-research package version

MCP_SEARCH_TOOL = "searxng_web_search"

# 有料/hosted検索API (tavily / openai / anthropicネイティブ検索) はMVPでは禁止
FORBIDDEN_SEARCH_APIS = frozenset({"tavily", "openai", "anthropic"})

# worker環境へ持ち込ませないキー:
# - 検索プロバイダのAPIキー (tavily等へのfallback防止)
# - open_deep_research.Configurationのフィールド名大文字 (env優先のため上書き防止)
FORBIDDEN_ENV_KEYS = frozenset(
    {
        "TAVILY_API_KEY",
        "EXA_API_KEY",
        "BING_API_KEY",
        "SERPER_API_KEY",
        "SERPAPI_API_KEY",
        "SEARCHAPI_API_KEY",
        "GOOGLE_API_KEY",
        "BRAVE_API_KEY",
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "OPENAI_API_BASE",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_BASE_URL",
        "GET_API_KEYS_FROM_CONFIG",
        # Configuration.from_runnable_config はenvが優先されるため、必ず遮断する
        "SEARCH_API",
        "ALLOW_CLARIFICATION",
        "MCP_CONFIG",
        "MCP_PROMPT",
        "RESEARCH_MODEL",
        "SUMMARIZATION_MODEL",
        "COMPRESSION_MODEL",
        "FINAL_REPORT_MODEL",
        "MAX_CONCURRENT_RESEARCH_UNITS",
        "MAX_RESEARCHER_ITERATIONS",
        "MAX_REACT_TOOL_CALLS",
        "MAX_STRUCTURED_OUTPUT_RETRIES",
    }
)


class EngineConfigError(ValueError):
    """RunRequestがこのエンジンの前提を満たさない場合のfail-fastエラー。"""


def search_disabled_by_option(request: RunRequest) -> bool:
    """options.search_api == "none" が明示された場合のみ検索なしモードを許可する。"""
    return request.options.get("search_api") == "none"


def validate_request(request: RunRequest) -> None:
    """run開始時の設定検証。満たさなければ日本語メッセージでfail-fastする。"""
    if request.llm is None:
        raise EngineConfigError(
            "LLM profileが未設定のためopen-deep-researchを実行できません。"
            "設定画面でこのエンジンにLLM profileを割り当ててください。"
        )
    if not request.llm.endpoint:
        raise EngineConfigError(
            "LLM endpointが未設定のためopen-deep-researchを実行できません。"
        )
    search_api_opt = request.options.get("search_api")
    if search_api_opt is not None and search_api_opt != "none":
        if search_api_opt in FORBIDDEN_SEARCH_APIS:
            raise EngineConfigError(
                f"有料/hosted検索APIはこのMVPでは使用できません (search_api={search_api_opt})。"
                "SearXNG (既定) か search_api=\"none\" を使用してください。"
            )
        raise EngineConfigError(
            f"search_api '{search_api_opt}' は未対応です。"
            "SearXNG (既定、optionsで指定不要) か \"none\" のみ使用できます。"
        )
    if search_disabled_by_option(request):
        return  # 明示的な検索なしモード (MCPツールなし)
    if request.search is None or request.search.provider != "searxng":
        raise EngineConfigError(
            "SearXNGが無効のためopen-deep-researchを実行できません。"
            "検索なしで実行する場合はoptionsで search_api=\"none\" を明示してください。"
        )
    if not request.search.endpoint:
        raise EngineConfigError(
            "SearXNGのendpointが未設定のためopen-deep-researchを実行できません。"
        )


def model_prefix(request: RunRequest) -> str:
    assert request.llm is not None
    return "anthropic:" if request.llm.api == "anthropic" else "openai:"


def _append_no_proxy(env: dict[str, str], hosts: str = "127.0.0.1,localhost") -> None:
    """MCPサーバー(localhost)への接続がproxyへ迂回しないようにNO_PROXYを補強する。"""
    for key in ("NO_PROXY", "no_proxy"):
        current = env.get(key, "")
        parts = [p for p in current.split(",") if p]
        for host in hosts.split(","):
            if host not in parts:
                parts.append(host)
        env[key] = ",".join(parts)


def build_base_env(
    request: RunRequest, *, base_env: Mapping[str, str] | None = None
) -> tuple[dict[str, str], list[str]]:
    """worker/MCP共通の最小環境 (PATH/HOME + proxy_env、秘密なし)。戻り値: (env, warnings)"""
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
    for key, value in (request.proxy_env or {}).items():
        if key.upper() in FORBIDDEN_ENV_KEYS:
            warnings.append(f"proxy_envのキー {key} は安全のため無視しました")
            continue
        env[key] = value
    return env, warnings


def build_worker_env(
    request: RunRequest, *, base_env: Mapping[str, str] | None = None
) -> tuple[dict[str, str], list[str]]:
    """workerサブプロセス用の環境変数dict。LLM APIキーはここでのみ渡す。"""
    validate_request(request)
    assert request.llm is not None
    env, warnings = build_base_env(request, base_env=base_env)
    llm = request.llm

    if llm.api == "anthropic":
        env["ANTHROPIC_API_KEY"] = llm.api_key or "dummy-key"
        env["ANTHROPIC_BASE_URL"] = llm.endpoint
        env["ANTHROPIC_API_URL"] = llm.endpoint
    else:
        env["OPENAI_API_KEY"] = llm.api_key or "dummy-key"
        env["OPENAI_BASE_URL"] = llm.endpoint
        env["OPENAI_API_BASE"] = llm.endpoint

    # ローカルMCPサーバーへの接続をproxy対象外にする
    if any(k.lower().endswith("_proxy") for k in env):
        _append_no_proxy(env)
    return env, warnings


def build_mcp_env(
    request: RunRequest,
    *,
    port: int,
    host: str = "127.0.0.1",
    base_env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """searxng_mcp.py 子プロセス用の環境変数。

    MCPサーバーは内部のSearXNGとのみ通信するため、proxy_envは渡さない。
    LLM APIキーも渡さない。
    """
    assert request.search is not None and request.search.endpoint
    base = dict(base_env) if base_env is not None else {}
    env: dict[str, str] = {
        "PATH": base.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": base.get("HOME", "/tmp"),
        "LANG": base.get("LANG", "C.UTF-8"),
        "PYTHONUNBUFFERED": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "SEARXNG_ENDPOINT": request.search.endpoint,
        "SEARXNG_TIMEOUT": str(request.search.timeout_seconds),
        "SEARXNG_MAX_RESULTS": str(max(1, int(request.search.max_results))),
        "MCP_HOST": host,
        "MCP_PORT": str(port),
    }
    if base.get("VIRTUAL_ENV"):
        env["VIRTUAL_ENV"] = base["VIRTUAL_ENV"]
    return env


def resolve_max_react_tool_calls(request: RunRequest) -> int:
    """researcherあたりのツール呼び出し上限。max_searchesを近似上限として適用する。"""
    try:
        value = int(request.options.get("max_react_tool_calls", 5))
    except (TypeError, ValueError):
        value = 5
    value = max(1, value)
    if request.max_searches is not None:
        value = max(1, min(value, int(request.max_searches)))
    return value


def build_configurable(request: RunRequest, mcp_url: str | None) -> dict[str, Any]:
    """open_deep_researchの configurable dictを構築する。"""
    assert request.llm is not None
    prefix = model_prefix(request)
    model = f"{prefix}{request.llm.model}"
    configurable: dict[str, Any] = {
        "allow_clarification": False,  # headless必須
        "search_api": "none",  # ネイティブ検索APIは使わない (検索はMCP経由のSearXNGのみ)
        "research_model": model,
        "summarization_model": model,
        "compression_model": model,
        "final_report_model": model,
        "max_structured_output_retries": 3,
        "max_concurrent_research_units": int(
            request.options.get("max_concurrent_research_units", 2)
        ),
        "max_researcher_iterations": int(request.options.get("max_researcher_iterations", 3)),
        "max_react_tool_calls": resolve_max_react_tool_calls(request),
    }
    if mcp_url:
        configurable["mcp_config"] = {
            "url": mcp_url,  # open_deep_research側が url.rstrip("/") + "/mcp" に接続する
            "tools": [MCP_SEARCH_TOOL],
            "auth_required": False,
        }
        configurable["mcp_prompt"] = (
            f"You MUST use the `{MCP_SEARCH_TOOL}` tool to search the web for "
            "information. It queries an internal SearXNG metasearch instance and "
            "returns titles, URLs and snippets. Always ground your findings in "
            "these search results and cite the URLs."
        )
    return configurable


def build_prompt(run_input: RunInput) -> str:
    """topic/objective/instructions/languageから調査プロンプトを合成する。"""
    parts = [f"Research topic: {run_input.topic.strip()}"]
    if run_input.objective:
        parts.append(f"Objective: {run_input.objective.strip()}")
    if run_input.instructions:
        parts.append(f"Additional instructions: {run_input.instructions.strip()}")
    if run_input.input_urls:
        urls = "\n".join(f"- {u}" for u in run_input.input_urls)
        parts.append(f"Consider these starting URLs:\n{urls}")
    lang = (run_input.language or "ja").lower()
    lang_name = {"ja": "Japanese", "en": "English"}.get(lang, run_input.language)
    parts.append(f"Write the final report in {lang_name}.")
    return "\n\n".join(parts)


def build_worker_payload(request: RunRequest, mcp_url: str | None) -> tuple[dict[str, Any], list[str]]:
    """workerのstdinへ渡すJSON payload (秘密情報は含めない)。戻り値: (payload, warnings)"""
    warnings: list[str] = []
    if request.input.documents:
        warnings.append(
            "documents入力はopen-deep-research Runnerでは未対応のため無視しました"
        )
    if search_disabled_by_option(request):
        warnings.append(
            "search_api=\"none\" が指定されたため、web検索なし (LLM知識のみ) で実行しました"
        )
    payload = {
        "prompt": build_prompt(request.input),
        "language": request.input.language,
        "configurable": build_configurable(request, mcp_url),
        "recursion_limit": int(request.options.get("recursion_limit", 50)),
    }
    return payload, warnings


# --- "### Sources" セクションのパース (捏造禁止: 見つからなければ空 + warning) ---

_SOURCES_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s*sources?\s*:?\s*$", re.IGNORECASE)
_URL_RE = re.compile(r"https?://[^\s<>\)\]]+")
_LINE_PREFIX_RE = re.compile(r"^\s*(?:[-*+]\s*)?(?:\[?\d+\]?[.):]?\s*)?")


def parse_sources_section(report_markdown: str | None) -> list[dict[str, Any]]:
    """final_report末尾の "### Sources" リスト `[n] Title: URL` をSourceRecord dict化する。

    セクションが無い場合は [] を返す (呼び出し側でwarningを付ける)。
    """
    if not report_markdown:
        return []
    lines = report_markdown.splitlines()
    start = None
    for i, line in enumerate(lines):
        if _SOURCES_HEADING_RE.match(line):
            start = i + 1  # 最後の Sources 見出しを採用
    if start is None:
        return []
    sources: list[dict[str, Any]] = []
    seen: set[str] = set()
    for line in lines[start:]:
        if re.match(r"^\s{0,3}#{1,6}\s", line):
            break  # 次の見出しでセクション終了
        url_match = _URL_RE.search(line)
        if not url_match:
            continue
        url = url_match.group(0).rstrip(".,;:")
        if url in seen:
            continue
        seen.add(url)
        title = _LINE_PREFIX_RE.sub("", line[: url_match.start()]).strip()
        title = title.rstrip(":-–—").strip().strip("*_").strip()
        sources.append({"url": url, "title": title or None, "excerpt": None, "meta": {}})
    return sources


# --- astream updates からの抽出ヘルパ (worker内で使用、純ロジックなのでここに置く) ---

def extract_usage(obj: Any, _depth: int = 0) -> tuple[int, int]:
    """update chunk内のAIMessage.usage_metadataを再帰的に合算する。

    戻り値: (input_tokens, output_tokens)。見つからなければ (0, 0)。
    """
    if _depth > 8 or obj is None or isinstance(obj, (str, bytes, int, float, bool)):
        return (0, 0)
    total_in = 0
    total_out = 0

    usage = None
    if isinstance(obj, dict):
        usage = obj.get("usage_metadata")
    else:
        usage = getattr(obj, "usage_metadata", None)
    if isinstance(usage, dict):
        try:
            total_in += int(usage.get("input_tokens") or 0)
            total_out += int(usage.get("output_tokens") or 0)
        except (TypeError, ValueError):
            pass

    children: list[Any] = []
    if isinstance(obj, dict):
        children = [v for k, v in obj.items() if k != "usage_metadata"]
    elif isinstance(obj, (list, tuple, set)):
        children = list(obj)
    else:
        content = getattr(obj, "content", None)
        if isinstance(content, (list, dict)):
            children = [content]
    for child in children:
        i, o = extract_usage(child, _depth + 1)
        total_in += i
        total_out += o
    return (total_in, total_out)


def find_state_value(obj: Any, key: str, _depth: int = 0) -> Any | None:
    """update chunkからstateキー (final_report / research_brief等) を再帰探索する。"""
    if _depth > 6 or obj is None or isinstance(obj, (str, bytes, int, float, bool)):
        return None
    if isinstance(obj, dict):
        if key in obj and obj[key] is not None:
            return obj[key]
        for value in obj.values():
            found = find_state_value(value, key, _depth + 1)
            if found is not None:
                return found
    elif isinstance(obj, (list, tuple)):
        for item in obj:
            found = find_state_value(item, key, _depth + 1)
            if found is not None:
                return found
    return None


# --- SearXNG検索結果の整形 (searxng_mcp.pyから使用する純関数) ---

def format_searx_results(query: str, payload: Any, max_results: int) -> str:
    """SearXNGの /search?format=json 応答をLLM向けテキストに整形する。"""
    lines = [f'Search results for "{query}":']
    results = []
    if isinstance(payload, dict):
        raw = payload.get("results")
        if isinstance(raw, list):
            results = raw
    count = 0
    for item in results:
        if not isinstance(item, dict):
            continue
        url = item.get("url") or item.get("href")
        if not isinstance(url, str) or not url:
            continue
        count += 1
        if count > max(1, max_results):
            count -= 1
            break
        title = item.get("title") if isinstance(item.get("title"), str) else "(no title)"
        snippet = item.get("content") or item.get("snippet") or ""
        if isinstance(snippet, str):
            snippet = " ".join(snippet.split())[:500]
        lines.append(f"{count}. {title}\n   URL: {url}\n   {snippet}".rstrip())
    if count == 0:
        lines.append("(no results)")
    return "\n\n".join(lines)


# --- JSONL workerサブプロセス実行 (gpt_researcher runnerと同型・イメージ独立のため重複実装) ---

def parse_worker_line(line: str) -> dict[str, Any] | None:
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
    for secret in secrets:
        if secret and len(secret) >= 6:
            text = text.replace(secret, "***")
    return text


_STDOUT_LIMIT = 2**25  # 32MiB


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
                continue
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
                break
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


def pick_free_port(host: str = "127.0.0.1") -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return sock.getsockname()[1]


async def wait_for_port(host: str, port: int, timeout_seconds: float = 20.0) -> None:
    deadline = asyncio.get_event_loop().time() + timeout_seconds
    last_error: Exception | None = None
    while asyncio.get_event_loop().time() < deadline:
        try:
            _, writer = await asyncio.open_connection(host, port)
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass
            return
        except OSError as e:
            last_error = e
            await asyncio.sleep(0.25)
    raise RuntimeError(f"SearXNG MCPサーバーが起動しませんでした ({host}:{port}): {last_error}")


class OpenDeepResearchEngine(Engine):
    engine_id = ENGINE_ID

    def __init__(
        self,
        worker_path: str | Path | None = None,
        mcp_server_path: str | Path | None = None,
    ):
        base = Path(__file__).parent
        self._worker_path = Path(worker_path) if worker_path else base / "worker.py"
        self._mcp_server_path = (
            Path(mcp_server_path) if mcp_server_path else base / "searxng_mcp.py"
        )

    def capabilities(self) -> EngineCapabilities:
        return EngineCapabilities(
            engine_id=self.engine_id,
            name="Open Deep Research (LangChain)",
            version=ENGINE_VERSION,
            output_kind="report",
            streaming=True,
            cancel=True,
            citations=False,  # レポート末尾のSourcesリストのみ。claim単位の引用なし
            token_usage=True,  # usage_metadataが取得できた場合のみ (取れなければnull+warning)
            cost=False,  # 価格表が不明なため推定もしない
            local_files=False,
            options_schema={
                "type": "object",
                "properties": {
                    "search_api": {
                        "type": "string",
                        "enum": ["none"],
                        "description": "検索なしモードの明示指定のみ許可 (既定はSearXNG/MCP)",
                    },
                    "max_researcher_iterations": {
                        "type": "integer", "minimum": 1, "maximum": 10, "default": 3,
                    },
                    "max_react_tool_calls": {
                        "type": "integer", "minimum": 1, "maximum": 20, "default": 5,
                        "description": "researcherあたりのツール呼び出し上限 (max_searchesで上限)",
                    },
                    "max_concurrent_research_units": {
                        "type": "integer", "minimum": 1, "maximum": 5, "default": 2,
                    },
                    "recursion_limit": {
                        "type": "integer", "minimum": 10, "maximum": 200, "default": 50,
                    },
                },
            },
            health="available",
            health_reason=None,
            required_config=["llm", "search:searxng"],
        )

    async def run(self, ctx: RunContext) -> RunResult:
        request = ctx.request
        validate_request(request)
        base_env = _os_environ()
        env, env_warnings = build_worker_env(request, base_env=base_env)

        mcp_proc: asyncio.subprocess.Process | None = None
        mcp_url: str | None = None
        try:
            if not search_disabled_by_option(request):
                host = "127.0.0.1"
                port = pick_free_port(host)
                mcp_env = build_mcp_env(request, port=port, host=host, base_env=base_env)
                ctx.emit("stage", {"stage": "starting_mcp_server", "port": port})
                mcp_proc = await asyncio.create_subprocess_exec(
                    sys.executable,
                    "-u",
                    str(self._mcp_server_path),
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                    env=mcp_env,
                    cwd=str(self._mcp_server_path.parent),
                )
                await wait_for_port(host, port)
                mcp_url = f"http://{host}:{port}"

            payload, payload_warnings = build_worker_payload(request, mcp_url)
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
        finally:
            if mcp_proc is not None and mcp_proc.returncode is None:
                mcp_proc.terminate()
                try:
                    await asyncio.wait_for(mcp_proc.wait(), timeout=5)
                except TimeoutError:
                    mcp_proc.kill()
                    await mcp_proc.wait()

        result = RunResult.model_validate(
            {k: v for k, v in result_payload.items() if k != "kind"}
        )
        result.warnings = [*env_warnings, *payload_warnings, *result.warnings]
        return result


def _os_environ() -> Mapping[str, str]:
    import os

    return os.environ
