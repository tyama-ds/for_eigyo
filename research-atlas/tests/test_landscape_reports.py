import base64
from copy import deepcopy
import json
import time

import pytest
from fastapi.testclient import TestClient

from app import connection_settings, field_llm, landscape, landscape_reports, large_storage, local_llm_stream, storage
from app.main import app


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    monkeypatch.setenv("ATLAS_DATA_DIR", str(tmp_path))
    result = {"id": storage.new_id(), "topics": [{"id": "steel", "label": "Steel"}],
              "papers": [{"id": f"paper-{i}", "title": "Steel treatment", "year": 2023 if i < 8 else 2024,
                          "abstract": "Strength reached 900 MPa using laser heating."} for i in range(16)], "meta": {}}
    storage.save("results", result)
    movement = {"id": "move", "topic_id": "steel", "topic_label": "Steel", "from_period": "2023", "to_period": "2024",
                "from_count": 8, "to_count": 8, "status": "shift", "distance_2d": .75, "cosine_distance": .25,
                "p_value": .015, "q_value": .03, "gap_periods": 0, "count_scope": "display_sample",
                "from_terms": [{"term": "laser", "count": 5}], "to_terms": [{"term": "welding", "count": 6}],
                "evidence_before": [f"paper-{i}" for i in range(8)], "evidence_after": [f"paper-{i}" for i in range(8, 16)],
                "from": {"x": 0, "y": 0}, "to": {"x": .75, "y": 0}, "explanation": "表示集合内での変化です。"}
    layout = {"result_id": result["id"], "projection_id": "projection-test", "topics": result["topics"],
              "movements": [movement], "meta": {"displayed_papers": 16, "excluded_date_count": 2},
              "warnings": ["日付精度を確認してください。"], "interpretation": {"limitations": []}}
    calls = []
    def build(identifier, *, projection="auto", interval="year"):
        calls.append((identifier, projection, interval))
        assert identifier == result["id"]
        return deepcopy(layout)
    monkeypatch.setattr(landscape, "build_landscape", build)
    return result, layout, calls


def report_for(fixture):
    return landscape_reports.prepare_report(fixture[0]["id"], "pca", "year", "move")


def output(text="レーザー加熱の記述があり、比較原文で用途の差を調べる必要があります。", ids=None):
    return {"headline": "話題の構成変化", "sections": [{"title": "内容の比較", "text": text,
            "evidence_ids": ["paper-0", "paper-8"] if ids is None else ids}], "caveats": ["仮説です。因果関係は未確認です。"]}


def test_bounded_exact_evidence_lookup_and_retrospective_interpretation(fixture, monkeypatch):
    original_read, original_lookup = storage.read, large_storage.papers_by_ids
    requests, reads = [], []
    def read(kind, identifier, *, include_papers=True):
        reads.append(include_papers)
        return original_read(kind, identifier, include_papers=include_papers)
    def lookup(result, root, identifiers):
        requests.extend(identifiers)
        return original_lookup(result, root, identifiers)
    monkeypatch.setattr(storage, "read", read)
    monkeypatch.setattr(large_storage, "papers_by_ids", lookup)
    report = report_for(fixture)
    assert reads == [False]
    assert len(requests) == len(report["evidence_papers"]) == 12
    assert [p["side"] for p in report["evidence_papers"]].count("before") == 6
    assert fixture[2] == [(fixture[0]["id"], "pca", "year")]
    assert report["projection_id"] == "projection-test"
    prose = json.dumps(report["narrative"], ensure_ascii=False)
    for phrase in ("高次元", "最大400", "将来予測の検証ではありません", "構成", "因果", "除外された論文が2件"):
        assert phrase in prose


def test_stable_means_not_detected_not_proven_unchanged(fixture):
    fixture[1]["movements"][0]["status"] = "stable"
    report = report_for(fixture)
    assert "変化がないことの証明ではありません" in report["observations"][0]["text"]


def test_rejects_wrong_movement_projection_and_missing_source_ids(fixture):
    with pytest.raises(ValueError, match="対象の重心移動"):
        landscape_reports.prepare_report(fixture[0]["id"], "pca", "year", "other-result-movement")
    with pytest.raises(ValueError, match="座標の版"):
        landscape_reports.prepare_report(fixture[0]["id"], "pca", "year", "move", "stale-projection")
    fixture[1]["movements"][0]["evidence_before"] = ["foreign-paper"]
    with pytest.raises(ValueError, match="根拠ID"):
        report_for(fixture)


@pytest.mark.parametrize("provider", ["local", "openai"])
def test_optional_llm_uses_selected_evidence_and_preserves_metrics(fixture, monkeypatch, provider):
    report = report_for(fixture)
    before = deepcopy(report)
    def structured(payload, schema, instructions, actual_provider, model, *, progress=None):
        assert actual_provider == provider and model == "test-model"
        assert len(payload["papers"]) == 12
        assert "untrusted DATA" in instructions and "2D projection" in instructions
        assert schema == field_llm.NarrativeOutput
        return output(), provider, "test-model"
    monkeypatch.setattr(field_llm, "structured_output", structured)
    narrative = landscape_reports.generate(report, provider, "test-model")
    assert report == before
    assert narrative["mode"] == provider
    assert narrative["validation"]["status"] == "passed"
    assert narrative["sections"][0]["evidence_ids"] == ["paper-0", "paper-8"]


