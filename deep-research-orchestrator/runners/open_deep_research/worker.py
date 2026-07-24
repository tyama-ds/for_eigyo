"""open_deep_research workerサブプロセス (Python 3.10+、本番はPython 3.12)。

親 (odr_engine.OpenDeepResearchEngine) から:
- 環境変数: OPENAI_API_KEY / OPENAI_BASE_URL / OPENAI_API_BASE
  (anthropic時は ANTHROPIC_API_KEY / ANTHROPIC_BASE_URL / ANTHROPIC_API_URL)
- stdin: JSON payload {"prompt", "language", "configurable", "recursion_limit"}
  configurable には allow_clarification=False / search_api="none" /
  4モデル / mcp_config (同梱SearXNG MCPサーバーのURL) が入っている。

stdoutへJSONLを出力する:
  {"kind":"event","type":"stage|log|token_usage",...}
  {"kind":"result", ...RunResultフィールド...}  または  {"kind":"fatal","error":...}
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from typing import Any, TextIO

# 同ディレクトリのodr_engineから純ロジックを共用する
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from odr_engine import extract_usage, find_state_value, parse_sources_section  # noqa: E402

_EMIT: TextIO | None = None


def _setup_stdio() -> TextIO:
    """JSONL用に元stdoutを確保し、fd1をstderrへリダイレクトする。

    (open_deep_researchは失敗時に print() する箇所があるためプロトコル保護必須)
    """
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


_STAGE_LABELS = {
    "clarify_with_user": "clarify",
    "write_research_brief": "brief",
    "research_supervisor": "supervisor",
    "supervisor": "supervisor",
    "supervisor_tools": "supervisor_tools",
    "researcher": "researcher",
    "researcher_tools": "researcher_tools",
    "compress_research": "compress",
    "final_report_generation": "final_report",
}


def _summary_from_report(report: str, limit: int = 400) -> str | None:
    for block in report.split("\n\n"):
        block = block.strip()
        if not block or block.startswith("#"):
            continue
        return " ".join(block.split())[:limit]
    return None


async def _research(payload: dict[str, Any]) -> dict[str, Any]:
    started = time.monotonic()
    from langchain_core.messages import HumanMessage  # noqa: PLC0415
    from open_deep_research.deep_researcher import deep_researcher  # noqa: PLC0415

    config: dict[str, Any] = {
        "configurable": payload["configurable"],
        "recursion_limit": int(payload.get("recursion_limit", 50)),
    }
    inputs = {"messages": [HumanMessage(content=payload["prompt"])]}

    final_report: str | None = None
    research_brief: str | None = None
    usage_in = 0
    usage_out = 0

    async for chunk in deep_researcher.astream(
        inputs, config=config, stream_mode="updates", subgraphs=True
    ):
        namespace: tuple[Any, ...] = ()
        update = chunk
        if isinstance(chunk, tuple) and len(chunk) == 2:
            namespace, update = chunk
        if not isinstance(update, dict):
            continue
        for node, delta in update.items():
            stage = _STAGE_LABELS.get(str(node), str(node))
            emit_event(
                "stage",
                {"stage": stage, "node": str(node), "namespace": [str(n) for n in namespace]},
            )
            i, o = extract_usage(delta)
            usage_in += i
            usage_out += o
            fr = find_state_value(delta, "final_report")
            if isinstance(fr, str) and fr.strip():
                final_report = fr
            rb = find_state_value(delta, "research_brief")
            if isinstance(rb, str) and rb.strip():
                research_brief = rb

    if usage_in or usage_out:
        emit_event(
            "token_usage",
            {"prompt_tokens": usage_in, "completion_tokens": usage_out},
        )

    if not final_report:
        raise RuntimeError("final_reportが生成されませんでした (graphが完了しなかった可能性)")
    if final_report.startswith("Error generating final report"):
        raise RuntimeError(f"open-deep-research内部エラー: {final_report[:500]}")

    sources = parse_sources_section(final_report)

    warnings = [
        "このエンジンはclaim単位の構造化引用を返しません (レポート末尾のSourcesリストのみ)",
        "llm_cost_usdは価格表が不明なため算出しません (null)",
    ]
    if not sources:
        warnings.append(
            "レポートに '### Sources' セクションが見つからなかったため、sourcesは空です"
        )
    got_usage = bool(usage_in or usage_out)
    if not got_usage:
        warnings.append("token使用量 (usage_metadata) を取得できなかったためnullです")

    return {
        "kind": "result",
        "output_kind": "report",
        "summary": _summary_from_report(final_report),
        "report_markdown": final_report,
        "claims": [],
        "sources": sources,
        "metrics": {
            "searches": None,
            "sources": len(sources),
            "prompt_tokens": usage_in if got_usage else None,
            "completion_tokens": usage_out if got_usage else None,
            "total_tokens": (usage_in + usage_out) if got_usage else None,
            "llm_cost_usd": None,
            "llm_cost_is_estimate": None,
            "search_api_cost_usd": 0.0,
            "infra_cost": "not_measured",
            "duration_seconds": round(time.monotonic() - started, 3),
        },
        "warnings": warnings,
        "raw": {
            "engine": "open-deep-research",
            "engine_version": "0.0.16",
            "research_brief": research_brief,
        },
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
