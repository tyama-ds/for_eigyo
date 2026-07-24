"""ジョブライフサイクル統合テスト — 受入条件 1,2,3,6,7,8,9,12,23 に対応。

実PostgreSQL + 実Redis + Celery worker subprocess + Mock Runner subprocessで検証。
"""

from __future__ import annotations

import uuid

import pytest

from tests.conftest import requires_infra
from tests.integration.helpers import (
    FAST,
    clear_roles,
    create_job,
    event_history,
    get_job,
    setup_llm_profile,
    wait_for_job,
    wait_for_run_status,
)

pytestmark = requires_infra


@pytest.fixture(autouse=True)
def _stack(api_client, celery_worker_proc):
    """全テストでAPI + workerを起動。"""
    yield


class TestParallelExecution:
    def test_three_mocks_run_in_parallel_with_per_engine_events(self, api_client):
        """受入1: 3つのMockが同時開始し、エンジン別カード相当の実イベントが更新される。"""
        engines = ["mock-fast", "mock-slow", "mock-partial"]
        job = create_job(api_client, engines)
        final = wait_for_job(api_client, job["id"], timeout=90)
        assert final["status"] in ("completed", "partial")
        assert {r["engine_id"] for r in final["runs"]} == set(engines)
        events = event_history(api_client, job["id"])
        # 各engineにstage/status イベントが存在する
        for engine in engines:
            engine_events = [e for e in events if e.get("engine_id") == engine]
            assert any(e["type"] == "engine_stage" for e in engine_events), engine
            assert any(e["type"] == "run_status" for e in engine_events), engine
        # 並列性: mock-slow完了前にmock-fastが完了している
        seq_by = {
            (e["engine_id"], e["payload"].get("status")): e["seq"]
            for e in events if e["type"] == "run_status"
        }
        assert seq_by[("mock-fast", "succeeded")] < seq_by[("mock-slow", "succeeded")]

    def test_metrics_and_null_not_fabricated(self, api_client):
        """mock-partialのtoken metricsはnullのまま保持され、0等へ捏造されない。"""
        job = create_job(api_client, ["mock-partial"])
        final = wait_for_job(api_client, job["id"])
        run = final["runs"][0]
        assert run["status"] == "succeeded"
        assert run["metrics"].get("prompt_tokens") is None
        assert run["metrics"].get("llm_cost_usd") is None
        assert run["metrics"].get("search_api_cost_usd") == 0.0
        assert any("取得できません" in w for w in run["warnings"])


class TestPartialFailure:
    def test_one_failure_does_not_stop_others(self, api_client):
        """受入2: 1つが失敗しても他は完了し、jobはpartialになる。"""
        job = create_job(api_client, ["mock-fast", "mock-fail", "mock-slow"])
        final = wait_for_job(api_client, job["id"], timeout=120)
        assert final["status"] == "partial"
        by_engine = {r["engine_id"]: r for r in final["runs"]}
        assert by_engine["mock-fast"]["status"] == "succeeded"
        assert by_engine["mock-slow"]["status"] == "succeeded"
        assert by_engine["mock-fail"]["status"] == "failed"
        assert by_engine["mock-fail"]["error"]
        # 再試行が行われた形跡 (attempt > 1)
        assert by_engine["mock-fail"]["attempt"] >= 2
        assert any("完了しませんでした" in w for w in final["warnings"])

    def test_all_fail_job_failed(self, api_client):
        job = create_job(api_client, ["mock-fail"])
        final = wait_for_job(api_client, job["id"], timeout=120)
        assert final["status"] == "failed"


class TestCancellation:
    def test_individual_cancel(self, api_client):
        """受入3a: 個別キャンセル — 他のrunは完走する。"""
        job = create_job(
            api_client,
            ["mock-fast", "mock-cancellable"],
            engine_options={"mock-fast": dict(FAST),
                            "mock-cancellable": {"speed_factor": 1.0, "seed": 42}},
        )
        run = wait_for_run_status(api_client, job["id"], "mock-cancellable",
                                  ("researching",))
        resp = api_client.post(f"/api/jobs/{job['id']}/runs/{run['id']}/cancel")
        assert resp.status_code == 200
        final = wait_for_job(api_client, job["id"], timeout=90)
        by_engine = {r["engine_id"]: r for r in final["runs"]}
        assert by_engine["mock-cancellable"]["status"] == "cancelled"
        assert by_engine["mock-fast"]["status"] == "succeeded"
        assert final["status"] == "partial"

    def test_cancel_all(self, api_client):
        """受入3b: 全体キャンセル。"""
        job = create_job(
            api_client,
            ["mock-cancellable", "mock-timeout"],
            engine_options={"mock-cancellable": {"speed_factor": 1.0},
                            "mock-timeout": {"speed_factor": 1.0}},
        )
        wait_for_run_status(api_client, job["id"], "mock-cancellable", ("researching",))
        resp = api_client.post(f"/api/jobs/{job['id']}/cancel")
        assert resp.status_code == 200
        final = wait_for_job(api_client, job["id"], timeout=90)
        assert final["status"] == "cancelled"
        for run in final["runs"]:
            assert run["status"] == "cancelled", run

    def test_timeout_marks_timed_out(self, api_client):
        job = create_job(
            api_client, ["mock-timeout"],
            engine_options={"mock-timeout": {"speed_factor": 0.5}},
            max_time_seconds=10,
        )
        final = wait_for_job(api_client, job["id"], timeout=120)
        assert final["runs"][0]["status"] == "timed_out"
        assert final["status"] == "failed"


