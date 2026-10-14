from copy import deepcopy
import threading

import pytest
from pydantic import ValidationError

from app import annual_landscape_api as api, annual_landscape_reports as reports
from app import landscape_reports, landscape_selection, storage


@pytest.fixture
def result(tmp_path, monkeypatch):
    monkeypatch.setenv("ATLAS_DATA_DIR", str(tmp_path))
    papers = [{"id": f"p{year}-{index}", "title": "Alloy tensile behavior", "abstract": "The alloy specimen was tested.",
               "year": year, "publication_date": f"{year}-06", "date_precision": "month", "topic_id": "t1",
               "citations": index, "landscape_vector": [1., index / 100.]}
              for year in (2021, 2022, 2023) for index in range(8)]
    value = {"id": storage.new_id(), "meta": {}, "papers": papers,
             "topics": [{"id": "t1", "label": "Materials"}],
             "map": {"nodes": [{"id": p["id"], "topic_id": "t1", "year": p["year"], "x": .5, "y": .5} for p in papers],
                     "projection_inputs": {"paper_ids": [p["id"] for p in papers],
                        "vectors": [p["landscape_vector"] for p in papers], "embedding": "nmf", "source": "saved_analysis_representation"}}}
    storage.save("results", value)
    return value


def request_body(result, **kwargs):
    return {"result_id": result["id"], "projection": "pca", "scope": "sample", "topic_id": "t1",
            "start_year": 2021, "end_year": 2023, "provider": "none", **kwargs}


def prepare(result, **kwargs):
    return reports.prepare_report(result["id"], "pca", "sample", "t1", 2021, 2023, **kwargs)


@pytest.mark.parametrize("invalid", [{"papers_per_period": 0}, {"papers_per_period": 21},
    {"papers_per_period": True}, {"papers_per_period": "8"}, {"selection_method": "random"},
    {"abstract_only": "true"}, {"abstract_only": 1}])
def test_request_selection_controls_are_strict(result, invalid):
    with pytest.raises(ValidationError):
        api.AnnualLandscapeRequest(**request_body(result, **invalid))


def test_initial_record_persists_options_and_export_preserves_them(result):
    options = api.AnnualLandscapeRequest(**request_body(result, papers_per_period=20,
        selection_method="cited", abstract_only=True)).model_dump()
    report = reports.initial_report(options, result)
    assert report["selection"] == {"papers_per_period": 20, "selection_method": "cited", "abstract_only": True,
        "method_label": landscape_selection.METHOD_LABELS["cited"], "applies_to": "transitions"}
    text = reports.export_csv(report)
    assert "/selection/papers_per_period" in text
    assert "/selection/abstract_only" in text
    assert "transitions" in text


def test_default_and_no_transition_jobs_do_not_construct_selection_context(result, monkeypatch):
    monkeypatch.setattr(landscape_selection, "SelectionContext", lambda *a: pytest.fail("Default/no-transition context should remain lazy"))
    default = prepare(result, include_transitions=True)
    no_transitions = prepare(result, papers_per_period=20, selection_method="cited")
    assert len(default["transitions"]) == 2
    assert no_transitions["transitions"] == []
    assert default["selection"]["papers_per_period"] == 6
    assert no_transitions["selection"]["papers_per_period"] == 20
    assert all(len(row["report"]["evidence_papers"]) == 6 for row in no_transitions["years"])


def test_custom_selection_context_reused_for_both_transitions_and_keeps_yearly_chapters(result, monkeypatch):
    baseline = prepare(result)
    factory = landscape_selection.SelectionContext
    contexts, calls = [], []
    def construct(*args):
        value = factory(*args)
        contexts.append(value)
        return value
    monkeypatch.setattr(landscape_selection, "SelectionContext", construct)
    original_prepare = landscape_reports.prepare_report
    def child(*args, **kwargs):
        calls.append(kwargs)
        return original_prepare(*args, **kwargs)
    monkeypatch.setattr(landscape_reports, "prepare_report", child)
    report = prepare(result, include_transitions=True, papers_per_period=7, selection_method="cited", abstract_only=True)
    assert len(contexts) == 1
    assert len(calls) == 2
    assert all(call["selection_context"] is contexts[0] for call in calls)
    assert all(call["papers_per_period"] == 7 and call["selection_method"] == "cited" and call["abstract_only"] for call in calls)
    assert report["annual_rows"] == baseline["annual_rows"]
    for row, old in zip(report["years"], baseline["years"]):
        assert row["report"]["centroid"] == old["report"]["centroid"]
        assert row["report"]["evidence_papers"] == old["report"]["evidence_papers"]
    for row in report["transitions"]:
        assert row["generation_status"] == "not_requested"
        papers = row["report"]["evidence_papers"]
        assert len([p for p in papers if p["side"] == "before"]) == 7
        assert len([p for p in papers if p["side"] == "after"]) == 7


def test_selection_context_failure_keeps_all_years_and_partial_snapshots(result, monkeypatch):
    calls, snapshots = [], []
    def broken(*args):
        calls.append(args)
        raise ValueError("cached representation unavailable")
    monkeypatch.setattr(landscape_selection, "SelectionContext", broken)
    report = prepare(result, include_transitions=True, papers_per_period=20, on_prepare=lambda value: snapshots.append(deepcopy(value)))
    assert len(calls) == 1
    assert [row["count"] for row in report["years"]] == [8, 8, 8]
    assert all(row["report"] is not None for row in report["years"])
    assert all(row["generation_status"] == "failed" for row in report["transitions"])
    assert report["generation_status"] == "partial"
    assert any(len(snapshot["years"]) == 3 and not snapshot["transitions"] for snapshot in snapshots)


def test_custom_missing_period_skips_comparison_without_erasing_metrics(result):
    for paper in result["papers"]:
        if paper["year"] == 2022:
            paper["abstract"] = ""
    storage.save("results", result)
    report = prepare(result, include_transitions=True, provider="local", papers_per_period=8, abstract_only=True)
    assert all(row["generation_status"] == "skipped" for row in report["transitions"])
    assert all(row["generation_error_kind"] == "selection_missing_period" for row in report["transitions"])
    assert all(row["report"] is not None for row in report["transitions"])
    assert [row["count"] for row in report["annual_rows"]] == [8, 8, 8]
    assert report["years"][1]["generation_error_kind"] == "missing_abstracts"
    assert report["years"][0]["generation_status"] == report["years"][2]["generation_status"] == "pending"


def test_all_missing_abstracts_keeps_existing_error_priority(result):
    child = {"kind": "movement", "selection": {"papers_per_period": 8}, "evidence_papers": []}
    state = reports._child_state(child, "local")
    assert state["generation_status"] == "skipped"
    assert state["generation_error_kind"] == "missing_abstracts"


def test_annual_api_worker_forwards_and_persists_selection_controls(result):
    from app.main import JOBS, new_job
    options = api.AnnualLandscapeRequest(**request_body(result, include_transitions=True,
        papers_per_period=7, selection_method="recent", abstract_only=True)).model_dump()
    report = reports.initial_report(options, result)
    storage.save(reports.STORE_KIND, report)
    job_id = new_job("Selection integration test")
    api.run_annual_report(job_id, report["id"], options, threading.Event())
    saved_report = storage.read(reports.STORE_KIND, report["id"])
    assert JOBS[job_id]["status"] == "completed"
    assert saved_report["selection"]["selection_method"] == "recent"
    assert all(len(row["report"]["evidence_papers"]) == 14 for row in saved_report["transitions"])
    assert saved_report["progress"]["llm_calls"] == 0
