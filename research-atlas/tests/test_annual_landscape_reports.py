import base64
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import csv
import io
import json
import threading
import time

import pytest
from fastapi.testclient import TestClient

from app import annual_landscape_api as api, annual_landscape_reports as reports
from app import centroid_reports, field_llm, landscape, landscape_reports, storage
from app.connection_settings import current_settings
from app.local_llm_stream import LocalStreamError
from app.main import app


@pytest.fixture
def saved(tmp_path, monkeypatch):
    monkeypatch.setenv("ATLAS_DATA_DIR", str(tmp_path))
    papers = [{"id": "p20a", "title": "Steel laser study", "year": 2020, "abstract": "Laser heating improved the steel specimen."},
              {"id": "p20b", "title": "Steel material study", "year": 2020, "abstract": "A steel specimen was evaluated."},
              {"id": "other21", "title": "Other topic", "year": 2021, "abstract": "Other research."},
              {"id": "p23", "title": "Welding study without abstract", "year": 2023, "abstract": ""},
              {"id": "p24", "title": "No usable vector", "year": 2024, "abstract": "Unknown vector."},
              {"id": "p25", "title": "Laser sensor study", "year": 2025, "abstract": "Laser sensors measured the material."},
              {"id": "other25a", "title": "Other topic a", "year": 2025, "abstract": "Other."},
              {"id": "other25b", "title": "Other topic b", "year": 2025, "abstract": "Other."}]
    result = {"id": storage.new_id(), "meta": {"is_demo": True}, "papers": papers,
              "topics": [{"id": "t1", "label": "Materials"}, {"id": "t2", "label": "Other"},
                         {"id": "unclassified", "label": "Unknown", "status": "unclassified"}]}
    storage.save("results", result)
    periods = [{"id": str(year), "index": i, "count": count, "observed": bool(count), "count_scope": "full_corpus"}
               for i, (year, count) in enumerate(((2020, 2), (2021, 1), (2022, 0), (2023, 1), (2024, 1), (2025, 3)))]
    centers = []
    for year, ids, count, denominator, terms in ((2020, ["p20a", "p20b"], 2, 2, ["steel", "laser"]),
            (2023, ["p23"], 1, 1, ["welding"]), (2024, [], 1, 1, ["laser"]),
            (2025, ["p25"], 1, 3, ["sensor", "laser"])):
        centers.append({"topic_id": "t1", "period_id": str(year), "count": count, "period_count": denominator,
                        "share_of_period": count / denominator, "valid_vector_count": count if ids else 0,
                        "evidence_ids": ids, "paper_ids": ids, "x": .2, "y": .4,
                        "terms": [{"term": term, "count": count} for term in terms], "count_scope": "full_corpus"})
    movements = []
    for before, after in zip(centers, centers[1:]):
        gap = int(after["period_id"]) - int(before["period_id"]) - 1
        movements.append({"id": "m" + before["period_id"] + after["period_id"], "topic_id": "t1",
            "from_period": before["period_id"], "to_period": after["period_id"], "from_count": before["count"],
            "to_count": after["count"], "from_terms": before["terms"], "to_terms": after["terms"],
            "evidence_before": before["evidence_ids"], "evidence_after": after["evidence_ids"],
            "gap_periods": gap, "status": "insufficient", "p_value": None, "q_value": None,
            "cosine_distance": None, "distance_2d": .1, "explanation": "観測間隔に欠損があります。" if gap else "表現が不足しています。"})
    snapshot = {"result_id": result["id"], "projection_id": "fixed-annual", "topics": result["topics"],
                "periods": periods, "centroids": centers, "movements": movements,
                "meta": {"scope": "full", "interval": "year", "analysis_papers": 8, "excluded_date_count": 2,
                         "excluded_date_reasons": {"invalid_year": 2}},
                "map": {"projection": {"requested_method": "pca"}}, "warnings": ["年度別の取得範囲は未検証です。"]}
    calls = []
    def build(result_id, projection="auto", interval="year", scope="sample"):
        calls.append((result_id, projection, interval, scope))
        assert result_id == result["id"] and interval == "year"
        layout = deepcopy(snapshot)
        layout["meta"].update(scope=scope)
        return layout
    monkeypatch.setattr(landscape, "build_landscape", build)
    return result, snapshot, calls


