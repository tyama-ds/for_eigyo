"""決定論的Mock Runner群。

固定seed (options.seed, 既定42) とtopicから決定論的に同じ結果を生成する。
E2E再現性のため乱数はhashlibベースで導出し、時刻に依存しない。

- mock-fast        : 短時間で成功。引用付きclaimsを返す
- mock-slow        : 時間をかけて成功。mock-fastと一部一致・一部矛盾するclaimsを返す
- mock-fail        : 調査途中で決定論的に失敗する
- mock-partial     : 成功するがmetrics欠損 (null) とwarningを含む
- mock-timeout     : 完了しない (orchestrator/Runnerのtimeoutで打ち切られる)
- mock-cancellable : キャンセルされるまで進捗を出し続け、協調キャンセルに応じる

speed_factor option (既定1.0) で全sleepを比例短縮できる (テスト用)。
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

from runner_core.engine import Engine, RunContext
from runner_core.models import (
    ClaimRecord,
    EngineCapabilities,
    EvidenceRecord,
    RunMetrics,
    RunResult,
    SourceRecord,
)


def _det_int(seed: int, *parts: str, mod: int = 1000) -> int:
    h = hashlib.sha256(("|".join(parts) + f"|{seed}").encode()).hexdigest()
    return int(h[:8], 16) % mod


def _slug(topic: str) -> str:
    h = hashlib.sha256(topic.encode()).hexdigest()[:10]
    return h


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _mk_sources(topic: str, engine: str, seed: int) -> list[SourceRecord]:
    slug = _slug(topic)
    # source 1,2 は全engine共通 (dedup+provenance検証用)。3はengine固有。
    common = [
        SourceRecord(
            url=f"https://example.org/reports/{slug}/overview?utm_source=mock",
            title=f"{topic} 概況レポート",
            fetched_at=_now(),
            excerpt=f"{topic}の市場規模は2025年時点で約120億ドルと推計される。",
            meta={"rank": 1},
        ),
        SourceRecord(
            url=f"https://example.org/stats/{slug}",
            title=f"{topic} 統計データ集",
            fetched_at=_now(),
            excerpt=f"{topic}に関する主要統計。成長率の推計は調査機関により差がある。",
            meta={"rank": 2},
        ),
        SourceRecord(
            url=f"https://research.example.net/{engine}/{slug}",
            title=f"{topic} 詳細分析 ({engine})",
            fetched_at=_now(),
            excerpt=f"{engine}が独自に収集した{topic}の分析結果。",
            meta={"rank": 3},
        ),
    ]
    return common


def _base_claims(topic: str, engine: str, seed: int, sources: list[SourceRecord]) -> list[ClaimRecord]:
    growth = {"mock-fast": "12%", "mock-slow": "25%", "mock-partial": "12%"}.get(engine, "12%")
    claims = [
        ClaimRecord(
            text=f"{topic}の市場規模は2025年時点で約120億ドルである。",
            key="market-size-2025",
            value="120億ドル",
            evidence=[
                EvidenceRecord(
                    source_url=sources[0].url,
                    excerpt=f"{topic}の市場規模は2025年時点で約120億ドルと推計される。",
                    locator="p.1",
                    stance="supports",
                    verification="verified",
                )
            ],
        ),
        ClaimRecord(
            text=f"{topic}の年平均成長率は{growth}である。",
            key="growth-rate",
            value=growth,
            evidence=[
                EvidenceRecord(
                    source_url=sources[1].url,
                    excerpt=f"成長率の推計は調査機関により差がある ({engine}推計: {growth})。",
                    locator="表2",
                    stance="supports",
                    verification="verified",
                )
            ],
        ),
        ClaimRecord(
            text=f"{engine}のみが発見: {topic}には規制強化の動きがある (確度{_det_int(seed, engine, 'c3', mod=40) + 60}%)。",
            key=f"unique-{engine}",
            value=None,
            evidence=[
                EvidenceRecord(
                    source_url=sources[2].url,
                    excerpt=f"{engine}が独自に収集した{topic}の分析結果。",
                    locator=None,
                    stance="supports",
                    verification="unverified",
                )
            ],
        ),
    ]
    return claims


def _report_md(topic: str, engine: str, claims: list[ClaimRecord], sources: list[SourceRecord]) -> str:
    lines = [f"# {topic} 調査レポート ({engine})", "", "## 主要な発見", ""]
    for i, c in enumerate(claims, 1):
        lines.append(f"{i}. {c.text} [{i}]")
    lines += ["", "## 出典", ""]
    for i, s in enumerate(sources, 1):
        lines.append(f"[{i}] {s.title}: {s.url}")
    return "\n".join(lines)


class _MockBase(Engine):
    engine_id = "mock-base"
    display_name = "Mock Base"
    steps = 4
    step_seconds = 0.5
    emit_usage = True  # Falseならtoken/costを一切報告しない (欠損値の検証用)

    def capabilities(self) -> EngineCapabilities:
        return EngineCapabilities(
            engine_id=self.engine_id,
            name=self.display_name,
            version="1.0.0",
            output_kind="report",
            streaming=True,
            cancel=True,
            citations=True,
            token_usage=True,
            cost=True,
            options_schema={
                "type": "object",
                "properties": {
                    "seed": {"type": "integer", "default": 42},
                    "speed_factor": {"type": "number", "default": 1.0},
                },
            },
        )

    def _speed(self, ctx: RunContext) -> float:
        try:
            return max(0.001, float(ctx.request.options.get("speed_factor", 1.0)))
        except (TypeError, ValueError):
            return 1.0

    def _seed(self, ctx: RunContext) -> int:
        try:
            return int(ctx.request.options.get("seed", 42))
        except (TypeError, ValueError):
            return 42

    async def run(self, ctx: RunContext) -> RunResult:
        topic = ctx.request.input.topic
        seed = self._seed(ctx)
        speed = self._speed(ctx)
        sources = _mk_sources(topic, self.engine_id, seed)
        stages = ["planning", "searching", "reading", "writing"]
        searches = 0
        for i, stage in enumerate(stages):
            ctx.check_cancelled()
            ctx.emit("stage", {"stage": stage, "step": i + 1, "total": len(stages)})
            await ctx.sleep(self.step_seconds * speed)
            if stage == "searching":
                for q in range(2):
                    searches += 1
                    ctx.emit(
                        "search",
                        {"query": f"{topic} 調査クエリ{q + 1}", "provider": "mock", "results": 3},
                    )
            if stage == "reading":
                for s in sources:
                    ctx.emit("source_found", {"url": s.url, "title": s.title})
        claims = _base_claims(topic, self.engine_id, seed, sources)
        if self.emit_usage:
            prompt_tokens = 900 + _det_int(seed, self.engine_id, "pt", mod=200)
            completion_tokens = 400 + _det_int(seed, self.engine_id, "ct", mod=100)
            ctx.emit(
                "token_usage",
                {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens},
            )
            metrics = RunMetrics(
                searches=searches,
                sources=len(sources),
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=prompt_tokens + completion_tokens,
                llm_cost_usd=round((prompt_tokens + completion_tokens) / 1_000_000 * 3.0, 6),
                llm_cost_is_estimate=True,
                search_api_cost_usd=0.0,
                infra_cost="not_measured",
            )
            ctx.emit("cost", {"llm_cost_usd": metrics.llm_cost_usd, "search_api_cost_usd": 0.0})
        else:
            metrics = RunMetrics(
                searches=searches,
                sources=len(sources),
                search_api_cost_usd=0.0,
                infra_cost="not_measured",
            )
        return RunResult(
            output_kind="report",
            summary=f"{topic}に関する{self.display_name}の調査結果。",
            report_markdown=_report_md(topic, self.engine_id, claims, sources),
            claims=claims,
            sources=sources,
            metrics=metrics,
            raw={"engine": self.engine_id, "seed": seed, "deterministic": True},
        )


class MockFast(_MockBase):
    engine_id = "mock-fast"
    display_name = "Mock Fast"
    step_seconds = 0.3


class MockSlow(_MockBase):
    engine_id = "mock-slow"
    display_name = "Mock Slow"
    step_seconds = 1.5


class MockFail(_MockBase):
    engine_id = "mock-fail"
    display_name = "Mock Fail"
    step_seconds = 0.3

    async def run(self, ctx: RunContext) -> RunResult:
        ctx.emit("stage", {"stage": "planning", "step": 1, "total": 4})
        await ctx.sleep(self.step_seconds * self._speed(ctx))
        ctx.emit("stage", {"stage": "searching", "step": 2, "total": 4})
        await ctx.sleep(self.step_seconds * self._speed(ctx))
        raise RuntimeError("mock-fail: 決定論的な調査失敗 (検索バックエンド応答なし)")


class MockPartial(_MockBase):
    engine_id = "mock-partial"
    display_name = "Mock Partial"
    step_seconds = 0.4
    emit_usage = False  # token/costを報告しないエンジンの再現

    async def run(self, ctx: RunContext) -> RunResult:
        result = await super().run(ctx)
        # metrics欠損とwarning、引用のないclaimを混ぜる (捏造禁止の検証用)
        result.warnings.append("token使用量はこのエンジンから取得できません (値はnull)")
        result.claims.append(
            ClaimRecord(
                text=f"{ctx.request.input.topic}は今後5年で主流になる (根拠となる引用なし)。",
                key="unsupported-forecast",
                evidence=[],
            )
        )
        result.warnings.append("1件のclaimに引用がありません")
        return result


class MockTimeout(_MockBase):
    engine_id = "mock-timeout"
    display_name = "Mock Timeout"

    async def run(self, ctx: RunContext) -> RunResult:
        ctx.emit("stage", {"stage": "planning", "step": 1, "total": 4})
        # 完了しない: キャンセルかtimeoutまで進捗イベントだけ出し続ける
        step = 0
        while True:
            step += 1
            await ctx.sleep(0.5 * self._speed(ctx))
            ctx.emit("log", {"message": f"まだ調査中... ({step})"})


class MockCancellable(_MockBase):
    engine_id = "mock-cancellable"
    display_name = "Mock Cancellable"

    async def run(self, ctx: RunContext) -> RunResult:
        step = 0
        while True:
            step += 1
            ctx.check_cancelled()
            ctx.emit("stage", {"stage": "searching", "step": step, "total": 0})
            await ctx.sleep(0.3 * self._speed(ctx))


ALL_ENGINES = {
    e.engine_id: e
    for e in [
        MockFast(),
        MockSlow(),
        MockFail(),
        MockPartial(),
        MockTimeout(),
        MockCancellable(),
    ]
}
