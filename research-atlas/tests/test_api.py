import base64
import json
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import insights, reports, storage
from app.main import app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("ATLAS_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    with TestClient(app) as value:
        yield value


@pytest.fixture
def result(client):
    csv = ("EID,Title,Year,Abstract,Author Keywords,Cited by,Authors\n"
           'p1,Battery ion transport,2021,Electrolyte ion transport energy storage,battery;electrolyte,20,Alpha A.\n'
           'p2,Solid electrolyte battery,2022,Lithium electrolyte transport storage,battery;electrolyte,10,Alpha A.;Beta B.\n'
           'p3,Quantum error correction,2023,Quantum qubits error correction circuit,quantum;qubit,8,Gamma G.\n'
           'p4,Quantum logical qubits,2024,Logical quantum qubit circuit error correction,quantum;qubit,2,Gamma G.\n'
           'p5,Quantum circuit codes,2025,Quantum logical circuit correction performance,quantum;qubit,,Gamma G.\n')
    uploaded = client.post("/api/import", files={"file": ("sample.csv", csv.encode(), "text/csv")})
    assert uploaded.status_code == 200, uploaded.text
    dataset_id = uploaded.json()["dataset"]["id"]
    job = client.post("/api/analyze", json={"dataset_id": dataset_id, "start_year": 2021, "end_year": 2025, "n_topics": 2})
    assert job.status_code == 200, job.text
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        current = client.get("/api/jobs/" + job.json()["job_id"]).json()
        if current["status"] in {"failed", "completed"}:
            break
        time.sleep(0.1)
    assert current["status"] == "completed", current
    response = client.get("/api/results/" + current["result_id"])
    assert response.status_code == 200
    return response.json()


def test_import_analyze_and_exports(client, result):
    assert result["summary"]["papers"] == 5
    assert result["meta"]["is_demo"] is False
    assert all(r["annual_citations"] is None for r in result["timeline"])
    assert json.loads(json.dumps(result, allow_nan=False))["id"] == result["id"]
    for format, mime in [("csv", "text/csv"), ("json", "application/json"), ("html", "text/html")]:
        response = client.get(f"/api/results/{result['id']}/export?format={format}")
        assert response.status_code == 200
        assert mime in response.headers["content-type"]
        assert "attachment" in response.headers["content-disposition"]
        assert len(response.content) > 100
    local = client.post("/api/insights", json={"result_id": result["id"]})
    assert local.status_code == 200
    assert local.json()["mode"] == "local"
    allowed = {p["id"] for p in result["papers"]}
    assert set(local.json()["evidence_ids"]) <= allowed


def test_citation_revision_does_not_change_past_result(client, result):
    before = storage.read("datasets", result["dataset_id"])
    response = client.post(f"/api/datasets/{result['dataset_id']}/citations", files={"file": ("citations.csv", b"EID,Year,Citations\np1,2024,3\np1,2025,5\n", "text/csv")})
    assert response.status_code == 200, response.text
    revision_id = response.json()["dataset"]["id"]
    assert revision_id != result["dataset_id"]
    assert storage.read("datasets", result["dataset_id"]) == before
    assert storage.read("results", result["id"]) == result
    revised = storage.read("datasets", revision_id)
    assert revised["papers"][0]["citation_history"]["2025"] == 5


def test_bad_upload_options_and_cross_origin(client):
    assert client.post("/api/import", files={"file": ("sample.csv", b"", "text/csv")}).status_code == 422
    assert client.post("/api/import", files={"file": ("sample.exe", b"abc", "text/plain")}).status_code == 422
    assert client.post("/api/analyze", json={"dataset_id": "a" * 32, "start_year": 2025, "end_year": 2021}).status_code == 422
    assert client.post("/api/demo", headers={"Origin": "https://untrusted.example"}).status_code == 403
    assert client.get("/api/status", headers={"Host": "evil.example"}).status_code == 400
    assert client.get("/api/results/" + "f" * 32).status_code == 404
    assert client.get("/api/jobs/unknown").status_code == 404
    invalid = client.post("/api/analyze", json={"dataset_id": "a" * 32, "embedding": "unknown"})
    assert invalid.status_code == 422
    assert isinstance(invalid.json()["detail"], str)


def test_llm_is_opt_in_and_redacted(client, result, monkeypatch):
    assert not client.get("/api/status").json()["llm_configured"]
    missing = client.post("/api/insights", json={"result_id": result["id"], "use_llm": True})
    assert missing.status_code == 422
    assert "接続設定" in missing.json()["detail"]
    # Official SDK boundary mocked: no real external requests in the test suite.
    client.headers["X-Atlas-Connection"] = base64.b64encode(json.dumps({"openai": {
        "api_key": "test-secret-must-never-be-exposed", "model": "configured-model"}}).encode()).decode()
    sent = {}
    class FakeClient:
        def __init__(self, **kwargs):
            self.responses = self
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def parse(self, **kwargs):
            sent.update(kwargs)
            data = json.loads(kwargs["input"][1]["content"])
            pid = data["papers"][0]["id"]
            return SimpleNamespace(output_parsed=insights.InsightOutput(headline="検証", findings=["観測"], hypotheses=["仮説"], caveats=["不確実"], evidence_ids=[pid]))
    import openai
    monkeypatch.setattr(openai, "OpenAI", FakeClient)
    response = client.post("/api/insights", json={"result_id": result["id"], "use_llm": True})
    assert response.status_code == 200, response.text
    assert response.json()["mode"] == "llm"
    assert sent["store"] is False
    assert sent["model"] == "configured-model"
    assert "test-secret" not in response.text
    evidence = json.loads(sent["input"][1]["content"])
    assert all("authors" not in p for p in evidence["papers"])


def test_llm_rejects_unknown_evidence(client, result, monkeypatch):
    client.headers["X-Atlas-Connection"] = base64.b64encode(json.dumps({"openai": {
        "api_key": "test", "model": "configured-model"}}).encode()).decode()
    class BadClient:
        def __init__(self, **kwargs): self.responses = self
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def parse(self, **kwargs):
            return SimpleNamespace(output_parsed=insights.InsightOutput(headline="bad", findings=[], hypotheses=[], caveats=[], evidence_ids=["fabricated-paper"]))
    import openai
    monkeypatch.setattr(openai, "OpenAI", BadClient)
    response = client.post("/api/insights", json={"result_id": result["id"], "use_llm": True})
    assert response.status_code == 502
    assert "採用しませんでした" in response.json()["detail"]


def test_export_escapes_untrusted_labels(client, result):
    result["topics"][0]["label"] = '<script>alert(1)</script>'
    html = reports.report_html(result)
    assert '<script>alert(1)</script>' not in html
    assert '&lt;script&gt;' in html
    result["topics"][0]["label"] = '=HYPERLINK("https://evil.example")'
    assert "'=HYPERLINK" in reports.topic_csv(result)


def test_demo_download_is_reimportable(client):
    csv = client.get("/api/demo.csv")
    assert csv.status_code == 200
    response = client.post("/api/import", files={"file": ("synthetic.csv", csv.content, "text/csv")})
    assert response.status_code == 200, response.text
    assert response.json()["dataset"]["paper_count"] > 500
    assert response.json()["dataset"]["is_demo"] is True


def test_sdk_exception_does_not_expose_request_or_key(client, result, monkeypatch):
    client.headers["X-Atlas-Connection"] = base64.b64encode(json.dumps({"openai": {
        "api_key": "test-secret", "model": "configured-model"}}).encode()).decode()
    class FailingClient:
        def __init__(self, **kwargs):
            raise ValueError("test-secret private abstract request dump")
    import openai
    monkeypatch.setattr(openai, "OpenAI", FailingClient)
    response = client.post("/api/insights", json={"result_id": result["id"], "use_llm": True})
    assert response.status_code == 502
    assert "test-secret" not in response.text
    assert "private abstract" not in response.text


def test_evidence_payload_excludes_import_warning_text(client, result):
    result["meta"]["warnings"].append("unselected private document title")
    evidence = insights.build_evidence(result, None, "")
    assert "unselected private document title" not in json.dumps(evidence)


def test_uploaded_synthetic_does_not_replace_builtin_demo(client):
    uploaded = client.post("/api/import", files={"file": ("my-demo.csv", client.get("/api/demo.csv").content, "text/csv")}).json()["dataset"]
    builtin = client.post("/api/demo").json()["dataset"]
    assert builtin["id"] != uploaded["id"]
    assert storage.read("datasets", builtin["id"])["report"]["built_in_demo"] is True
    assert client.post("/api/demo").json()["dataset"]["id"] == builtin["id"]


def test_export_preserves_missing_citations(client, result):
    import csv
    import io
    for p in result["papers"]:
        p["citations"] = None
    rows = list(csv.DictReader(io.StringIO(reports.topic_csv(result).lstrip("\ufeff"))))
    assert all(row["Cumulative citations (known only)"] == "" for row in rows)
    assert all(row["Papers with known citation count"] == "0" for row in rows)
    assert "未取得" in reports.report_html(result)
