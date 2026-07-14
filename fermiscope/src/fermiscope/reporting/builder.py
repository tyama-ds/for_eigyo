"""report_builder — 結果・式・証拠・批判・注釈を1つの構造にまとめる。

GUI とエクスポートの両方がこの構造を使う。数値の生成はここでは行わない
(すべて推定エンジンの計算結果の整理のみ)。
"""

from __future__ import annotations

from typing import Any

from fermiscope.domain.models import EstimateProject
from fermiscope.formula.graph import render_expression


def _format_number(value: float | None) -> str:
    if value is None:
        return "—"
    a = abs(value)
    if a >= 1e12:
        return f"{value / 1e12:,.2f}兆"
    if a >= 1e8:
        return f"{value / 1e8:,.2f}億"
    if a >= 1e4:
        return f"{value / 1e4:,.1f}万"
    if a >= 100:
        return f"{value:,.0f}"
    if a >= 1:
        return f"{value:,.2f}"
    return f"{value:.4g}"


def build_report(project: EstimateProject) -> dict[str, Any]:
    """プロジェクト状態から表示・エクスポート用のレポート構造を組み立てる。"""
    primary = project.primary_model()
    primary_sim = next(
        (r for r in project.simulation_results if primary and r.model_id == primary.id), None
    )
    scenarios = [
        {
            "kind": s.kind,
            "name": s.name,
            "value": s.value,
            "value_display": _format_number(s.value),
            "quantile": s.quantile,
            "description": s.description,
        }
        for s in project.scenarios
    ]

    symbols = {pid: p.symbol or pid for pid, p in project.parameters.items()}

    parameters = []
    for pid, p in sorted(project.parameters.items()):
        critiques = [project.critiques[cid] for cid in p.critique_ids if cid in project.critiques]
        parameters.append(
            {
                "id": pid,
                "name": p.name,
                "symbol": p.symbol,
                "definition": p.definition or p.description,
                "unit": p.unit,
                "central": p.central,
                "low": p.low,
                "high": p.high,
                "central_display": _format_number(p.central),
                "distribution": p.distribution.value,
                "distribution_rationale": p.distribution_rationale,
                "value_basis": p.value_basis.value,
                "status": p.status.value,
                "unresolved_reason": p.unresolved_reason,
                "confidence": p.confidence,
                "sensitivity": p.sensitivity,
                "geography": p.target_geography,
                "period": p.target_period,
                "evidence_ids": p.evidence_ids,
                "assumptions": p.assumptions,
                "fusion_note": p.fusion_note,
                "critique_count": len(critiques),
                "max_critique_severity": max((c.severity for c in critiques), default=0.0),
                "verification_note": p.verification_note,
                "decomposition_status": p.decomposition_status.value,
                "user_overridden": p.user_overridden,
                "ai_assisted": p.ai_assisted,
                "depth": p.depth,
                "parent_parameter_id": p.parent_parameter_id,
                "history": [h.model_dump(mode="json") for h in p.history],
            }
        )

    evidence = []
    for e in project.evidence.values():
        evidence.append(
            {
                "id": e.id,
                "parameter_id": e.parameter_id,
                "url": e.url,
                "title": e.title,
                "publisher": e.publisher,
                "source_class": e.source_class.value,
                "evidence_score": e.evidence_score,
                "publication_date": e.publication_date,
                "retrieval_date": e.retrieval_date.isoformat() if e.retrieval_date else None,
                "time_period": e.time_period,
                "geography": e.geography,
                "methodology_summary": e.methodology_summary,
                "extracted_value": e.extracted_value,
                "normalized_value": e.normalized_value,
                "unit": e.unit,
                "excerpt": e.short_supporting_excerpt,
                "locator": e.locator,
                "cluster_id": e.cluster_id,
                "accepted": e.accepted,
                "rejection_reason": e.rejection_reason,
                "incompatible_reason": e.incompatible_reason,
                "extraction_method": e.extraction_method,
                "ai_assisted": e.ai_assisted,
                "scoring_reasons": e.scoring_reasons,
                "subscores": e.subscores,
                "penalties": e.penalties_applied,
                "purpose": e.search_purpose.value if e.search_purpose else None,
            }
        )

    critiques_out = [
        {
            "id": c.id,
            "parameter_id": c.parameter_id,
            "issue_type": c.issue_type.value,
            "claim": c.claim,
            "severity": c.severity,
            "probability": c.probability,
            "direction": c.likely_direction_of_bias,
            "estimated_impact": c.estimated_impact,
            "recommended_action": c.recommended_action,
            "resolution_status": c.resolution_status.value,
            "resolution_note": c.resolution_note,
            "detected_by": c.detected_by,
            "check_detail": c.check_detail,
            "ai_assisted": c.ai_assisted,
            "supporting_evidence_ids": c.supporting_evidence_ids,
            "opposing_evidence_ids": c.opposing_evidence_ids,
        }
        for c in project.critiques.values()
    ]

    sensitivity = [
        {
            "model_id": s.model_id,
            "parameter_id": s.parameter_id,
            "parameter_name": s.parameter_name,
            "oat_low_output": s.oat_low_output,
            "oat_high_output": s.oat_high_output,
            "oat_span": s.oat_span,
            "elasticity": s.elasticity,
            "spearman": s.spearman,
            "uncertainty_span": s.uncertainty_span,
            "direction": s.direction,
            "critique_severity": s.critique_severity,
            "importance": s.importance,
            "contribution_rank": s.contribution_rank,
            "expected_improvement": s.expected_improvement,
        }
        for s in project.sensitivity_results
    ]

    models = [
        {
            "id": m.id,
            "name": m.name,
            "role": m.role,
            "approach": m.approach,
            "description": m.description,
            "expression": render_expression(m.formula.root, symbols),
            "expression_raw": m.formula.expression,
            "formula_tree": m.formula.root.model_dump(mode="json"),
            "target_unit": m.formula.target_unit,
            "unit_check_passed": m.formula.unit_check_passed,
            "unit_check_detail": m.formula.unit_check_detail,
            "scores": m.scores,
            "total_score": m.total_score,
            "selection_reason": m.selection_reason,
            "proposed_by": m.proposed_by,
            "parameter_ids": m.formula.leaf_parameter_ids(),
        }
        for m in project.models
    ]

    run = project.current_run()
    return {
        "project": {
            "id": project.id,
            "name": project.name,
            "created_at": project.created_at.isoformat(),
            "updated_at": project.updated_at.isoformat(),
            "app_version": project.app_version,
            "config_hash": project.config_hash,
            "research_mode": project.research_mode.value,
        },
        "question": project.question.model_dump(mode="json"),
        "conclusion": {
            "central": primary_sim.median if primary_sim else None,
            "central_display": _format_number(primary_sim.median if primary_sim else None),
            # 期間を含む単位を表示する(フローは item/day 等)。主モデルの目標単位を優先。
            "unit": (primary.formula.target_unit if primary else "") or project.question.target_unit,
            "range_low": primary_sim.quantiles.get("0.1") if primary_sim else None,
            "range_high": primary_sim.quantiles.get("0.9") if primary_sim else None,
            "range_display": (
                f"{_format_number(primary_sim.quantiles.get('0.1'))} 〜 "
                f"{_format_number(primary_sim.quantiles.get('0.9'))}"
                if primary_sim
                else "—"
            ),
            "confidence": project.overall_confidence,
            "confidence_reasons": project.confidence_reasons,
            "key_caveats": project.key_caveats,
        },
        "scenarios": scenarios,
        "models": models,
        "parameters": parameters,
        "evidence": evidence,
        "critiques": critiques_out,
        "contradictions": [c.model_dump(mode="json") for c in project.contradictions],
        "irreducible_assumptions": [
            i.model_dump(mode="json") for i in project.irreducible_assumptions
        ],
        "decomposition_attempts": [
            a.model_dump(mode="json", exclude={"child_parameters"})
            for a in project.decomposition_attempts
        ],
        "sensitivity": sensitivity,
        "simulation": {
            "config": project.simulation_config.model_dump(mode="json"),
            "results": [r.model_dump(mode="json") for r in project.simulation_results],
        },
        "validation": project.validation.model_dump(mode="json") if project.validation else None,
        "run": run.model_dump(mode="json") if run else None,
        "audit_events": [a.model_dump(mode="json") for a in project.audit_events],
        "searches": [q.model_dump(mode="json") for q in project.searches],
    }
