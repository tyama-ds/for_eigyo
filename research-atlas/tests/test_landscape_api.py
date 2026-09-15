import pytest
from fastapi.testclient import TestClient

from app import storage
from app.main import app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("ATLAS_DATA_DIR", str(tmp_path))
    with TestClient(app) as client:
        yield client


def test_api_is_read_only_and_validates_selectors(client, tmp_path):
    identifier = storage.new_id()
    storage.save("results", {"id": identifier})
    path = tmp_path / "results" / f"{identifier}.json"
    before = path.read_bytes()
    response = client.get(f"/api/results/{identifier}/landscape?projection=pca&interval=quarter")
    assert response.status_code == 200, response.text
    assert response.json()["meta"]["interval"] == "quarter"
    assert "no-store" in response.headers["cache-control"]
    assert path.read_bytes() == before
    assert client.get(f"/api/results/{identifier}/landscape?projection=other").status_code == 422
    assert client.get(f"/api/results/{identifier}/landscape?interval=day").status_code == 422


@pytest.mark.parametrize("identifier", ["bad", "0" * 32])
def test_missing_result_is_404(client, identifier):
    assert client.get(f"/api/results/{identifier}/landscape").status_code == 404
