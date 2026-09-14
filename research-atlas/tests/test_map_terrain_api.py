import json

import pytest
from fastapi.testclient import TestClient

from app import storage
from app.main import app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("ATLAS_DATA_DIR", str(tmp_path))
    with TestClient(app) as client:
        yield client


def test_saved_results_gain_terrain_without_reanalysis_or_persistence(client, tmp_path):
    result = {"id": storage.new_id(), "map": {"nodes": [
        {"id": "paper-a", "x": .25, "y": .4, "citations": 900},
        {"id": "paper-b", "x": .75, "y": .6, "citations": 1},
    ]}, "papers": [{"id": "paper-a"}, {"id": "paper-b"}]}
    storage.save("results", result)
    before = {str(p.relative_to(tmp_path)): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    response = client.get(f"/api/results/{result['id']}/terrain")
    assert response.status_code == 200, response.text
    value = response.json()
    assert value["version"] == 1
    assert value["meta"]["displayed_papers"] == 2
    assert set(value["node_heights"]) == {"paper-a", "paper-b"}
    assert len(value["grid"]["values"]) == value["grid"]["width"] * value["grid"]["height"]
    assert "no-store" in response.headers["cache-control"]
    assert client.get(f"/api/results/{result['id']}/terrain").json() == value
    after = {str(p.relative_to(tmp_path)): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    assert after == before
    assert storage.read("results", result["id"]) == result
    json.dumps(value, allow_nan=False)


def test_terrain_handles_legacy_result_without_map(client):
    identifier = storage.new_id()
    storage.save("results", {"id": identifier})
    response = client.get(f"/api/results/{identifier}/terrain")
    assert response.status_code == 200
    assert response.json()["meta"]["displayed_papers"] == 0
    assert not any(response.json()["grid"]["values"])


@pytest.mark.parametrize("identifier", ["missing", "0" * 32])
def test_terrain_missing_results_return_404(client, identifier):
    response = client.get(f"/api/results/{identifier}/terrain")
    assert response.status_code == 404
    assert "traceback" not in response.text.lower()


def test_same_result_id_with_changed_projection_cannot_reuse_stale_geometry(client):
    identifier = storage.new_id()
    storage.save("results", {"id": identifier, "map": {"nodes": [{"id": "a", "x": .2, "y": .2}]}})
    before = client.get(f"/api/results/{identifier}/terrain").json()
    storage.save("results", {"id": identifier, "map": {"nodes": [{"id": "a", "x": .8, "y": .8}]}})
    after = client.get(f"/api/results/{identifier}/terrain").json()
    assert before["grid"] != after["grid"]
