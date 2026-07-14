"""Section 6/2: 診断API・health・Origin検証・秘密の非漏洩の回帰テスト。"""

from __future__ import annotations

from tests.conftest import PIANO_QUESTION

from fermiscope.config import load_settings, proxy_without_credentials
from fermiscope.diagnostics import collect_diagnostics


def test_healthz_liveness(app_client):
    r = app_client.get("/healthz")
    assert r.status_code == 200 and r.json()["status"] == "ok"


def test_readyz_readiness(app_client):
    r = app_client.get("/readyz")
    assert r.status_code == 200
    assert r.json()["ready"] is True


def test_api_health_hides_secrets():
    """診断はプロキシ認証情報・DB資格情報・APIキーを出さない。"""
    s = load_settings(
        env={
            "HTTPS_PROXY": "http://user:supersecret@proxy.corp:3128",
            "NO_PROXY": "localhost",
            "FERMISCOPE_DATABASE_URL": "postgresql://dbuser:dbpass@dbhost/fermi",
        }
    )
    diag = collect_diagnostics(s)
    blob = str(diag)
    assert "supersecret" not in blob  # プロキシパスワード
    assert "dbpass" not in blob and "dbuser" not in blob  # DB資格情報
    assert "dbhost" not in blob  # DBホスト名も出さない
    # 有無・スキームは出る
    assert diag["proxy"]["https_proxy_set"] is True
    assert diag["proxy"]["https_proxy"] == "http://proxy.corp:3128"
    assert diag["database_scheme"] == "postgresql"


def test_proxy_without_credentials():
    assert (
        proxy_without_credentials("http://u:p@host:3128") == "http://host:3128"
    )
    assert proxy_without_credentials("http://host:3128") == "http://host:3128"
    assert proxy_without_credentials("") == ""


def test_origin_guard_rejects_cross_site_post(app_client):
    """状態変更POSTに外部Originが付いていれば 403(CSRF対策)。"""
    r = app_client.post(
        "/api/projects",
        json={"question": PIANO_QUESTION},
        headers={"origin": "https://evil.example.com"},
    )
    assert r.status_code == 403


def test_origin_guard_allows_same_site_post(app_client):
    """同一Origin(testserver)のPOSTは許可される。"""
    r = app_client.post(
        "/api/projects",
        json={"question": PIANO_QUESTION},
        headers={"origin": "http://testserver"},
    )
    assert r.status_code == 200


def test_get_requests_not_origin_guarded(app_client):
    """GET は Origin 検証の対象外(安全メソッド)。"""
    r = app_client.get("/api/health", headers={"origin": "https://evil.example.com"})
    assert r.status_code == 200
