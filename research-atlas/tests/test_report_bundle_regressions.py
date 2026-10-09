"""Preserve saved scientific scope when adapting reports for Office/PDF."""
from copy import deepcopy
import json

import pytest

from app import report_bundle, report_documents, report_export_api, storage


def result():
    return {"id": "a" * 32, "dataset_name": "保存データ", "meta": {}, "topics": [], "timeline": []}


def report(**values):
    return {"id": "b" * 32, "result_id": "a" * 32, **values}


def prose(bundle):
    return "\n".join(section["title"] + "\n" + section["text"] for section in bundle["sections"])


def audit(bundle):
    return list(next(table["rows"] for table in bundle["tables"] if table.get("xlsx_only")))


def test_saved_insight_output_keeps_findings_hypotheses_and_evidence_ids():
    value = result()
    value["insights"] = {"mode": "openai", "headline": "保存された解釈", "model": "saved-model",
        "findings": ["OBSERVATION_ONE", "OBSERVATION_TWO"], "hypotheses": ["CONDITIONAL_HYPOTHESIS"],
        "evidence_ids": ["source-first", "source-last"], "caveats": ["HYPOTHESIS_NOT_VALIDATED"]}
    before = deepcopy(value)
    bundle = report_bundle.build_bundle("result", value, value)
    content = prose(bundle)
    for expected in ("OBSERVATION_ONE", "OBSERVATION_TWO", "CONDITIONAL_HYPOTHESIS", "source-first", "source-last", "HYPOTHESIS_NOT_VALIDATED"):
        assert expected in content
    assert "仮説" in content and "観測" in content and "saved-model" in content
    assert value == before


@pytest.mark.parametrize("status", ["running", "pending", "partial", "stale", "failed"])
def test_corpus_old_group_prose_is_retained_but_marked_as_unfinished(status):
    value = report(status="running", synthesis_status="running", counts={"total": 20, "completed": 20},
        groups=[{"label": "対象分野", "status": status, "source_count": 20,
                 "narrative": {"text": "PREVIOUS_SAVED_REVIEW"}}])
    bundle = report_bundle.build_bundle("corpus", value, result())
    assert "PREVIOUS_SAVED_REVIEW" in prose(bundle)
    assert "synthesis_status: running" in prose(bundle)
    assert any("対象分野" in warning and "前の処理時点" in warning for warning in bundle["warnings"])


def test_completed_group_has_no_false_stale_warning():
    value = report(synthesis_status="completed", counts={"total": 1, "completed": 1},
        groups=[{"label": "完了した分野", "status": "completed", "narrative": {"text": "READY"}}])
    bundle = report_bundle.build_bundle("corpus", value, result())
    assert not any("前の処理時点" in warning for warning in bundle["warnings"])
    assert "synthesis_status: completed" in prose(bundle)


def test_twenty_thousand_reference_ids_are_bounded_in_prose_and_complete_in_audit():
    identifiers = [f"paper-{index:05d}" for index in range(20_000)]
    body = "COMMENTARY_START\n" + "保存された科学的評論を省略しません。" * 250 + "\nCOMMENTARY_END"
    value = report(counts={"total": 20_000, "completed": 20_000}, narrative={
        "text": "全件に基づく保存済み要約", "sections": [{"title": "総合比較", "text": body, "paper_ids": identifiers}]})
    before = deepcopy(value)
    bundle = report_bundle.build_bundle("corpus", value, result())
    content = prose(bundle)
    assert body in content
    assert identifiers[0] in content and identifiers[11] in content
    assert identifiers[12] not in content and identifiers[-1] not in content
    assert "20,000" in content and "Excel全指標" in content
    exported_ids = [row[2] for row in audit(bundle) if row[0].startswith("/narrative/sections/0/paper_ids/")]
    assert exported_ids == identifiers
    assert value == before