def prepare(saved, **options):
    return reports.prepare_report(saved[0]["id"], "pca", options.pop("scope", "full"), "t1", 2020, 2025,
                                  projection_id="fixed-annual", **options)


def body(saved, **options):
    return {"result_id": saved[0]["id"], "projection": "pca", "projection_id": "fixed-annual", "interval": "year",
            "scope": "full", "topic_id": "t1", "start_year": 2020, "end_year": 2025, "provider": "none", **options}


def wait_job(client, identifier):
    for _ in range(300):
        job = client.get(f"/api/jobs/{identifier}").json()
        if job["status"] in {"completed", "failed"}:
            return job
        time.sleep(.01)
    pytest.fail(f"Annual job did not finish: {job}")


@pytest.mark.parametrize("scope", ["sample", "full"])
def test_one_fixed_snapshot_includes_empty_years_exact_metrics_and_existing_transitions(saved, scope):
    before = deepcopy(saved[1])
    report = prepare(saved, scope=scope, include_transitions=True)
    assert len(saved[2]) == 1
    assert saved[1] == before
    assert [row["year"] for row in report["years"]] == list(range(2020, 2026))
    assert [row["count"] for row in report["annual_rows"]] == [2, 0, 0, 1, 1, 1]
    assert [row["period_count"] for row in report["annual_rows"]] == [2, 1, 0, 1, 1, 3]
    assert report["years"][1]["share_of_period"] == 0
    assert report["years"][1]["coverage_status"] == "topic_zero"
    assert report["years"][2]["share_of_period"] is None
    assert report["years"][2]["coverage_status"] == "no_corpus_observation"
    assert report["years"][4]["count"] == 1 and report["years"][4]["report"] is None
    assert report["years"][4]["generation_error_kind"] == "missing_evidence"
    assert report["meta"]["excluded_date_reasons"] == {"invalid_year": 2}
    assert report["meta"]["is_demo"] is True
    assert [row["report"]["movement"] for row in report["transitions"]] == saved[1]["movements"]
    assert report["transitions"][0]["gap_periods"] == 2
    assert report["transitions"][0]["gap_note"]
    assert all(row["report"]["movement"]["p_value"] is None for row in report["transitions"])
    assert report["overview"]["mode"] == "deterministic"
    assert report["overview"]["count_change"] == -1
    assert report["overview"]["share_change"] == pytest.approx(1 / 3 - 1)
    assert report["overview"]["terms_entered"] == ["sensor"]
    assert report["overview"]["terms_left"] == ["steel"]
    assert report["overview"]["terms_shared"] == ["laser"]
    assert report["overview"]["gap_years"] == [2022]
    assert report["progress"]["completed"] == report["progress"]["total"] == 9
    assert report["progress"]["llm_calls"] == report["progress"]["planned_llm_calls"] == 0
    assert all(row["report"]["narrative"]["mode"] == "deterministic" for row in report["years"] if row["report"])


def test_default_has_no_transition_calls_and_zero_only_years_have_no_model_work(saved, monkeypatch):
    monkeypatch.setattr(landscape_reports, "generate", lambda *a, **k: pytest.fail("No LLM call"))
    report = prepare(saved)
    reports.generate_children(report)
    assert report["transitions"] == []
    zero = reports.prepare_report(saved[0]["id"], "pca", "full", "t1", 2021, 2022, provider="local")
    reports.generate_children(zero)
    assert zero["progress"]["planned_llm_calls"] == zero["progress"]["llm_calls"] == 0
    assert zero["generation_status"] == "not_generated"
    assert zero["overview"]["count_change"] is None and zero["overview"]["share_change"] is None


@pytest.mark.parametrize("topic,start,end", [("all", 2020, 2025), ("absent", 2020, 2025),
    ("unclassified", 2020, 2025), ("t1", 2019, 2025), ("t1", 2020, 2027), ("t1", 1900, 2025)])
def test_selection_bounds_real_topic_and_existing_year_limit(saved, topic, start, end):
    with pytest.raises(ValueError):
        reports.prepare_report(saved[0]["id"], "pca", "full", topic, start, end)


