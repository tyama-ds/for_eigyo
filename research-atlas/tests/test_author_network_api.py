import csv
from copy import deepcopy
import io
import json
import time

import pytest
from fastapi.testclient import TestClient

from app import storage, author_exports
from app.main import app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("ATLAS_DATA_DIR", str(tmp_path))
    with TestClient(app) as value:
        yield value


@pytest.fixture
def result(client):
    alice = {"id": "scopus:100", "name": "Alice Ito", "affiliations": ["School of Engineering, Example University"]}
    bob = {"id": "scopus:200", "name": "Bob Sato", "affiliations": ["Example Institute"]}
    another = {"id": "scopus:300", "name": "Alice Ito"}
    papers = [{"id": "p1", "title": "Synthetic author comparison 1", "year": 2023, "authors": [alice, bob],
               "citations": None, "topic_id": "t1", "affiliations": ["Do not assign this to all authors"]},
              {"id": "p2", "title": "Synthetic author comparison 2", "year": 2024, "authors": [alice, bob, another],
               "citations": 0, "topic_id": "t2"}]
    return storage.save("results", {"id": storage.new_id(), "dataset_id": storage.new_id(),
        "dataset_name": "Synthetic identity fixture", "papers": papers,
        "topics": [{"id": "t1", "label": "materials"}, {"id": "t2", "label": "manufacturing"}],
        "network": {"nodes": [], "edges": []}, "meta": {"start_year": 2023, "end_year": 2024, "is_demo": True}})


def run(client, result, group_by):
    response = client.post("/api/author-networks", json={"result_id": result["id"], "group_by": group_by})
    assert response.status_code == 200, response.text
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        job = client.get("/api/jobs/" + response.json()["job_id"]).json()
        if job["status"] in {"completed", "failed"}:
            return job
        time.sleep(.02)
    pytest.fail("Author network did not terminate")


@pytest.mark.parametrize("group_by", ["id", "name", "institution", "community", "topic"])
def test_saved_old_result_supports_all_groupings_and_csv(client, result, group_by):
    before = deepcopy(result)
    job = run(client, result, group_by)
    assert job["status"] == "completed", job
    response = client.get("/api/author-networks/" + job["author_network_id"])
    assert response.status_code == 200
    network = response.json()
    assert network["result_id"] == result["id"] and network["group_by"] == group_by
    assert network["scope"]["is_demo"]
    assert network["stats"]["authors_total"] == 3
    assert network["edges"] and network["clusters"]
    for kind in ("authors", "edges", "clusters"):
        exported = client.get(f"/api/author-networks/{network['id']}/export?kind={kind}")
        assert exported.status_code == 200 and exported.content.startswith(b"\xef\xbb\xbf")
        assert "attachment" in exported.headers["content-disposition"]
        rows = list(csv.DictReader(io.StringIO(exported.content.decode("utf-8-sig"))))
        assert rows and all(row["Grouping"] == group_by for row in rows)
        if kind == "authors":
            assert len(rows) == 3
            assert {row["Author ID"] for row in rows} == {"scopus:100", "scopus:200", "scopus:300"}
    assert storage.read("results", result["id"]) == before


def test_author_network_input_errors_and_origin(client, result):
    body = {"result_id": result["id"]}
    assert client.post("/api/author-networks", json={**body, "group_by": "unsupported"}).status_code == 422
    assert client.post("/api/author-networks", json={"result_id": "../anything"}).status_code == 422
    assert client.post("/api/author-networks", json=body, headers={"Origin": "https://other.example"}).status_code == 403
    assert client.get("/api/author-networks/" + "f" * 32).status_code == 404
    assert client.get("/api/author-networks/" + "f" * 32 + "/export?kind=invalid").status_code == 422


def test_network_disk_error_terminates_job_and_redacts_details(client, result, monkeypatch):
    def fail(*args):
        raise OSError("private filesystem path")
    monkeypatch.setattr(storage, "save", fail)
    job = run(client, result, "institution")
    assert job["status"] == "failed" and "author_network_id" not in job
    assert "private filesystem path" not in job["error"]


def test_authors_csv_uses_complete_export_not_display_cap_and_preserves_unknown():
    node = {"id": "a", "label": "=HYPERLINK(bad)", "count": 1, "citations": 0,
            "citation_known_papers": 0, "original_ids": ["a"], "paper_ids": ["p1"], "cluster_id": None}
    network = {"id": "n", "result_id": "r", "group_by": "community", "nodes": [], "clusters": [],
               "export_data": {"authors": [node]}, "stats": {"displayed_authors": 0, "authors_total": 1}}
    content = author_exports.network_csv(network, "authors")
    row = next(csv.DictReader(io.StringIO(content.lstrip("\ufeff"))))
    assert row["Name"].startswith("'=HYPERLINK")
    assert row["Cumulative citations"] == ""
    assert json.loads(row["Paper IDs (JSON)"]) == ["p1"]
    assert json.loads(row["Author metadata (JSON)"])["cluster_id"] is None
