"""gpt-researcher workerサブプロセス (Python 3.12専用)。

親 (gptr_engine.GptResearcherEngine) から:
- 環境変数: RETRIEVER=searx / SEARX_URL / OPENAI_BASE_URL / OPENAI_API_KEY /
  FAST_LLM / SMART_LLM / STRATEGIC_LLM / EMBEDDING / SCRAPER / MAX_ITERATIONS /
  MAX_SEARCH_RESULTS_PER_QUERY / LANGUAGE (すべて親が制御して渡す)
- stdin: JSON payload {"query", "report_type", "language", "input_urls"}

stdoutへJSONLを出力する:
  {"kind":"event","type":"log|stage|source_found",...}
  {"kind":"result", ...RunResultフィールド...}  または  {"kind":"fatal","error":...}

注意: gpt-researcher本体やその依存がstdoutへprintしてもプロトコルが壊れないよう、
起動直後にfd1をstderrへ付け替え、JSONL専用に元のstdoutをdupして保持する。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from typing import Any, TextIO

_EMIT: TextIO | None = None


def _setup_stdio() -> TextIO:
    """JSONL用に元stdoutを確保し、fd1をstderrへリダイレクトする。"""
    emit_fd = os.dup(1)
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    return os.fdopen(emit_fd, "w", encoding="utf-8", buffering=1)


def _emit(obj: dict[str, Any]) -> None:
    assert _EMIT is not None
    _EMIT.write(json.dumps(obj, ensure_ascii=False, default=str) + "\n")
    _EMIT.flush()


def emit_event(type_: str, payload: dict[str, Any]) -> None:
    _emit({"kind": "event", "type": type_, "payload": payload})


def _install_typing_shim() -> None:
    """gpt-researcher 0.16.0 の上流バグ対策。

    gpt_researcher/actions/query_processing.py 等が `Any` / `List` を
    `from typing import ...` せずに注釈へ使用しており、import時に
    NameErrorとなる。builtinsへtyping名を注入して回避する (worker
    プロセス内に閉じたパッチ)。
    """
    import builtins
    import typing

    for name in (
        "Any", "List", "Dict", "Optional", "Tuple", "Set",
        "Union", "Callable", "Iterable", "Sequence",
    ):
        if not hasattr(builtins, name):
            setattr(builtins, name, getattr(typing, name))


class WsBridge:
    """gpt-researcherのwebsocket互換オブジェクト。send_json → JSONLイベント。"""

    async def send_json(self, data: dict[str, Any]) -> None:
        if not isinstance(data, dict):
            return
        type_ = data.get("type")
        content = data.get("content")
        output = data.get("output")
        metadata = data.get("metadata")
        if type_ == "images":
            return
        if type_ == "logs" and content == "added_source_url" and isinstance(metadata, str):
            emit_event("source_found", {"url": metadata})
            return
        if type_ == "report":
            # レポート本文のストリーミングchunk。全文はresultで返すため進捗のみ通知。
            emit_event("log", {"message": "(report chunk)", "content": "report_chunk"})
            return
        payload: dict[str, Any] = {"message": str(output) if output is not None else ""}
        if content is not None:
            payload["content"] = str(content)
        if metadata is not None:
            payload["metadata"] = metadata
        emit_event("log", payload)


class LogBridge:
    """gpt-researcherのlog_handler互換オブジェクト。"""

    async def on_tool_start(self, tool_name: str, **kwargs: Any) -> None:
        emit_event("log", {"message": f"tool start: {tool_name}", "tool": str(tool_name)})

    async def on_agent_action(self, action: str, **kwargs: Any) -> None:
        emit_event("stage", {"stage": f"action:{action}"})

    async def on_research_step(self, step: str, details: Any, **kwargs: Any) -> None:
        payload: dict[str, Any] = {"stage": str(step)}
        if isinstance(details, dict):
            payload["details"] = {k: str(v)[:300] for k, v in details.items()}
        emit_event("stage", payload)


def _excerpt(text: Any, limit: int = 200) -> str | None:
    if not isinstance(text, str) or not text.strip():
        return None
    text = " ".join(text.split())
    return text[:limit]


def _summary_from_report(report: str, limit: int = 400) -> str | None:
    for block in report.split("\n\n"):
        block = block.strip()
        if not block or block.startswith("#"):
            continue
        return " ".join(block.split())[:limit]
    return None


async def _research(payload: dict[str, Any]) -> dict[str, Any]:
    started = time.monotonic()
    _install_typing_shim()
    from gpt_researcher import GPTResearcher  # noqa: PLC0415 (env設定後にimport)

    input_urls = payload.get("input_urls") or []
    researcher = GPTResearcher(
        query=payload["query"],
        report_type=payload.get("report_type", "research_report"),
        report_source="web",
        source_urls=input_urls or None,
        complement_source_urls=bool(input_urls),
        websocket=WsBridge(),
        log_handler=LogBridge(),
        verbose=True,
    )

    emit_event("stage", {"stage": "conduct_research"})
    await researcher.conduct_research()
    emit_event("stage", {"stage": "write_report"})
    report = await researcher.write_report()

    source_urls: list[str] = list(researcher.get_source_urls() or [])
    research_sources = researcher.get_research_sources() or []

    sources: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in research_sources:
        if not isinstance(item, dict):
            continue
        url = item.get("url") or item.get("href")
        if not isinstance(url, str) or not url or url in seen:
            continue
        seen.add(url)
        sources.append(
            {
                "url": url,
                "title": item.get("title") if isinstance(item.get("title"), str) else None,
                "excerpt": _excerpt(item.get("content") or item.get("raw_content")),
                "meta": {},
            }
        )
    for url in source_urls:
        if isinstance(url, str) and url and url not in seen:
            seen.add(url)
            sources.append({"url": url, "title": None, "excerpt": None, "meta": {}})

    for s in sources:
        emit_event("source_found", {"url": s["url"], "title": s.get("title")})

    # 取得できないコストは0へ丸めず null にする (捏造禁止)
    llm_cost: float | None
    try:
        raw_cost = researcher.get_costs()
        llm_cost = float(raw_cost) if raw_cost else None
    except (TypeError, ValueError, AttributeError):
        llm_cost = None
    emit_event("cost", {"llm_cost_usd": llm_cost, "estimate": True, "search_api_cost_usd": 0.0})

    warnings = [
        "このエンジンはclaim単位の構造化引用を返しません (レポート本文中のinline引用のみ)",
        "llm_cost_usdはgpt-researcher内蔵のOpenAI価格表による推定値です。"
        "ローカル/独自LLMでは実費と一致しません (取得不能の場合はnull)",
        "token使用量はgpt-researcherから取得できないためnullです",
    ]

    visited_urls = getattr(researcher, "visited_urls", None)
    raw: dict[str, Any] = {
        "engine": "gpt-researcher",
        "engine_version": "0.16.0",
        "source_urls": source_urls,
        "visited_urls": sorted(visited_urls) if isinstance(visited_urls, set) else visited_urls,
    }
    try:
        raw["step_costs"] = researcher.get_step_costs()
    except Exception:  # noqa: BLE001 - 任意の補助情報
        pass

    return {
        "kind": "result",
        "output_kind": "report",
        "summary": _summary_from_report(report),
        "report_markdown": report,
        "claims": [],
        "sources": sources,
        "metrics": {
            "searches": None,
            "sources": len(sources),
            "prompt_tokens": None,
            "completion_tokens": None,
            "total_tokens": None,
            "llm_cost_usd": llm_cost,
            "llm_cost_is_estimate": True,
            "search_api_cost_usd": 0.0,
            "infra_cost": "not_measured",
            "duration_seconds": round(time.monotonic() - started, 3),
        },
        "warnings": warnings,
        "raw": raw,
    }


def main() -> None:
    global _EMIT
    _EMIT = _setup_stdio()
    try:
        payload = json.load(sys.stdin)
        result = asyncio.run(_research(payload))
        _emit(result)
    except Exception as e:  # noqa: BLE001 - fatal行へ変換して親に伝える
        _emit({"kind": "fatal", "error": f"{type(e).__name__}: {e}"})
        sys.exit(1)


if __name__ == "__main__":
    main()
