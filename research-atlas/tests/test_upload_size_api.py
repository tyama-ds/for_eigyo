"""CSV uploads are uncapped and processed from the spooled file."""
import io

import pytest
from fastapi.testclient import TestClient
from starlette.datastructures import UploadFile

from app import main, storage


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("ATLAS_DATA_DIR", str(tmp_path))
    with TestClient(main.app) as value:
        yield value


def test_large_declared_upload_is_not_rejected_by_request_size_guard(client):
    # ASGI exercises the Content-Length guard independently from parsing bytes.
    response = client.post("/api/import", files={"file": ("papers.csv", b"Title,Year\nSteel,2025\n")},
                           headers={"Content-Length": str(300 * 1024 * 1024)})
    assert response.status_code == 200, response.text
    assert response.json()["dataset"]["paper_count"] == 1


def test_large_declared_citation_upload_also_bypasses_byte_guard(client):
    uploaded = client.post("/api/import", files={"file": ("papers.csv", b"EID,Title,Year\np1,Steel,2025\n")})
    identifier = uploaded.json()["dataset"]["id"]
    response = client.post(f"/api/datasets/{identifier}/citations",
        files={"file": ("citations.csv", b"EID,Year,Citations\np1,2025,12\n")},
        headers={"Content-Length": str(300 * 1024 * 1024)})
    assert response.status_code == 200, response.text
    saved = storage.read("datasets", response.json()["dataset"]["id"])
    assert saved["papers"][0]["citation_history"]["2025"] == 12


def test_non_upload_request_size_guard_and_origin_check_remain(client):
    response = client.post("/api/demo", headers={"Content-Length": str(300 * 1024 * 1024)})
    assert response.status_code == 413
    assert client.post("/api/import", files={"file": ("papers.csv", b"Title,Year\nSteel,2025\n")},
                       headers={"Origin": "https://elsewhere.example"}).status_code == 403


def test_csv_parse_receives_seekable_file_instead_of_full_upload_bytes(client, monkeypatch):
    original = main.parse_scopus_csv
    seen = []
    def parse(source):
        assert not isinstance(source, bytes)
        assert source.seekable() and not source.closed
        seen.append(source)
        return original(source)
    monkeypatch.setattr(main, "parse_scopus_csv", parse)
    response = client.post("/api/import", files={"file": ("papers.csv", b"Title,Year\nSteel,2025\n")})
    assert response.status_code == 200, response.text
    assert len(seen) == 1 and seen[0].closed


@pytest.mark.parametrize("filename,content", [("bad.exe", b"invalid"), ("empty.csv", b""), ("bad.csv", b"bad header")])
def test_invalid_upload_closes_the_spooled_file(filename, content):
    import asyncio
    source = io.BytesIO(content)
    upload = UploadFile(file=source, filename=filename)
    with pytest.raises((ValueError, main.HTTPException)):
        asyncio.run(main.import_csv(upload, "scopus_csv"))
    assert source.closed