def test_candidate_selection_filters_reviews_rounds_and_user_feedback_without_mutation():
    value = report(candidates=[
        {"id": "chosen", "label": "選択候補", "narrative": {"text": "CHOSEN_REVIEW"}},
        {"id": "other", "label": "別候補", "narrative": {"text": "OTHER_REVIEW"}}],
        feedback=[{"candidate_id": "chosen", "paper_id": "p1", "stance": "CHOSEN_FEEDBACK"},
                  {"candidate_id": "other", "paper_id": "p2", "stance": "OTHER_FEEDBACK"}],
        rounds=[{"candidate_id": "chosen", "notes": "CHOSEN_ROUND"}, {"candidate_id": "other", "notes": "OTHER_ROUND"}])
    before = deepcopy(value)
    bundle = report_bundle.build_bundle("foresight", value, result(), candidate_id="chosen")
    content = prose(bundle) + json.dumps(audit(bundle), ensure_ascii=False)
    for expected in ("CHOSEN_REVIEW", "CHOSEN_FEEDBACK", "CHOSEN_ROUND"):
        assert expected in content
    for omitted in ("OTHER_REVIEW", "OTHER_FEEDBACK", "OTHER_ROUND"):
        assert omitted not in content
    assert value == before


def test_heuristic_facts_keep_verification_stance_attribution_and_readiness_limits():
    value = report(candidates=[{"id": "candidate", "label": "候補", "evidence": [{
        "paper_id": "p1", "text": "The prior study reported a limitation.",
        "verification": "heuristic", "stance": "counter", "attribution": "prior_work"}],
        "readiness": {"notes": ["KEYWORD_MENTION_IS_NOT_READINESS"], "dimensions": []}}])
    content = prose(report_bundle.build_bundle("foresight", value, result()))
    for expected in ("The prior study reported a limitation.", "検証状態: heuristic", "論旨: counter", "帰属: prior_work", "KEYWORD_MENTION_IS_NOT_READINESS"):
        assert expected in content


@pytest.mark.parametrize("format", ["pdf", "docx", "xlsx", "pptx"])
def test_field_auxiliary_manifest_is_restored_for_complete_export_audit(tmp_path, monkeypatch, format):
    monkeypatch.setenv("ATLAS_DATA_DIR", str(tmp_path))
    saved_result = result()
    saved_result["papers"] = [{"id": "p1", "topic_id": "focus", "abstract": "source"},
                               {"id": "p2", "topic_id": "outside", "abstract": "other"}]
    storage.save("results", saved_result)
    owner = "c" * 32
    auxiliary = {"institutions": [{"name": "VISIBLE_ROW", "focus_count": 1},
                                   {"name": "BEYOND_DISPLAY_LIMIT_ROW", "focus_count": 9}],
                 "shared_authors": [{"id": "COMPLETE_AUTHOR_ROW"}]}
    (tmp_path / "large").mkdir(exist_ok=True)
    filename = owner + ".export_data.json"
    (tmp_path / "large" / filename).write_text(json.dumps(auxiliary), encoding="utf-8")
    saved = report(focus={"id": "focus", "label": "対象分野"}, neighbor=None,
        institutions={"rows": auxiliary["institutions"][:1]}, export_data={},
        _large_store={"owner": owner, "count": 0, "export_data": filename})
    storage.save("field_reports", saved)
    assert storage.read("field_reports", saved["id"], include_papers=False)["export_data"] == {}
    captured = {}
    def render(bundle, chosen_format, records=None):
        captured.update(format=chosen_format, audit=audit(bundle),
                        records=None if records is None else list(records))
        return b"document-container"
    monkeypatch.setattr(report_documents, "render_report", render)
    response = report_export_api.document_response("field", saved["id"],
        report_export_api.ExportOptions(format=format, include_figures=False))
    assert response.body == b"document-container" and captured["format"] == format
    leaves = {row[0]: row[2] for row in captured["audit"]}
    assert leaves["/export_data/institutions/1/name"] == "BEYOND_DISPLAY_LIMIT_ROW"
    assert leaves["/export_data/shared_authors/0/id"] == "COMPLETE_AUTHOR_ROW"
    if format == "xlsx":
        assert [row["id"] for row in captured["records"]] == ["p1"]
    else:
        assert captured["records"] is None