def test_wrong_projection_id_and_snapshot_ownership_are_rejected(saved, monkeypatch):
    with pytest.raises(ValueError, match="版"):
        reports.prepare_report(saved[0]["id"], "pca", "full", "t1", 2020, 2025, projection_id="different")
    for key, value in (("result_id", "different"), ("interval", "month"), ("scope", "sample")):
        invalid = deepcopy(saved[1])
        (invalid if key == "result_id" else invalid["meta"])[key] = value
        monkeypatch.setattr(landscape, "build_landscape", lambda *a, **k: invalid)
        with pytest.raises(ValueError, match="一致"):
            reports.prepare_report(saved[0]["id"], "pca", "full", "t1", 2020, 2025)


def test_one_failed_child_and_missing_abstract_keep_other_narratives_and_metrics(saved, monkeypatch):
    report = prepare(saved, provider="local")
    metrics = deepcopy(report["annual_rows"])
    assert report["years"][3]["generation_status"] == "skipped"
    assert report["years"][3]["generation_error_kind"] == "missing_abstracts"
    calls, snapshots = [], []
    def generate(child, provider, model, **kwargs):
        calls.append(child["centroid"]["period_id"])
        if calls[-1] == "2020":
            raise LocalStreamError("private-secret", kind="token_limit")
        return {"mode": "local_llm", "model": "fixture", "headline": "読み取った内容", "sections": [
            {"title": "観測", "text": "誤った数値999を含む本文も警告付きで保存。", "evidence_ids": ["p25"]}],
            "caveats": ["原文を確認"], "validation": {"status": "warning", "warnings": [{"code": "numeric_mismatch", "message": "照合警告"}]}}
    monkeypatch.setattr(landscape_reports, "generate", generate)
    reports.generate_children(report, on_save=lambda value: snapshots.append(deepcopy(value)))
    assert calls == ["2020", "2025"]
    assert report["generation_status"] == "partial"
    assert report["years"][0]["generation_status"] == "failed"
    assert report["years"][-1]["generation_status"] == "generated"
    assert report["years"][-1]["report"]["narrative"]["validation"]["status"] == "warning"
    assert report["annual_rows"] == metrics
    assert report["progress"]["llm_calls"] == report["progress"]["planned_llm_calls"] == 2
    assert report["progress"]["completed"] == report["progress"]["total"] == 6
    assert "private-secret" not in json.dumps(report)
    assert any(snapshot["years"][0]["generation_status"] == "failed" and snapshot["years"][-1]["generation_status"] == "pending" for snapshot in snapshots)


def test_existing_generators_use_the_same_instructions_and_preserve_warning_prose(saved, monkeypatch):
    seen = []
    def output(payload, schema, instructions, provider, model, **kwargs):
        seen.append((payload, instructions))
        assert kwargs["allow_text"] is True
        return "受信した分析本文です。", "local_llm", "fixture"
    monkeypatch.setattr(field_llm, "structured_output", output)
    report = prepare(saved, provider="local", include_transitions=True)
    reports.generate_children(report)
    assert len(seen) == report["progress"]["llm_calls"] == 4
    for payload, instructions in seen:
        expected = centroid_reports.INSTRUCTIONS if payload.get("kind") == "centroid" else landscape_reports.INSTRUCTIONS.replace(
            "complete DISPLAY SAMPLE", "specified analysis scope (sample or full corpus)")
        assert instructions == expected
    assert report["years"][-1]["report"]["narrative"]["sections"][0]["text"] == "受信した分析本文です。"
    assert report["years"][-1]["report"]["narrative"]["validation"]["status"] == "warning"


def test_csv_preserves_metrics_nulls_actual_prose_warnings_excerpts_transitions_and_gaps(saved, monkeypatch):
    report = prepare(saved, provider="local", include_transitions=True)
    monkeypatch.setattr(field_llm, "structured_output", lambda *a, **k: ("=受信した本文", "local_llm", "fixture"))
    reports.generate_children(report)
    text = reports.export_csv(report)
    assert text.startswith("\ufeff")
    rows = list(csv.DictReader(io.StringIO(text.lstrip("\ufeff"))))
    null_share = next(row for row in rows if row["Section"] == "annual_metrics" and row["Year"] == "2022" and row["Metric / JSON Pointer"] == "share_of_period")
    assert null_share["Value"] == "" and null_share["Value type"] == "null"
    assert any(row["Value"] == "'=受信した本文" for row in rows)
    assert "Laser heating improved" in text and "output_format_recovered" in text
    assert "/transitions/0/report/movement/gap_periods" in text
    assert "/overview/gap_years/0" in text and "/years/3/llm_error" in text
    assert "no_corpus_observation" in text and "topic_zero" in text