class TestIdempotency:
    def test_same_idempotency_key_no_duplicate(self, api_client):
        """受入6: 同じidempotency keyで重複実行されない。"""
        key = f"idem-{uuid.uuid4().hex}"
        job1 = create_job(api_client, ["mock-fast"], idempotency_key=key)
        resp = api_client.post(
            "/api/jobs",
            json={"topic": "テスト用の調査テーマ", "language": "ja",
                  "engines": ["mock-fast"],
                  "engine_options": {"mock-fast": dict(FAST)},
                  "auto_synthesize": False, "idempotency_key": key},
        )
        assert resp.status_code == 200  # 201ではなく既存を返す
        assert resp.json()["id"] == job1["id"]
        jobs = [j for j in api_client.get("/api/jobs").json()
                if j.get("id") == job1["id"]]
        assert len(jobs) == 1


class TestCitationsAndProvenance:
    def test_citation_resolves_to_runner_url_excerpt(self, api_client):
        """受入7: 引用からRunner、元URL、excerptへ辿れる。"""
        job = create_job(api_client, ["mock-fast", "mock-slow"])
        wait_for_job(api_client, job["id"])
        claims = api_client.get(f"/api/jobs/{job['id']}/claims").json()
        assert claims
        cited = [c for c in claims if c["evidence"]]
        assert cited
        for claim in cited:
            assert claim["engine_id"] in ("mock-fast", "mock-slow")  # Runnerへ辿れる
            for ev in claim["evidence"]:
                assert ev["url"], claim  # 元URL
        # 少なくとも1つはexcerptを持つ
        assert any(ev["excerpt"] for c in cited for ev in c["evidence"])

    def test_unsupported_claim_not_marked_cited(self, api_client):
        """受入8: 引用のない主張は引用済みとして扱われない。"""
        job = create_job(api_client, ["mock-partial"])
        wait_for_job(api_client, job["id"])
        claims = api_client.get(f"/api/jobs/{job['id']}/claims").json()
        unsupported = [c for c in claims if not c["evidence"]]
        assert unsupported, "mock-partialは引用なしclaimを含むはず"
        compare = api_client.get(f"/api/jobs/{job['id']}/compare").json()
        texts = [u["text"] for u in compare["unsupported_claims"]]
        assert any("根拠となる引用なし" in t for t in texts)

    def test_source_dedup_keeps_provenance(self, api_client):
        """URL重複排除後もRunner別のprovenanceが残る。"""
        job = create_job(api_client, ["mock-fast", "mock-slow"])
        wait_for_job(api_client, job["id"])
        sources = api_client.get(f"/api/jobs/{job['id']}/sources").json()
        # 共通source (canonical同一) が両engineのrun行として存在
        by_canonical: dict[str, set[str]] = {}
        for s in sources:
            by_canonical.setdefault(s["canonical_url"], set()).add(s["engine_id"])
        shared = [urls for urls in by_canonical.values() if len(urls) == 2]
        assert shared, "両エンジンが同一canonical URLを発見しているはず"
        # canonicalizeの検証: utm_source付きURLの原型が保持される
        raw_urls = [s["url"] for s in sources]
        assert any("utm_source" in u for u in raw_urls)
        assert all("utm_source" not in s["canonical_url"] for s in sources)


