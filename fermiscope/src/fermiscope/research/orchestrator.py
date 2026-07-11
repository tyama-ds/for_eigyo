"""ResearchOrchestrator — 調査パイプライン全体の実行。

問い正規化 → モデル生成(API側)→ 検索計画 → 検索 → 取得 → 抽出 → 採点 →
クラスタリング → 矛盾検出 → 統合 → 敵対的検証 → 再分解 → シミュレーション →
感度分析 → 検算 → レポート。

進捗イベントは実際の処理(検索数・文書数・検証数)に基づいて発行する。
架空のパーセンテージは発行しない。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

from fermiscope import __version__
from fermiscope.adversarial.verifier import verify_parameter
from fermiscope.config import Settings
from fermiscope.decomposition.engine import decide_decompositions
from fermiscope.domain.enums import (
    ParameterStatus,
    RunStage,
    RunStatus,
    SearchPurpose,
)
from fermiscope.domain.models import (
    EstimateProject,
    EvidenceItem,
    ModelCandidate,
    ParameterEstimate,
    ResearchRun,
    SimulationResult,
    utcnow,
)
from fermiscope.estimation.engine import (
    EstimationError,
    compute_scenarios,
    run_monte_carlo,
)
from fermiscope.estimation.fusion import fuse_evidence
from fermiscope.evidence.clustering import cluster_evidence
from fermiscope.evidence.contradiction import detect_contradictions
from fermiscope.evidence.dates import parse_year
from fermiscope.evidence.extractor import (
    extract_evidence,
    validate_llm_extraction,
)
from fermiscope.evidence.ranker import rank_evidence
from fermiscope.llm.base import LLMProvider
from fermiscope.research.fetcher import DocumentFetcher, FetchError
from fermiscope.research.planner import plan_searches
from fermiscope.research.search.service import SearchBudgetExceeded, SearchService
from fermiscope.security.boundary import wrap_untrusted
from fermiscope.security.url_guard import UrlGuardError
from fermiscope.sensitivity.engine import analyze_sensitivity
from fermiscope.validation.engine import validate_models

logger = logging.getLogger(__name__)

EmitFn = Callable[[str, str, dict[str, Any]], None]

_VALUE_PURPOSES = {
    SearchPurpose.DIRECT_VALUE,
    SearchPurpose.PRIMARY_SOURCE,
    SearchPurpose.LATEST_VALUE,
    SearchPurpose.ALTERNATIVE_VALUE,
}


class CancelledError(RuntimeError):
    pass


class ResearchOrchestrator:
    def __init__(
        self,
        settings: Settings,
        search_service: SearchService,
        fetcher: DocumentFetcher,
        llm: LLMProvider,
        emit: EmitFn | None = None,
    ) -> None:
        self.settings = settings
        self.search = search_service
        self.fetcher = fetcher
        self.llm = llm
        self._emit = emit or (lambda et, msg, data: None)

    # ---- ユーティリティ ----

    def _stage(self, project: EstimateProject, run: ResearchRun, stage: RunStage, msg: str) -> None:
        run.stage = stage
        project.audit("stage", msg, stage=stage.value)
        self._emit("stage", msg, self._counters(run))

    def _counters(self, run: ResearchRun) -> dict[str, Any]:
        return {
            "stage": run.stage.value,
            "searches_executed": run.searches_executed,
            "cache_hits": run.search_cache_hits,
            "documents_fetched": run.documents_fetched,
            "evidence_found": run.evidence_found,
            "parameters_verified": run.parameters_verified,
            "warnings": run.warnings_count,
            "ai_fallback_uses": run.ai_fallback_uses,
        }

    def _check_cancel(self, run: ResearchRun) -> None:
        if run.cancel_requested:
            raise CancelledError("ユーザーによりキャンセルされました")

    def _note_ai_fallback(
        self, project: EstimateProject, run: ResearchRun, where: str, detail: str
    ) -> None:
        run.ai_fallback_uses += 1
        project.audit("ai_fallback", f"AIフォールバック使用: {where}", detail=detail)
        self._emit("ai_fallback", f"AIフォールバック使用: {where}", self._counters(run))

    # ---- 個別ステップ ----

    async def _research_parameter(
        self,
        project: EstimateProject,
        run: ResearchRun,
        param: ParameterEstimate,
    ) -> None:
        """1パラメータの検索・取得・抽出・採点。"""
        param.status = ParameterStatus.RESEARCHING
        queries, ai_q = await plan_searches(
            param, project.research_mode, project.question.reference_date, self.llm
        )
        if ai_q:
            self._note_ai_fallback(project, run, "検索語生成", param.id)
        fetched_urls: set[str] = set()

        for sq in queries:
            self._check_cancel(run)
            try:
                hits = await self.search.run(sq)
            except SearchBudgetExceeded as exc:
                project.audit("warning", f"検索上限: {exc}", parameter_id=param.id)
                run.warnings_count += 1
                self._emit("warning", str(exc), self._counters(run))
                project.searches.append(sq)
                return
            project.searches.append(sq)
            run.searches_executed = self.search.session_searches
            run.search_cache_hits = self.search.cache_hits
            project.audit(
                "search",
                f"検索実行: {sq.query}",
                parameter_id=param.id,
                purpose=sq.purpose.value,
                results=len(hits),
                cache_hit=sq.cache_hit,
            )
            self._emit("search", f"検索: {sq.query}({len(hits)}件)", self._counters(run))

            new_fetches = 0
            for hit in hits:
                if new_fetches >= 3:
                    break
                self._check_cancel(run)
                key = f"{param.id}::{hit.url}"
                if key in fetched_urls:
                    continue
                fetched_urls.add(key)
                new_fetches += 1
                project.search_hits.append(hit)
                try:
                    doc = await self.fetcher.fetch(hit.url)
                except (FetchError, UrlGuardError) as exc:
                    project.audit(
                        "fetch_error", f"取得失敗: {hit.url}", error=str(exc), parameter_id=param.id
                    )
                    continue
                run.documents_fetched += 1
                project.audit(
                    "fetch",
                    f"取得: {hit.url}",
                    content_hash=doc.content_hash,
                    doc_type=doc.doc_type.value,
                    parameter_id=param.id,
                )

                items = extract_evidence(doc, param, sq.query, sq.purpose)

                # ルール抽出が失敗した場合のみLLM構造化抽出(フォールバック条件を明示)
                if not items and sq.purpose in _VALUE_PURPOSES and self.llm.available:
                    payload = await self.llm.extract_structured_evidence(
                        wrap_untrusted(doc.text), param.name, param.unit
                    )
                    if payload is not None:
                        ok, reason = validate_llm_extraction(
                            doc, param, payload.model_dump()
                        )
                        if ok:
                            item = EvidenceItem(
                                url=doc.url,
                                canonical_url=doc.final_url,
                                title=doc.title or doc.url,
                                retrieval_date=doc.fetched_at,
                                document_type=doc.doc_type,  # type: ignore[arg-type]
                                search_query=sq.query,
                                search_purpose=sq.purpose,
                                parameter_id=param.id,
                                extracted_value=payload.value,
                                extracted_low=payload.low,
                                extracted_high=payload.high,
                                unit=payload.unit,
                                time_period=payload.time_period,
                                population_definition=payload.population,
                                exact_definition=payload.definition,
                                short_supporting_excerpt=payload.excerpt[:300],
                                locator=payload.locator,
                                content_hash=doc.content_hash,
                                extraction_method="llm",
                                ai_assisted=True,
                            )
                            items = [item]
                            self._note_ai_fallback(
                                project, run, "構造化抽出", f"{param.id} ← {doc.url}"
                            )
                        else:
                            project.audit(
                                "ai_rejected",
                                f"LLM抽出を棄却: {reason}",
                                parameter_id=param.id,
                                url=doc.url,
                            )

                # 反証・訂正目的では値がなくてもメタデータ証拠を残す
                if not items and sq.purpose in (
                    SearchPurpose.COUNTER_EVIDENCE,
                    SearchPurpose.CORRECTION,
                ):
                    items = [
                        EvidenceItem(
                            url=doc.url,
                            canonical_url=doc.final_url,
                            title=doc.title or doc.url,
                            retrieval_date=doc.fetched_at,
                            document_type=doc.doc_type,  # type: ignore[arg-type]
                            search_query=sq.query,
                            search_purpose=sq.purpose,
                            parameter_id=param.id,
                            short_supporting_excerpt=doc.text[:300],
                            content_hash=doc.content_hash,
                            extraction_method="rule_text",
                        )
                    ]

                for item in items:
                    project.evidence[item.id] = item
                    run.evidence_found += 1
                self._emit(
                    "evidence",
                    f"証拠 {len(items)} 件抽出: {param.name}",
                    self._counters(run),
                )

    def _rank_and_fuse(
        self, project: EstimateProject, run: ResearchRun, param_ids: list[str]
    ) -> None:
        """クラスタリング → 採点 → 矛盾検出 → 統合。"""
        reference_year = parse_year(project.question.reference_date)
        for pid in param_ids:
            param = project.parameters[pid]
            items = [e for e in project.evidence.values() if e.parameter_id == pid]
            cluster_evidence(items, self.settings)
            for ev in items:
                rank_evidence(ev, param, self.settings, reference_year=reference_year)
                if not ev.scoring_reasons:
                    continue
            contradictions = detect_contradictions(param, items, self.settings)
            for con in contradictions:
                project.contradictions.append(con)
                project.audit(
                    "contradiction",
                    f"矛盾検出: {param.name}({con.ratio}倍の乖離)",
                    parameter_id=pid,
                    evidence_ids=con.evidence_ids,
                )
                run.warnings_count += 1

            if param.user_overridden:
                continue  # ユーザー上書き値は統合で壊さない
            value_items = [
                e for e in items if e.search_purpose in _VALUE_PURPOSES or e.search_purpose is None
            ]
            fuse_evidence(param, value_items, self.settings, reference_year=reference_year)
            if param.status == ParameterStatus.UNRESOLVED:
                project.audit(
                    "unresolved",
                    f"未解決パラメータ: {param.name}({param.unresolved_reason})",
                    parameter_id=pid,
                )
                run.warnings_count += 1
            else:
                project.audit(
                    "value_change",
                    f"パラメータ推定: {param.name} = {param.central:g} {param.unit}",
                    parameter_id=pid,
                    low=param.low,
                    high=param.high,
                    basis=param.value_basis.value,
                )

    async def _verify_parameters(
        self,
        project: EstimateProject,
        run: ResearchRun,
        model: ModelCandidate | None,
        param_ids: list[str],
    ) -> None:
        reference_year = parse_year(project.question.reference_date)
        for pid in param_ids:
            self._check_cancel(run)
            param = project.parameters[pid]
            critiques, ai_used = await verify_parameter(
                param, project.evidence, model, self.settings, reference_year, self.llm
            )
            if ai_used:
                self._note_ai_fallback(project, run, "批判仮説生成", pid)
            for c in critiques:
                project.critiques[c.id] = c
                if c.id not in param.critique_ids:
                    param.critique_ids.append(c.id)
            param.verification_note = (
                f"敵対的検証を実施({len(critiques)}件の指摘)"
                if critiques
                else "敵対的検証を実施(決定論チェック全項目で問題なし)"
            )
            run.parameters_verified += 1
            self._emit(
                "verified",
                f"敵対的検証: {param.name}({len(critiques)}件の指摘)",
                self._counters(run),
            )

    def _simulate_model(
        self, project: EstimateProject, model: ModelCandidate
    ) -> SimulationResult | None:
        cfg = project.simulation_config
        try:
            result, _samples, _outputs = run_monte_carlo(
                model, project.parameters, cfg, self.settings
            )
        except EstimationError as exc:
            project.audit(
                "warning",
                f"モデル {model.name} のシミュレーションを実行できません: {exc}",
                model_id=model.id,
            )
            return None
        project.simulation_results = [
            r for r in project.simulation_results if r.model_id != model.id
        ] + [result]
        project.audit(
            "simulation",
            f"モンテカルロ完了: {model.name}(反復 {result.iterations}、シード {result.seed})",
            model_id=model.id,
            median=result.median,
        )
        return result

    # ---- メインパイプライン ----

    async def run_research(self, project: EstimateProject) -> EstimateProject:
        run = ResearchRun(
            project_id=project.id,
            status=RunStatus.RUNNING,
            mode=project.research_mode,
            started_at=utcnow(),
            seed=project.simulation_config.seed,
            app_version=__version__,
            config_hash=self.settings.config_hash,
        )
        project.runs.append(run)
        project.app_version = __version__
        project.config_hash = self.settings.config_hash
        project.audit(
            "run_start",
            "調査を開始しました",
            seed=run.seed,
            app_version=run.app_version,
            config_hash=run.config_hash,
            mode=run.mode.value,
            llm_provider=self.llm.name,
            search_provider=self.search.provider.name,
        )

        try:
            primary = project.primary_model()
            check = project.check_model()
            if primary is None:
                raise EstimationError("主モデルがありません。モデル生成を先に実行してください。")

            researched: set[str] = set()
            for model in [m for m in (primary, check) if m is not None]:
                self._stage(
                    project, run, RunStage.SEARCHING, f"{model.name} のパラメータを調査中"
                )
                for pid in model.formula.leaf_parameter_ids():
                    if pid in researched:
                        continue
                    researched.add(pid)
                    await self._research_parameter(project, run, project.parameters[pid])

            self._stage(project, run, RunStage.RANKING, "証拠を採点・クラスタリング中")
            self._rank_and_fuse(project, run, sorted(researched))

            self._stage(project, run, RunStage.VERIFYING, "敵対的検証を実行中")
            for model in [m for m in (primary, check) if m is not None]:
                await self._verify_parameters(
                    project, run, model, model.formula.leaf_parameter_ids()
                )

            # シミュレーション → 感度 → 再分解 → (必要なら)再調査、最大2周
            for round_no in range(2):
                self._check_cancel(run)
                self._stage(
                    project,
                    run,
                    RunStage.SIMULATING,
                    f"モンテカルロシミュレーション実行中(第{round_no + 1}round)",
                )
                sims: dict[str, SimulationResult] = {}
                for model in [m for m in (primary, check) if m is not None]:
                    sim = self._simulate_model(project, model)
                    if sim is not None:
                        sims[model.id] = sim

                self._stage(project, run, RunStage.SENSITIVITY, "感度分析を計算中")
                project.sensitivity_results = []
                for model in [m for m in (primary, check) if m is not None]:
                    if model.id in sims:
                        project.sensitivity_results.extend(
                            analyze_sensitivity(
                                model, project.parameters, sims[model.id], project.critiques
                            )
                        )

                if round_no >= 1:
                    break  # 分解は1回まで(上限は設定でも制御)

                self._stage(project, run, RunStage.DECOMPOSING, "再分解の判断中")
                new_param_ids: list[str] = []
                for model in [m for m in (primary, check) if m is not None]:
                    sens = [s for s in project.sensitivity_results if s.model_id == model.id]
                    attempts, irreducibles, ai_used = await decide_decompositions(
                        model,
                        project.parameters,
                        project.critiques,
                        sens,
                        self.settings,
                        self.llm,
                    )
                    if ai_used:
                        self._note_ai_fallback(project, run, "分解候補生成", model.id)
                    project.decomposition_attempts.extend(attempts)
                    for irr in irreducibles:
                        if not any(
                            i.parameter_id == irr.parameter_id
                            for i in project.irreducible_assumptions
                        ):
                            project.irreducible_assumptions.append(irr)
                    for attempt in attempts:
                        project.audit(
                            "decomposition",
                            f"分解{'採用' if attempt.accepted else '却下'}: "
                            f"{attempt.parameter_id} → {attempt.expression or '(候補なし)'}",
                            accepted=attempt.accepted,
                            reason=attempt.rejection_reason,
                            checks=attempt.checks,
                        )
                        if attempt.accepted:
                            new_param_ids.extend(p.id for p in attempt.child_parameters)

                if not new_param_ids:
                    break

                self._stage(
                    project, run, RunStage.SEARCHING, "分解後の下位パラメータを調査中"
                )
                for pid in new_param_ids:
                    self._check_cancel(run)
                    await self._research_parameter(project, run, project.parameters[pid])
                self._rank_and_fuse(project, run, new_param_ids)
                for model in [m for m in (primary, check) if m is not None]:
                    await self._verify_parameters(
                        project,
                        run,
                        model,
                        [p for p in new_param_ids if p in model.formula.leaf_parameter_ids()],
                    )

            # シナリオ
            primary_sim = next(
                (r for r in project.simulation_results if r.model_id == primary.id), None
            )
            project.scenarios = []
            if primary_sim is not None:
                project.scenarios = compute_scenarios(
                    primary, project.parameters, primary_sim, self.settings
                )

            # 検算
            self._stage(project, run, RunStage.VALIDATING, "検算モデルと比較中")
            check_sim = (
                next((r for r in project.simulation_results if r.model_id == check.id), None)
                if check is not None
                else None
            )
            if check is not None and primary_sim is not None and check_sim is not None:
                project.validation = validate_models(
                    primary,
                    check,
                    primary_sim,
                    check_sim,
                    project.parameters,
                    project.evidence,
                    project.critiques,
                    self.settings,
                )
                run.warnings_count += len(project.validation.warnings)
                for w in project.validation.warnings:
                    project.audit("validation_warning", w)

            self._stage(project, run, RunStage.REPORTING, "結果を整理中")
            self._finalize_confidence(project)

            run.status = RunStatus.DONE
            run.stage = RunStage.DONE
            run.finished_at = utcnow()
            project.audit("run_done", "調査が完了しました", **self._counters(run))
            self._emit("done", "調査が完了しました", self._counters(run))
        except asyncio.CancelledError:
            # サーバ停止やタスクキャンセルで送出される。status を確定させてから
            # 再送出し、RUNNING のまま永続化されるのを防ぐ(呼び出し側の finally で保存)。
            run.status = RunStatus.CANCELLED
            run.stage = RunStage.CANCELLED
            run.finished_at = utcnow()
            project.audit("run_cancelled", "調査が中断されました(タスクキャンセル)")
            raise
        except CancelledError:
            run.status = RunStatus.CANCELLED
            run.stage = RunStage.CANCELLED
            run.finished_at = utcnow()
            project.audit("run_cancelled", "調査はキャンセルされました")
            self._emit("cancelled", "調査はキャンセルされました", self._counters(run))
        except Exception as exc:  # noqa: BLE001 — 失敗理由を必ず記録して終了する
            logger.exception("research run failed")
            run.status = RunStatus.FAILED
            run.stage = RunStage.FAILED
            run.error = f"{type(exc).__name__}: {exc}"
            run.finished_at = utcnow()
            project.audit("run_failed", f"調査が失敗しました: {run.error}")
            self._emit("failed", f"調査が失敗しました: {run.error}", self._counters(run))
        return project

    def _finalize_confidence(self, project: EstimateProject) -> None:
        primary = project.primary_model()
        reasons: list[str] = []
        caveats: list[str] = []
        if primary is None:
            project.overall_confidence = None
            return
        leaf_ids = primary.formula.leaf_parameter_ids()
        confs = [c for p in leaf_ids if (c := project.parameters[p].confidence) is not None]
        base = sum(confs) / len(confs) if confs else 0.2
        reasons.append(f"主モデル{len(leaf_ids)}パラメータの証拠信頼度平均: {base:.2f}")

        unresolved = [p for p in leaf_ids if project.parameters[p].central is None]
        if unresolved:
            base -= 0.2
            caveats.append(
                f"未解決のパラメータが{len(unresolved)}件あります(値を入力してください)。"
            )

        severe = [
            c
            for c in project.critiques.values()
            if c.parameter_id in leaf_ids and c.severity >= 0.6 and c.resolution_status == "open"
        ]
        if severe:
            base -= min(0.05 * len(severe), 0.2)
            reasons.append(f"未解決の重大な批判 {len(severe)} 件により減点。")

        if project.validation is not None:
            if project.validation.agreement == "consistent":
                base += 0.05
                reasons.append("検算モデルと桁が整合(+0.05)。")
            elif project.validation.agreement == "discrepant":
                base -= 0.15
                caveats.append("検算モデルと大きな不一致があります(モデル間不一致を参照)。")

        for irr in project.irreducible_assumptions:
            p = project.parameters.get(irr.parameter_id)
            caveats.append(
                f"「{p.name if p else irr.parameter_id}」はこれ以上分解できない仮定です: {irr.reason}"
            )
        for con in project.contradictions:
            p = project.parameters.get(con.parameter_id)
            caveats.append(f"「{p.name if p else con.parameter_id}」の証拠間に矛盾があります。")

        project.overall_confidence = round(min(max(base, 0.05), 0.95), 2)
        project.confidence_reasons = reasons
        project.key_caveats = caveats


def recalculate_project(project: EstimateProject, settings: Settings) -> EstimateProject:
    """検索を再実行せず、現在のパラメータ値でローカル再計算する。"""
    primary = project.primary_model()
    check = project.check_model()
    project.simulation_results = []
    project.sensitivity_results = []
    sims: dict[str, SimulationResult] = {}
    for model in [m for m in (primary, check) if m is not None]:
        try:
            result, _s, _o = run_monte_carlo(
                model, project.parameters, project.simulation_config, settings
            )
        except EstimationError as exc:
            project.audit("warning", f"再計算不能: {model.name}: {exc}", model_id=model.id)
            continue
        sims[model.id] = result
        project.simulation_results.append(result)
        project.sensitivity_results.extend(
            analyze_sensitivity(model, project.parameters, result, project.critiques)
        )
    if primary is not None and primary.id in sims:
        custom = next(
            (s.parameter_overrides for s in project.scenarios if s.kind == "custom"), None
        )
        project.scenarios = compute_scenarios(
            primary, project.parameters, sims[primary.id], settings, custom_overrides=custom
        )
    if primary is not None and check is not None and primary.id in sims and check.id in sims:
        project.validation = validate_models(
            primary,
            check,
            sims[primary.id],
            sims[check.id],
            project.parameters,
            project.evidence,
            project.critiques,
            settings,
        )
    project.audit(
        "recalculate",
        "ローカル再計算を実行しました(Web検索なし)",
        seed=project.simulation_config.seed,
    )
    return project