def test_api_persists_initial_partial_and_final_with_request_context_without_secrets(saved, monkeypatch):
    started, release = threading.Event(), threading.Event()
    seen = []
    def generate(child, provider, model, **kwargs):
        seen.append((current_settings().local.api_key.get_secret_value(), child["centroid"]["period_id"]))
        if len(seen) == 1:
            started.set()
            assert release.wait(5)
        return {**deepcopy(child["narrative"]), "mode": "local_llm", "model": model}
    monkeypatch.setattr(landscape_reports, "generate", generate)
    settings = {"version": 1, "local": {"backend": "openai_compatible", "url": "http://127.0.0.1:1234/v1", "api_key": "context-secret"}}
    header = base64.b64encode(json.dumps(settings).encode()).decode()
    with TestClient(app, headers={"x-atlas-connection": header}) as client:
        response = client.post("/api/landscape-annual-reports", json=body(saved, provider="local", model="fixture"))
        assert response.status_code == 200, response.text
        ids = response.json()
        assert set(ids) == {"job_id", "annual_report_id"}
        try:
            assert started.wait(5)
            partial = client.get("/api/landscape-annual-reports/" + ids["annual_report_id"]).json()
            assert partial["topic"]["id"] == "t1" and partial["start_year"] == 2020
            assert partial["generation_status"] == "generating"
            assert partial["years"][0]["generation_status"] == "generating"
            assert "context-secret" not in json.dumps(partial)
        finally:
            release.set()
        job = wait_job(client, ids["job_id"])
        assert job["status"] == "completed" and job["generation_status"] == "partial"
        final = client.get("/api/landscape-annual-reports/" + ids["annual_report_id"]).json()
        assert final["years"][-1]["generation_status"] == "generated"
        assert "context-secret" not in json.dumps(job) + json.dumps(final)
        assert [secret for secret, _ in seen] == ["context-secret", "context-secret"]
        exported = client.get(f"/api/landscape-annual-reports/{ids['annual_report_id']}/export?format=json")
        assert exported.json() == final
        assert client.get(f"/api/landscape-annual-reports/{ids['annual_report_id']}/export").text.startswith("\ufeff")


def test_cancel_waits_for_current_generation_then_preserves_it_and_skips_next(saved, monkeypatch):
    started, release = threading.Event(), threading.Event()
    calls = []
    def generate(child, *args, **kwargs):
        calls.append(child["centroid"]["period_id"])
        started.set()
        assert release.wait(5)
        return {**deepcopy(child["narrative"]), "mode": "local_llm"}
    monkeypatch.setattr(landscape_reports, "generate", generate)
    with TestClient(app) as client:
        ids = client.post("/api/landscape-annual-reports", json=body(saved, provider="local")).json()
        try:
            assert started.wait(5)
            stopped = client.post(f"/api/landscape-annual-reports/{ids['annual_report_id']}/cancel").json()
            assert stopped["cancel_requested"] is True
            assert stopped["report"]["generation_status"] == "generating"
        finally:
            release.set()
        job = wait_job(client, ids["job_id"])
        report = client.get(f"/api/landscape-annual-reports/{ids['annual_report_id']}").json()
        assert calls == ["2020"]
        assert job["generation_status"] == report["generation_status"] == "cancelled"
        assert report["years"][0]["generation_status"] == "generated"
        assert report["years"][-1]["generation_status"] == "cancelled"
        assert report["progress"]["llm_calls"] == 1 and report["progress"]["planned_llm_calls"] == 2
        assert ids["annual_report_id"] not in api._CANCELLATIONS
        assert client.post(f"/api/landscape-annual-reports/{ids['annual_report_id']}/cancel").json()["cancel_requested"] is False


