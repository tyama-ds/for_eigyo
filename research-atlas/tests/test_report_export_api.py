"""Report export contracts: four containers, saved scope and no regeneration."""
from copy import deepcopy
import base64
from html import unescape
from io import BytesIO
import json
from zipfile import ZipFile

from fastapi.testclient import TestClient
from PIL import Image
import pytest

from app import report_bundle, report_documents, report_export_api as exports, report_figures, storage
from app.main import app
from test_corpus_reporting import ledger, finish, paper, result


def png():
    output = BytesIO()
    Image.new("RGB", (120, 80), "teal").save(output, "PNG")
    return output.getvalue()


@pytest.fixture
def saved(tmp_path, monkeypatch):
    monkeypatch.setenv("ATLAS_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(report_documents, "_font", lambda: "Helvetica")
    monkeypatch.setattr(report_figures, "build_report_figures", lambda *args, **kwargs: [
        {"title": "共有座標の時相展開", "caption": "表示標本・年別。赤矢印は構成の変化。", "png": png(), "width": 120, "height": 80}])
    value = {"id": "a" * 32, "dataset_name": "Saved analysis", "meta": {"is_demo": True},
             "papers": [{"id": "p1", "title": "=Dangerous()", "year": 2024, "topic_id": "t1", "abstract": "Saved abstract"}],
             "topics": [{"id": "t1", "label": "材料", "count": 1}], "timeline": [{"year": 2024, "papers": 1}]}
    storage.save("results", value)
    narrative = {"headline": "Saved review", "mode": "local", "sections": [{"title": "論文内容", "text": "KEEP_SAVED_PROSE", "evidence_ids": ["p1"]}],
                 "validation": {"status": "warning", "warnings": [{"code": "numeric_mismatch", "message": "数値照合に注意", "unmatched_numbers": ["1200"]}]}}
    common = {"id": "b" * 32, "result_id": value["id"], "created_at": "2026-10-09", "narrative": narrative,
              "evidence_papers": value["papers"], "topic": {"id": "t1", "label": "材料"}, "generation_status": "generated"}
    reports = {
        "landscape": {**common, "kind": "movement", "scope": "full", "projection_id": "pca-one", "movement": {"from_period": "2023", "to_period": "2024", "from_count": 2, "to_count": 1, "cosine_distance": .25}},
        "field": {**common, "focus": {"id": "t1", "label": "材料"}, "neighbor": None, "annual": [{"year": 2024, "focus_count": 1, "neighbor_count": None}]},
        "foresight": {**common, "papers": value["papers"], "candidates": [{"id": "c1", "label": "材料", "paper_ids": ["p1"], "narrative": narrative, "growth": {"growth_pct": None, "series": [{"year": 2024, "count": 1}]}, "readiness": {"stage_label": "未判定"}}]},
    }
    reports["annual"] = {**common, "kind": "annual", "scope": "full", "years": [{"year": 2024, "report": reports["landscape"]}],
        "annual_rows": [{"year": 2024, "count": 1, "period_count": 4, "share_of_period": .25}]}
    for kind, report in reports.items():
        storage.save(exports.STORES[kind], report)
    return value, reports


@pytest.mark.parametrize("kind", ["result", "landscape", "annual", "field", "foresight"])
@pytest.mark.parametrize("format", ["pdf", "docx", "xlsx", "pptx"])
def test_all_report_types_generate_readable_containers_with_figures(saved, kind, format):
    value, reports = saved
    identifier = value["id"] if kind == "result" else reports[kind]["id"]
    response = TestClient(app).post(f"/api/report-exports/{kind}/{identifier}", json={"format": format})
    assert response.status_code == 200, response.text[:100] if response.status_code != 200 else ""
    assert response.headers["content-type"].startswith(exports.MIME[format])
    assert response.headers["content-disposition"].endswith(f'.{format}"')
    if format == "pdf":
        assert response.content.startswith(b"%PDF-") and b"/Subtype /Image" in response.content
    else:
        with ZipFile(BytesIO(response.content)) as archive:
            assert archive.testzip() is None
            names = archive.namelist()
            assert any("/media/" in name and name.endswith(".png") for name in names)
            content = unescape("".join(archive.read(name).decode("utf-8") for name in names if name.endswith(".xml")))
            if kind != "result":
                assert "KEEP_SAVED_PROSE" in content
                assert "数値照合に注意" in content and "1200" in content


def test_snapshot_ownership_png_validation_no_remote_fetch_and_optional_figures(saved):
    value, _ = saved
    client = TestClient(app)
    route = f"/api/report-exports/result/{value['id']}"
    image = {"result_id": value["id"], "title": "Current camera", "caption": "2024 → 2025 / PCA",
             "data_url": "data:image/png;base64," + base64.b64encode(png()).decode()}
    for change in ({"result_id": "f" * 32}, {"data_url": "https://example.com/figure.png"},
                   {"data_url": "data:image/png;base64,invalid"}, {"data_url": "data:image/svg+xml;base64,anything"}):
        response = client.post(route, json={"format": "docx", "snapshots": [{**image, **change}]})
        assert response.status_code == 422
    assert client.post(route, json={"snapshots": [image] * 5}).status_code == 422
    response = client.post(route, json={"format": "docx", "snapshots": [image]})
    with ZipFile(BytesIO(response.content)) as archive:
        xml = archive.read("word/document.xml").decode()
        assert "Current camera" in xml and "2024 → 2025 / PCA" in xml
    response = client.post(route, json={"format": "xlsx", "include_figures": False})
    with ZipFile(BytesIO(response.content)) as archive:
        assert not any("/media/" in name for name in archive.namelist())


def test_bad_options_missing_results_origin_and_busy_errors_are_actionable(saved, monkeypatch):
    value, _ = saved
    client = TestClient(app)
    route = f"/api/report-exports/result/{value['id']}"
    for options in ({"format": "exe"}, {"include_figures": "true"}, {"api_key": "dont-echo-this"}):
        response = client.post(route, json=options)
        assert response.status_code == 422 and "dont-echo-this" not in response.text
    assert client.post(route, json={}, headers={"Origin": "https://example.com"}).status_code == 403
    assert client.get("/api/report-exports/result/" + "f" * 32).status_code == 404
    assert client.get(route + "?candidate_id=not-applicable").status_code == 422
    exports._RENDERS.acquire()
    exports._RENDERS.acquire()
    try:
        assert client.post(route, json={}).status_code == 409
    finally:
        exports._RENDERS.release()
        exports._RENDERS.release()
    monkeypatch.setattr(exports, "MAX_EXPORT_REQUEST", 20)
    assert client.post(route, content=b"x" * 21).status_code == 413


def test_candidate_filter_retains_selected_review_and_data_only(saved):
    value, reports = saved
    report = reports["foresight"]
    report["candidates"].append({"id": "c2", "label": "OTHER_CANDIDATE", "narrative": {"sections": [{"title": "other", "text": "DO_NOT_EXPORT"}]}})
    storage.save("assessments", report)
    route = f"/api/report-exports/foresight/{report['id']}"
    response = TestClient(app).post(route, json={"format": "xlsx", "candidate_id": "c1"})
    assert response.status_code == 200
    with ZipFile(BytesIO(response.content)) as archive:
        xml = "".join(archive.read(name).decode() for name in archive.namelist() if name.endswith(".xml"))
        assert "KEEP_SAVED_PROSE" in xml and "DO_NOT_EXPORT" not in xml and "OTHER_CANDIDATE" not in xml
    assert TestClient(app).post(route, json={"candidate_id": "absent"}).status_code == 404


def test_corpus_export_preserves_full_prose_and_all_records_without_llm(ledger, monkeypatch):
    _, state = ledger
    saved_result = result([paper("a"), paper("b", abstract=""), paper("c")])
    report = finish(exports.corpus_reporting.create_report(saved_result["id"], batch_size=1)["id"])
    calls_before = deepcopy(state["calls"])
    seen = {}
    def capture(bundle, format, records=None):
        seen.update(bundle=bundle, records=list(records))
        return b"exported"
    monkeypatch.setattr(report_documents, "render_report", capture)
    route = f"/api/report-exports/corpus/{report['id']}"
    response = TestClient(app).post(route, json={"format": "xlsx", "include_figures": False})
    assert response.status_code == 200
    assert len(seen["records"]) == 3
    assert {row["status"] for row in seen["records"]} == {"completed", "missing", "pending"}
    assert state["calls"] == calls_before and not state["syntheses"]
    assert any("未処理" in note for note in seen["bundle"]["warnings"])


def test_bundle_does_not_mutate_reports_and_preserves_late_sections(saved):
    value, reports = saved
    report = reports["annual"]
    child = deepcopy(reports["landscape"])
    child["narrative"]["sections"].append({"title": "Final section", "text": "LAST_SAVED_SECTION"})
    report["years"].append({"year": 2025, "report": child})
    before = json.dumps(report)
    bundle = report_bundle.build_bundle("annual", report, value)
    assert any("LAST_SAVED_SECTION" in row["text"] for row in bundle["sections"])
    assert json.dumps(report) == before
    assert any(row["title"].startswith("[!]") for row in bundle["sections"])