class TestConflictsAndSynthesis:
    def test_conflicts_survive_into_compare_and_synthesis(self, api_client, celery_worker_proc):
        """受入9+23: 矛盾がConflictsと統合レポートに残る。LLMはlocal fixtureのみ
        (OpenAI/Anthropic key未設定のbaseline動作 = 受入23のmock版)。"""
        from tests.fixtures.servers import OpenAiCompatServer

        server = OpenAiCompatServer().start()
        try:
            setup_llm_profile(api_client, server.url + "/v1")
            job = create_job(api_client, ["mock-fast", "mock-slow"],
                             auto_synthesize=True)
            final = wait_for_job(api_client, job["id"], timeout=120)
            assert final["status"] == "completed"

            compare = api_client.get(f"/api/jobs/{job['id']}/compare").json()
            conflict_keys = [c["key"] for c in compare["conflicts"]]
            assert "growth-rate" in conflict_keys
            growth = next(c for c in compare["conflicts"] if c["key"] == "growth-rate")
            assert sorted(growth["values"]) == ["12%", "25%"]
            # 多数決で隠されず両claimが残る
            assert {m["engine_id"] for m in growth["claims"]} == {"mock-fast", "mock-slow"}
            # 一致した発見
            agree_keys = [a["key"] for a in compare["agreements"]]
            assert "market-size-2025" in agree_keys

            synthesis = api_client.get(f"/api/jobs/{job['id']}/synthesis").json()
            assert synthesis["status"] == "succeeded"
            assert "矛盾" in synthesis["report_markdown"]
            # 引用がsource registryへ解決されている
            assert synthesis["citations"]
            for c in synthesis["citations"]:
                assert c["url"].startswith("http")
                assert c["engines"]
            # fixtureが混ぜた未知引用 [S99] は除去されwarningになる
            assert "[S99]" not in synthesis["report_markdown"]
            assert any("S99" in w for w in synthesis["warnings"])
            # 決定論的比較セクションも保持
            assert synthesis["sections"]["conflicts"]
        finally:
            clear_roles(api_client)
            server.stop()

    def test_synthesis_unavailable_without_llm(self, api_client):
        """LLM未設定時はsilent fallbackせずunavailable+理由を返す。"""
        clear_roles(api_client)
        job = create_job(api_client, ["mock-fast"], auto_synthesize=True)
        final = wait_for_job(api_client, job["id"], timeout=90)
        assert final["status"] == "completed"
        synthesis = api_client.get(f"/api/jobs/{job['id']}/synthesis").json()
        assert synthesis["status"] == "unavailable"
        assert "割り当てられていません" in synthesis["error"]

    def test_synthesis_only_retry(self, api_client):
        from tests.fixtures.servers import OpenAiCompatServer

        clear_roles(api_client)
        job = create_job(api_client, ["mock-fast", "mock-slow"], auto_synthesize=True)
        wait_for_job(api_client, job["id"], timeout=90)
        assert api_client.get(f"/api/jobs/{job['id']}/synthesis").json()["status"] == "unavailable"

        server = OpenAiCompatServer().start()
        try:
            setup_llm_profile(api_client, server.url + "/v1")
            resp = api_client.post(f"/api/jobs/{job['id']}/synthesis/retry")
            assert resp.status_code == 202
            import time

            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                synthesis = api_client.get(f"/api/jobs/{job['id']}/synthesis").json()
                if synthesis["status"] == "succeeded":
                    break
                time.sleep(0.5)
            assert synthesis["status"] == "succeeded"
            assert synthesis["attempt"] >= 2
        finally:
            clear_roles(api_client)
            server.stop()


class TestExport:
    def test_export_preserves_provenance(self, api_client):
        """受入12: Markdown/JSONエクスポートでprovenanceを保持。"""
        job = create_job(api_client, ["mock-fast", "mock-slow"])
        wait_for_job(api_client, job["id"])

        resp = api_client.get(f"/api/jobs/{job['id']}/export", params={"format": "json"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["sources"]
        for s in data["sources"]:
            assert s["engine_id"]  # provenance
            assert s["url"]
        assert data["claims"]
        cited = [c for c in data["claims"] if c["evidence"]]
        assert cited and all(e["url"] for c in cited for e in c["evidence"])

        resp = api_client.get(f"/api/jobs/{job['id']}/export", params={"format": "markdown"})
        assert resp.status_code == 200
        md = resp.text
        assert "エンジン: mock-fast" in md
        assert "https://example.org/" in md


class TestEngineValidation:
    def test_unknown_engine_rejected(self, api_client):
        resp = api_client.post(
            "/api/jobs",
            json={"topic": "t", "language": "ja", "engines": ["no-such-engine"],
                  "auto_synthesize": False},
        )
        assert resp.status_code == 400
        assert "未知のエンジン" in resp.json()["detail"]

    def test_disabled_engine_rejected_no_silent_fallback(self, api_client, db_session):
        """受入11/24: disabled/unsupportedなエンジンは起動前に拒否され、
        Mockへのsilent fallbackも試行通信も発生しない。"""
        from app.db.models import EngineConfig

        cfg = EngineConfig(
            engine_id="paid-search-engine",
            display_name="Paid Search Engine",
            runner_url="http://127.0.0.1:1",
            enabled=True,
            availability="unsupported",
            unavailable_reason="有料検索API (Tavily) が必須のためMVPでは未対応です",
        )
        db_session.add(cfg)
        db_session.commit()
        try:
            resp = api_client.post(
                "/api/jobs",
                json={"topic": "t", "language": "ja",
                      "engines": ["paid-search-engine"], "auto_synthesize": False},
            )
            assert resp.status_code == 400
            assert "有料検索API" in resp.json()["detail"]
            # jobは作成されない (fallback実行なし)
            jobs = api_client.get("/api/jobs").json()
            assert all(
                all(r["engine_id"] != "paid-search-engine" for r in j["runs"])
                for j in jobs
            )
        finally:
            db_session.delete(cfg)
            db_session.commit()

    def test_engines_endpoint_reports_health(self, api_client):
        engines = api_client.get("/api/engines").json()
        by_id = {e["engine_id"]: e for e in engines}
        assert by_id["mock-fast"]["availability"] == "available"
        assert by_id["mock-fast"]["healthy"] is True
        # 実エンジンはRunner未設定ならdisabled + 理由
        for real in ("gpt-researcher", "open-deep-research"):
            if real in by_id and not by_id[real]["enabled"]:
                assert by_id[real]["availability"] == "disabled"
                assert by_id[real]["unavailable_reason"]