def test_cancel_queued_report_preserves_shell_and_never_builds(saved, monkeypatch):
    import app.main as main
    blocked, release = threading.Event(), threading.Event()
    with ThreadPoolExecutor(max_workers=1) as executor:
        monkeypatch.setattr(main, "REPORT_EXECUTOR", executor)
        executor.submit(lambda: (blocked.set(), release.wait(5)))
        assert blocked.wait(2)
        with TestClient(app) as client:
            try:
                ids = client.post("/api/landscape-annual-reports", json=body(saved, provider="local")).json()
                shell = client.get(f"/api/landscape-annual-reports/{ids['annual_report_id']}").json()
                assert shell["generation_status"] == "preparing" and shell["annual_rows"] == []
                assert shell["topic"]["id"] == "t1" and shell["scope"] == "full"
                assert client.post(f"/api/landscape-annual-reports/{ids['annual_report_id']}/cancel").json()["cancel_requested"]
            finally:
                release.set()
            job = wait_job(client, ids["job_id"])
            assert job["generation_status"] == "cancelled"
            assert saved[2] == []
            report = client.get(f"/api/landscape-annual-reports/{ids['annual_report_id']}").json()
            assert report["id"] == shell["id"] and report["created_at"] == shell["created_at"]


@pytest.mark.parametrize("patch", [{"interval": "month"}, {"topic_id": "all"}, {"start_year": True},
    {"start_year": 2020.5}, {"start_year": 2025, "end_year": 2020}, {"end_year": 9999},
    {"start_year": 1900}, {"connection_secret": "private"}])
def test_api_rejects_invalid_requests_without_scheduling(saved, patch):
    with TestClient(app) as client:
        assert client.post("/api/landscape-annual-reports", json=body(saved, **patch)).status_code == 422
    assert saved[2] == []


def test_report_namespace_and_result_ownership_are_not_interchangeable(saved):
    other = {"id": storage.new_id(), "kind": "movement", "result_id": saved[0]["id"]}
    storage.save("landscape_reports", other)
    with TestClient(app) as client:
        for suffix in ("", "/export"):
            assert client.get(f"/api/landscape-annual-reports/{other['id']}{suffix}").status_code == 404
        assert client.post(f"/api/landscape-annual-reports/{other['id']}/cancel").status_code == 404
        ids = client.post("/api/landscape-annual-reports", json=body(saved)).json()
        assert wait_job(client, ids["job_id"])["status"] == "completed"
        report = client.get(f"/api/landscape-annual-reports/{ids['annual_report_id']}").json()
        assert report["result_id"] == saved[0]["id"]
        assert all(row["report"]["result_id"] == report["result_id"] for row in report["years"] if row["report"])


def test_all_attempts_failed_is_not_partial_but_keeps_readable_metrics(saved, monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("private model error and secret")
    monkeypatch.setattr(landscape_reports, "generate", fail)
    with TestClient(app) as client:
        ids = client.post("/api/landscape-annual-reports", json=body(saved, provider="local")).json()
        job = wait_job(client, ids["job_id"])
        report = client.get(f"/api/landscape-annual-reports/{ids['annual_report_id']}").json()
        assert job["status"] == "completed" and "生成に失敗" in job["stage"]
        assert job["generation_status"] == report["generation_status"] == "failed"
        assert report["progress"]["llm_calls"] == 2
        assert report["annual_rows"][0]["count"] == 2
        assert report["years"][0]["report"]["narrative"]["mode"] == "deterministic"
        assert "private model" not in json.dumps(report)


def test_all_missing_abstracts_or_evidence_are_not_generated_and_never_call_llm(saved, monkeypatch):
    monkeypatch.setattr(landscape_reports, "generate", lambda *a, **k: pytest.fail("No usable abstract"))
    report = reports.prepare_report(saved[0]["id"], "pca", "full", "t1", 2023, 2024, provider="local")
    reports.generate_children(report)
    assert report["generation_status"] == "not_generated"
    assert report["progress"]["llm_calls"] == report["progress"]["planned_llm_calls"] == 0
    assert [row["count"] for row in report["annual_rows"]] == [1, 1]
    assert report["years"][0]["report"] is not None
    assert report["years"][0]["generation_error_kind"] == "missing_abstracts"
    assert report["years"][1]["generation_error_kind"] == "missing_evidence"


def test_single_year_success_is_generated_without_inventing_endpoint_change(saved, monkeypatch):
    monkeypatch.setattr(landscape_reports, "generate", lambda child, *a, **k: {**child["narrative"], "mode": "local_llm"})
    report = reports.prepare_report(saved[0]["id"], "pca", "full", "t1", 2025, 2025, provider="local", include_transitions=True)
    reports.generate_children(report)
    assert report["generation_status"] == "generated" and report["transitions"] == []
    assert report["progress"]["llm_calls"] == report["progress"]["planned_llm_calls"] == 1
    assert report["overview"]["count_change"] is None and report["overview"]["term_comparison_available"] is False