def test_numeric_and_unit_mismatch_keeps_original_commentary_with_alert(fixture):
    payload = landscape_reports.evidence_payload(report_for(fixture))
    text = "強度は900 GPa、改善率は87%です。"
    narrative = landscape_reports.validate_narrative(output(text), payload, "local_llm", "model")
    assert narrative["sections"][0]["text"] == text
    assert narrative["validation"]["status"] == "warning"
    warning = narrative["sections"][0]["validation"]["warnings"][0]
    assert warning["code"] == "numeric_mismatch"
    assert {"value": "900", "unit": "GPa"} in warning["unmatched_quantities"]
    assert "87%" in warning["unmatched_numbers"]


def test_numbers_must_match_cited_papers_not_other_evidence(fixture):
    payload = landscape_reports.evidence_payload(report_for(fixture))
    payload["papers"][0]["abstract"] = "Strength reached 123 MPa."
    narrative = landscape_reports.validate_narrative(output("強度は123 MPaです。", ["paper-8"]), payload, "local", "model")
    assert narrative["validation"]["status"] == "warning"


def test_unknown_ids_rejected_and_no_abstract_avoids_llm(fixture, monkeypatch):
    report = report_for(fixture)
    with pytest.raises(RuntimeError, match="論文ID"):
        landscape_reports.validate_narrative(output(ids=["paper-from-another-result"]), landscape_reports.evidence_payload(report), "local", "model")
    monkeypatch.setattr(field_llm, "structured_output", lambda *args, **kwargs: pytest.fail("Unexpected model call"))
    for paper in report["evidence_papers"]:
        paper["abstract"] = ""
    assert landscape_reports.generate(report, "none")["mode"] == "deterministic"
    with pytest.raises(landscape_reports.NarrativeValidationError, match="抄録がありません") as missing:
        landscape_reports.generate(report, "local")
    assert missing.value.kind == "missing_abstracts"


@pytest.mark.parametrize("ids", [[], ["paper-0"]])
def test_missing_period_citations_retain_text_but_warn(fixture, ids):
    payload = landscape_reports.evidence_payload(report_for(fixture))
    value = output(ids=ids)
    narrative = landscape_reports.validate_narrative(value, payload, "local", "model")
    assert narrative["sections"][0]["text"] == value["sections"][0]["text"]
    assert narrative["validation"]["status"] == "warning"
    assert "period_evidence_missing" in {warning["code"] for warning in narrative["validation"]["warnings"]}


def wait_job(client, result_id, **options):
    response = client.post("/api/landscape-reports", json={"result_id": result_id, "movement_id": "move", **options})
    assert response.status_code == 200, response.text
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        job = client.get("/api/jobs/" + response.json()["job_id"]).json()
        if job["status"] in {"completed", "failed"}:
            return job
        time.sleep(.01)
    pytest.fail("Landscape narrative did not finish")


def test_api_report_persistence_csv_and_invalid_requests(fixture):
    with TestClient(app) as client:
        job = wait_job(client, fixture[0]["id"])
        assert job["status"] == "completed", job
        report = client.get("/api/landscape-reports/" + job["landscape_report_id"]).json()
        assert report["narrative"]["mode"] == "deterministic"
        assert report["projection_id"] == job["projection_id"]
        exported = client.get(f"/api/landscape-reports/{report['id']}/export")
        assert exported.content.startswith(b"\xef\xbb\xbf") and "cosine_distance" in exported.text
        assert report == storage.read("landscape_reports", report["id"])
        for change in ({"provider": "unexpected"}, {"interval": "day"}, {"projection": "xyz"}, {"url": "http://secret"}):
            assert client.post("/api/landscape-reports", json={"result_id": fixture[0]["id"], "movement_id": "move", **change}).status_code == 422
        assert client.get("/api/landscape-reports/" + "f" * 32).status_code == 404
        assert client.post("/api/landscape-reports", json={"result_id": fixture[0]["id"], "movement_id": "move"}, headers={"Origin": "https://foreign.test"}).status_code == 403
        invalid = wait_job(client, fixture[0]["id"], movement_id="foreign")
        assert invalid["status"] == "failed"


def test_api_failure_keeps_deterministic_and_does_not_leak_exception_secrets(fixture, monkeypatch):
    def failed(*args, **kwargs):
        raise RuntimeError("http://private-url.test/path?api_key=very-secret")
    monkeypatch.setattr(field_llm, "structured_output", failed)
    with TestClient(app) as client:
        job = wait_job(client, fixture[0]["id"], provider="local")
        assert job["status"] == "completed" and job["validation_status"] == "warning"
        report = client.get("/api/landscape-reports/" + job["landscape_report_id"]).json()
    assert report["narrative"]["mode"] == "deterministic" and report["llm_error"]
    assert report["generation_status"] == job["generation_status"] == "failed"
    assert report["requested_provider"] == "local" and report["generation_error_kind"] == "generation_failed"
    assert "LLM評論の生成に失敗" in job["stage"]
    assert report["movement"]["cosine_distance"] == .25
    assert "private-url" not in json.dumps(report) and "very-secret" not in json.dumps(job)


