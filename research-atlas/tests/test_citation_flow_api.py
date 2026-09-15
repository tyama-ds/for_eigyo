import csv
import io
import json

import pytest
from fastapi.testclient import TestClient

from app import storage
from app.citation_flow_api import flow_csv
from app.main import app


@pytest.fixture
def saved(tmp_path, monkeypatch):
    monkeypatch.setenv("ATLAS_DATA_DIR", str(tmp_path))
    papers = [
        {"id": "old", "doi": "10.9999/old", "title": "=Old formula title", "year": 2022,
         "topic_id": "topic-1", "landscape_vector": [1, 0], "references": []},
        {"id": "new", "doi": "10.9999/new", "title": "New steel research", "year": 2024,
         "topic_id": "topic-1", "landscape_vector": [0, 1],
         "references": [{"doi": "10.9999/old"}], "references_status": "provided"},
    ]
    result = {"id": storage.new_id(), "meta": {"paper_count": 2, "is_demo": True,
              "landscape_representation": {"basis_scope": "full_corpus", "embedding": "nmf", "dimensions": 2}},
              "papers": papers, "topics": [{"id": "topic-1", "label": "Steel", "color": "#58e0c5"}]}
    storage.save("results", result)
    return result, tmp_path / "results" / (result["id"] + ".json")


def test_citation_map_and_exports_keep_source_result_unchanged(saved):
    result, path = saved
    before = path.read_bytes()
    url = f"/api/results/{result['id']}/citation-flow"
    with TestClient(app) as client:
        response = client.get(url)
        assert response.status_code == 200, response.text
        assert response.headers["cache-control"] == "no-store"
        value = response.json()
        assert any(e["source_id"] == "new" and e["target_id"] == "old" for e in value["edges"])
        assert value["result_id"] == result["id"]
        assert client.get(url + "?topic_id=topic-1").status_code == 200
        exported = client.get(url + "/export?format=json")
        assert exported.json() == value
        assert "attachment" in exported.headers["content-disposition"]
        flat = client.get(url + "/export?format=csv")
        assert flat.content.startswith(b"\xef\xbb\xbf")
        rows = list(csv.reader(io.StringIO(flat.text.lstrip("\ufeff"))))
        assert rows[0] == ["Analysis ID", "Citation flow ID", "JSON Pointer", "Value type", "Value"]
        assert any(row[2].endswith("/source_id") and row[-1] == "new" for row in rows[1:])
        assert any(row[-1] == "'=Old formula title" for row in rows[1:])
    assert path.read_bytes() == before


def test_invalid_citation_view_selectors_and_missing_result_are_errors(saved):
    url = f"/api/results/{saved[0]['id']}/citation-flow"
    with TestClient(app) as client:
        for suffix in ("?interval=day", "?topic_id=", "?topic_id=" + "x" * 121,
                       "?topic_id=not-a-topic", "/export?format=html"):
            assert client.get(url + suffix).status_code == 422
        assert client.get("/api/results/" + "0" * 32 + "/citation-flow").status_code == 404


def test_csv_preserves_missing_zero_empty_and_multiline_values():
    value = {"result_id": "r", "flow_id": "f", "a/b~c": None, "zero": 0,
             "list": [], "object": {}, "warning": "first\nsecond", "flag": False}
    rows = list(csv.reader(io.StringIO(flow_csv(value).lstrip("\ufeff"))))[1:]
    by_path = {r[2]: r[3:] for r in rows}
    assert by_path["/a~1b~0c"] == ["null", ""]
    assert by_path["/zero"] == ["number", "0"]
    assert by_path["/list"] == ["array", "[]"]
    assert by_path["/object"] == ["object", "{}"]
    assert by_path["/flag"] == ["boolean", "false"]
    assert by_path["/warning"] == ["string", "first\nsecond"]
