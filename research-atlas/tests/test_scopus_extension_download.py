import io
import json
import zipfile

from fastapi.testclient import TestClient

from app.main import app
from app import scopus_extension_api


def test_extension_download_contains_only_installable_companion_files():
    with TestClient(app) as client:
        response = client.get("/api/scopus-browser-extension")
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/zip"
        assert "attachment" in response.headers["content-disposition"]
        assert response.content == client.get("/api/scopus-browser-extension").content
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        prefix = "research-atlas-scopus-dates/"
        assert set(archive.namelist()) == {prefix + name for name in scopus_extension_api.EXTENSION_FILES}
        manifest = json.loads(archive.read(prefix + "manifest.json"))
        assert manifest["manifest_version"] == 3
        assert set(manifest.get("permissions", [])) <= {"storage", "scripting", "tabs"}
        assert set(manifest["host_permissions"]) <= {"https://www.scopus.com/*", "https://scopus.com/*"}
        assert "<all_urls>" not in str(manifest)
        assert not manifest.get("externally_connectable")


def test_incomplete_extension_returns_actionable_error(tmp_path, monkeypatch):
    monkeypatch.setattr(scopus_extension_api, "EXTENSION_ROOT", tmp_path)
    with TestClient(app) as client:
        response = client.get("/api/scopus-browser-extension")
    assert response.status_code == 503
    assert "更新" in response.json()["detail"]