@pytest.mark.parametrize("kind,expected", [
    ("reasoning_incomplete", "思考部分（<think>）が閉じられず"),
    ("missing_final_answer", "思考部分だけ"),
    ("token_limit", "トークン上限"),
    ("malformed_json", "JSONとして読み取れません"),
    ("incomplete", "完了前に途切れました"),
    ("read_timeout", "受信が長時間停止"),
])
def test_api_stream_failure_is_distinct_from_numeric_warning(fixture, monkeypatch, kind, expected):
    def failed(*args, **kwargs):
        raise local_llm_stream.LocalStreamError("private exception with key=secret", kind=kind)
    monkeypatch.setattr(field_llm, "structured_output", failed)
    with TestClient(app) as client:
        job = wait_job(client, fixture[0]["id"], provider="local")
        report = client.get("/api/landscape-reports/" + job["landscape_report_id"]).json()
    assert job["status"] == "completed" and job["generation_status"] == "failed"
    assert report["generation_status"] == "failed" and report["generation_error_kind"] == kind
    assert expected in report["llm_error"]
    assert report["narrative"]["mode"] == "deterministic"
    assert report["movement"]["cosine_distance"] == .25
    assert "key=secret" not in json.dumps(report)


@pytest.mark.parametrize("kind,expected", [
    ("invalid_schema", "形式を満たしていません"),
    ("invalid_evidence_ids", "根拠資料にない論文ID"),
    ("missing_abstracts", "抄録がなく"),
])
def test_api_validation_failure_reports_fixed_specific_reason(fixture, monkeypatch, kind, expected):
    def failed(*args, **kwargs):
        raise landscape_reports.NarrativeValidationError("private validation text", kind=kind)
    monkeypatch.setattr(landscape_reports, "generate", failed)
    with TestClient(app) as client:
        job = wait_job(client, fixture[0]["id"], provider="local")
        report = client.get("/api/landscape-reports/" + job["landscape_report_id"]).json()
    assert report["generation_error_kind"] == kind and expected in report["llm_error"]
    assert "private validation text" not in json.dumps(report)


def test_api_numeric_warning_keeps_generated_critique_and_success_status(fixture, monkeypatch):
    text = "強度は900 GPa、改善率は87%です。"
    monkeypatch.setattr(field_llm, "structured_output", lambda *args, **kwargs: (output(text), "local_llm", "test"))
    with TestClient(app) as client:
        job = wait_job(client, fixture[0]["id"], provider="local")
        report = client.get("/api/landscape-reports/" + job["landscape_report_id"]).json()
    assert job["status"] == "completed" and job["generation_status"] == "generated"
    assert job["validation_status"] == "warning" and "失敗" not in job["stage"]
    assert report["generation_status"] == "generated" and "llm_error" not in report
    assert report["narrative"]["sections"][0]["text"] == text
    assert any(w["code"] == "numeric_mismatch" for w in report["narrative"]["validation"]["warnings"])


def test_api_worker_receives_browser_context_without_persisting_connections(fixture, monkeypatch, tmp_path):
    seen = []
    def structured(payload, schema, instructions, provider, model, *, progress=None):
        settings = connection_settings.current_settings()
        seen.append((settings.local.url, settings.local.api_key.get_secret_value(), settings.proxy.password.get_secret_value()))
        progress({"elapsed_seconds": 67, "received_chars": 500})
        return output(), "local_llm", "model"
    monkeypatch.setattr(field_llm, "structured_output", structured)
    settings = {"local": {"url": "http://192.168.0.8:1234/v1", "api_key": "llm-key"},
                "proxy": {"url": "http://proxy.test:8080", "username": "proxy-user", "password": "proxy-password"}}
    header = base64.b64encode(json.dumps(settings).encode()).decode()
    with TestClient(app, headers={"x-atlas-connection": header}) as client:
        job = wait_job(client, fixture[0]["id"], provider="local")
        report = client.get("/api/landscape-reports/" + job["landscape_report_id"]).json()
    assert job["status"] == "completed"
    assert seen == [("http://192.168.0.8:1234/v1", "llm-key", "proxy-password")]
    persisted = json.dumps(report) + json.dumps(job) + "".join(path.read_text(encoding="utf-8") for path in (tmp_path / "landscape_reports").glob("*.json"))
    for secret in ("192.168.0.8", "llm-key", "proxy-password", "proxy.test"):
        assert secret not in persisted


def test_csv_sanitizes_generated_formulas(fixture):
    report = report_for(fixture)
    report["narrative"]["sections"][0]["text"] = "=HYPERLINK(unsafe)"
    assert "'=HYPERLINK(unsafe)" in landscape_reports.export_csv(report)
