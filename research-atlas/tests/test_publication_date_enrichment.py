"""Offline enrichment checks: durable batches, date semantics and immutable input."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import csv
import io
import json
import sqlite3
import threading
import time

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app import connection_settings, large_storage, storage
from app import publication_date_enrichment as dates
from app.publication_date_api import router


def paper(identifier, doi=None, **fields):
    return {"id": identifier, "title": "Paper " + identifier, "year": 2021, "doi": doi or "10.1234/" + identifier,
            "publication_date": "", "date_precision": "year", "date_source": "csv:Year", "abstract": "source abstract", **fields}


def found(doi, *candidates):
    return {"doi": doi, "status": "found", "provider": "crossref", "candidates": list(candidates) or [candidate("2021-03-12")], "retryable": False}


def candidate(date, kind="published-online"):
    return {"date": date, "precision": {4: "year", 7: "month", 10: "day"}[len(date)], "kind": kind}


@pytest.fixture
def env(tmp_path, monkeypatch):
    assert not dates._ACTIVE
    monkeypatch.setenv("ATLAS_DATA_DIR", str(tmp_path))
    calls, answers, config = [], {}, {"concurrency": 1}

    class FakeClient:
        def __init__(self, contact_email="", stop_event=None):
            self.concurrency = config["concurrency"]
            self.event = stop_event
            self.email = contact_email

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def fetch(self, doi):
            calls.append(doi)
            value = answers.get(doi, found(doi))
            return value(self) if callable(value) else deepcopy(value)

    monkeypatch.setattr(dates, "CrossrefDateClient", FakeClient)
    yield tmp_path, calls, answers, config
    for _, event in list(dates._ACTIVE.values()):
        event.set()
    timeout = time.monotonic() + 8
    while dates._ACTIVE and time.monotonic() < timeout:
        time.sleep(0.01)
    assert not dates._ACTIVE


def finish(identifier, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = dates.get_job(identifier)
        if value["status"] not in {"preparing", "running"} and not dates._ACTIVE:
            return value
        time.sleep(0.01)
    raise AssertionError("offline date job did not finish")


def start(records, **options):
    dataset = storage.create_dataset(records, "original", report={"source_reports": [{"discovery_request": {"start_year": 2020, "end_year": 2022}}], "warnings": ["original warning"]})
    job = dates.create_job(dataset["id"], **options)
    return dataset, finish(job["id"])


def test_bounded_batch_resume_and_deduplication(env):
    _, calls, _, _ = env
    dataset, first = start([paper("a"), paper("b", "10.1234/a"), paper("c"), paper("d")], batch_size=1)
    assert first["status"] == "paused" and first["unique_dois"] == 3
    assert first["total_papers"] == first["eligible_papers"] == 4
    assert first["processed_dois"] == 1 and first["remaining_dois"] == 2
    dates.resume_job(first["id"], batch_size=2)
    last = finish(first["id"])
    assert last["status"] == "completed" and last["found_dois"] == 3
    assert len(calls) == len(set(calls)) == 3
    assert storage.read("datasets", dataset["id"])["papers"] == dataset["papers"]


def test_cache_reused_across_jobs_without_spending_network_batch(env):
    _, calls, _, _ = env
    start([paper("a"), paper("b")])
    _, second = start([paper("a"), paper("b"), paper("c")], batch_size=1)
    assert second["status"] == "completed" and second["cached_dois"] == 2
    assert second["found_dois"] == 3 and calls == ["10.1234/a", "10.1234/b", "10.1234/c"]


def test_recent_not_found_cached_but_expired_not_found_retried(env):
    path, calls, answers, _ = env
    answers["10.1234/a"] = {"status": "not_found", "candidates": []}
    _, first = start([paper("a")])
    assert first["not_found_dois"] == 1
    _, second = start([paper("a")])
    assert second["cached_dois"] == 1 and len(calls) == 1
    with sqlite3.connect(path / "publication_dates.sqlite") as db:
        db.execute("UPDATE observations SET fetched_at=?", ((datetime.now(timezone.utc) - timedelta(days=8)).isoformat(),))
    start([paper("a")])
    assert len(calls) == 2


def test_restart_recovers_interrupted_job_and_resumes_only_pending(env):
    path, calls, _, _ = env
    _, job = start([paper("a"), paper("b")], batch_size=1)
    with sqlite3.connect(path / "publication_dates.sqlite") as db:
        db.execute("UPDATE jobs SET status='running' WHERE id=?", (job["id"],))
    restored = dates.get_job(job["id"])
    assert restored["status"] == "paused" and restored["error_kind"] == "interrupted"
    dates.resume_job(job["id"])
    assert finish(job["id"])["status"] == "completed"
    assert calls == ["10.1234/a", "10.1234/b"]


def test_pause_during_fetch_preserves_response_and_prevents_followup(env):
    _, calls, answers, _ = env
    ready, release = threading.Event(), threading.Event()
    def delayed(client):
        ready.set()
        release.wait(5)
        return found("10.1234/a")
    answers["10.1234/a"] = delayed
    dataset = storage.create_dataset([paper("a"), paper("b")], "pause")
    job = dates.create_job(dataset["id"])
    assert ready.wait(5)
    assert dates.pause_job(job["id"])["pause_requested"]
    with pytest.raises(dates.JobBusy):
        dates.create_job(dataset["id"])
    release.set()
    saved = finish(job["id"])
    assert saved["status"] == "paused" and saved["found_dois"] == 1 and saved["remaining_dois"] == 1
    assert calls == ["10.1234/a"]


def test_rate_limit_persists_cooldown_and_resume_cannot_bypass(env):
    path, calls, answers, _ = env
    answers["10.1234/a"] = {"status": "error", "error_kind": "rate_limited", "retryable": True, "retry_after_seconds": 120}
    _, job = start([paper("a"), paper("b")])
    assert job["status"] == "paused" and job["error_dois"] == 1 and job["remaining_dois"] == 2
    assert job["retry_at"] > storage.now()
    with pytest.raises(dates.JobBusy, match="再開可能"):
        dates.resume_job(job["id"])
    other = storage.create_dataset([paper("other")], "other")
    with pytest.raises(dates.JobBusy, match="再開可能"):
        dates.create_job(other["id"])
    with sqlite3.connect(path / "publication_dates.sqlite") as db:
        db.execute("UPDATE jobs SET retry_at=?", ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),))
    answers.clear()
    dates.resume_job(job["id"])
    assert finish(job["id"])["status"] == "completed"
    assert calls == ["10.1234/a", "10.1234/a", "10.1234/b"]


def test_retryable_error_is_tried_once_per_batch_and_other_dois_survive(env):
    _, calls, answers, _ = env
    answers["10.1234/a"] = {"status": "error", "error_kind": "timeout", "retryable": True}
    _, job = start([paper("a"), paper("b")])
    assert job["status"] == "paused" and job["found_dois"] == 1 and job["error_dois"] == 1
    assert calls == ["10.1234/a", "10.1234/b"]
    answers.clear()
    dates.resume_job(job["id"])
    assert finish(job["id"])["found_dois"] == 2


def test_arbitrary_error_and_contact_and_proxy_are_not_saved(env):
    path, _, answers, config = env
    config["concurrency"] = 3
    observed = []
    def inspect(client):
        observed.append((client.email, connection_settings.current_settings().proxy.password.get_secret_value()))
        return {"status": "error", "error_kind": "http://user:password@private.invalid/research@example.org", "retryable": False,
                "source_url": "private", "error": "raw-secret"}
    answers["10.1234/a"] = inspect
    settings = connection_settings.ConnectionSettings.model_validate({"proxy": {"enabled": True, "url": "http://proxy.example:8080", "username": "proxy-user", "password": "proxy-password-secret"}})
    with connection_settings.settings_context(settings):
        _, job = start([paper("a")], contact_email="research@example.org")
    assert job["error_dois"] == 1
    assert observed == [("research@example.org", "proxy-password-secret")]
    exported = "".join(dates.export_csv(job["id"]))
    db_bytes = (path / "publication_dates.sqlite").read_bytes()
    for secret in ["research@example.org", "proxy-password-secret", "raw-secret", "private.invalid"]:
        assert secret not in exported and secret.encode() not in db_bytes


def test_targets_cover_year_month_day_and_doi_link(env):
    _, calls, _, _ = env
    records = [paper("year"), paper("month", publication_date="2021-03", date_precision="month"),
               paper("day", publication_date="2021-03-12", date_precision="day"), paper("link", doi="", source_link="https://doi.org/10.1234/from-link"),
               paper("missing", doi="", source_link="https://www.scopus.com/record/display.uri?eid=2-s2.0-1")]
    records[-1]["doi"] = ""
    records[-2]["doi"] = ""
    dataset, job = start(records, target="missing_month")
    assert job["eligible_papers"] == 3 and job["unique_dois"] == 2 and job["missing_doi_papers"] == 1
    assert "10.1234/from-link" in calls
    coverage = dates.dataset_summary(dataset["id"])["coverage"]
    assert coverage == {"total_papers": 5, "month_or_day": 2, "day": 1, "missing_month": 3, "missing_day": 4, "no_doi": 1}


@pytest.mark.parametrize("policy,expected", [("same_year", "2021-04"), ("online_first", "2020-12-30"), ("print_first", "2021-04")])
def test_publication_kinds_remain_distinct_and_policy_changes_year_only_explicitly(policy, expected):
    result = dates.choose_date({"year": 2021, "publication_date": "", "date_precision": "year"},
        [candidate("2020-12-30"), candidate("2021-04", "published-print")], policy=policy)
    assert result["candidate"]["date"] == expected and result["apply"] and result["conflict"]


@pytest.mark.parametrize("existing,precision,incoming,apply", [("", "year", "2021-06", True),
    ("2021-06", "month", "2021-06-15", True), ("2021-05", "month", "2021-06-15", False),
    ("2021-06-14", "day", "2021-06-15", False), ("2021-06-15", "day", "2021-06", False)])
def test_fill_only_preserves_existing_precision_and_conflicting_explicit_dates(existing, precision, incoming, apply):
    result = dates.choose_date({"year": 2021, "publication_date": existing, "date_precision": precision}, [candidate(incoming)])
    assert result["apply"] is apply


def test_year_only_source_does_not_invent_month_and_cross_year_default_not_applied():
    original = {"year": 2021, "publication_date": "", "date_precision": "year"}
    assert not dates.choose_date(original, [candidate("2021")])["apply"]
    conflict = dates.choose_date(original, [candidate("2020-12-30")])
    assert not conflict["apply"] and conflict["reason"] == "year_conflict"


def test_matching_month_refinement_survives_conflicting_preferred_kind():
    result = dates.choose_date({"year": 2021, "publication_date": "2021-05", "date_precision": "month"},
        [candidate("2021-04-30"), candidate("2021-05-02", "published-print")])
    assert result["apply"] and result["candidate"]["date"] == "2021-05-02" and result["conflict"]


def test_future_exclusions_and_safe_failure_reason_are_retained(env):
    _, _, answers, _ = env
    answers["10.1234/a"] = {"status": "not_found", "error_kind": "no_publication_date", "candidates": [],
        "excluded_candidates": [{**candidate("2099-03-04"), "reason": "future"}]}
    _, job = start([paper("a")])
    record = dates.records(job["id"])["items"][0]
    assert record["observation"]["excluded_candidates"][0]["reason"] == "future"
    assert record["observation"]["error_kind"] == "no_publication_date"
    assert "2099-03-04" in "".join(dates.export_csv(job["id"]))
    assert dates.apply_job(job["id"])["applied_count"] == 0


def test_apply_is_immutable_and_keeps_discovery_provenance_and_snapshot(env):
    path, _, answers, _ = env
    answers["10.1234/b"] = found("10.1234/b", candidate("2020-12-31"))
    source, job = start([paper("a"), paper("b")])
    before = deepcopy(storage.read("datasets", source["id"]))
    applied = dates.apply_job(job["id"])
    derived = storage.read("datasets", applied["dataset_id"])
    assert applied["applied_count"] == applied["conflict_count"] == 1
    assert derived["report"]["source_reports"] == source["report"]["source_reports"]
    assert derived["report"]["date_precision_counts"]["day"] == 1
    assert derived["papers"][0]["publication_date"] == "2021-03-12"
    assert derived["papers"][1]["publication_date"] == ""
    assert derived["papers"][0]["date_enrichment"]["original"]["date_precision"] == "year"
    assert storage.read("datasets", source["id"]) == before
    with sqlite3.connect(path / "publication_dates.sqlite") as db:
        snapshot = db.execute("SELECT COUNT(*) FROM snapshot_items WHERE snapshot_id=?", (applied["snapshot_id"],)).fetchone()[0]
    assert snapshot == 2


def test_partial_apply_does_not_wait_for_missing_dois_and_future_fetch_does_not_change_snapshot(env):
    path, _, _, _ = env
    source, job = start([paper("a"), paper("b")], batch_size=1)
    applied = dates.apply_job(job["id"])
    assert applied["applied_count"] == 1
    dates.resume_job(job["id"])
    finish(job["id"])
    derived = storage.read("datasets", applied["dataset_id"])
    assert derived["papers"][1]["publication_date"] == ""
    with sqlite3.connect(path / "publication_dates.sqlite") as db:
        assert db.execute("SELECT COUNT(*) FROM snapshot_items WHERE snapshot_id=?", (applied["snapshot_id"],)).fetchone()[0] == 1


def test_indexed_dataset_uses_streaming_prepare_and_externalized_derived_data(env, monkeypatch):
    _, _, _, _ = env
    monkeypatch.setattr(large_storage, "LARGE_CORPUS_THRESHOLD", 2)
    records = [paper(str(index), doi="10.1234/shared") for index in range(600)]
    dataset = storage.create_dataset(records, "large")
    original_read = storage.read
    def only_manifest(kind, identifier, *, include_papers=True):
        assert not include_papers, "enrichment must stream source papers from the manifest"
        return original_read(kind, identifier, include_papers=False)
    monkeypatch.setattr(storage, "read", only_manifest)
    job = finish(dates.create_job(dataset["id"], batch_size=1)["id"])
    assert job["total_papers"] == 600 and job["unique_dois"] == 1
    applied = dates.apply_job(job["id"])
    assert applied["applied_count"] == 600
    assert original_read("datasets", applied["dataset_id"], include_papers=False)["_large_store"]["count"] == 600


def test_twenty_thousand_papers_deduplicate_before_network_and_counts_stay_exact(env, monkeypatch):
    _, calls, _, _ = env
    monkeypatch.setattr(large_storage, "LARGE_CORPUS_THRESHOLD", 25000)
    _, job = start([paper(str(index), doi="10.1234/shared") for index in range(20000)], batch_size=1)
    assert job["status"] == "completed" and job["total_papers"] == job["eligible_papers"] == 20000
    assert job["unique_dois"] == job["processed_dois"] == 1 and calls == ["10.1234/shared"]
    page = dates.records(job["id"], limit=2, offset=19998)
    assert len(page["items"]) == 2 and page["total"] == 20000


def test_twenty_thousand_unique_dois_persist_two_bounded_batches_and_restart(env, monkeypatch):
    path, calls, _, _ = env
    monkeypatch.setattr(large_storage, "LARGE_CORPUS_THRESHOLD", 25000)
    dataset = storage.create_dataset([paper(f"p{index:05d}") for index in range(20000)], "20k unique DOI")
    first = finish(dates.create_job(dataset["id"], batch_size=500)["id"], timeout=45)
    assert first["status"] == "paused" and first["unique_dois"] == 20000
    assert first["processed_dois"] == first["found_dois"] == 500 and first["remaining_dois"] == 19500
    with sqlite3.connect(path / "publication_dates.sqlite") as db:
        assert db.execute("SELECT COUNT(*) FROM items WHERE job_id=? AND status='found'", (first["id"],)).fetchone()[0] == 500
        # Simulate shutdown while the status was marked running. The completed
        # observation references survive independently of process memory.
        db.execute("UPDATE jobs SET status='running' WHERE id=?", (first["id"],))
    recovered = dates.get_job(first["id"])
    assert recovered["status"] == "paused" and recovered["error_kind"] == "interrupted"
    dates.resume_job(first["id"], batch_size=500)
    second = finish(first["id"], timeout=45)
    assert second["status"] == "paused" and second["processed_dois"] == 1000 and second["remaining_dois"] == 19000
    assert len(calls) == len(set(calls)) == 1000
    page = dates.records(first["id"], limit=2, offset=19998)
    assert [record["paper_id"] for record in page["items"]] == ["p19998", "p19999"]
    assert all(record["status"] == "pending" for record in page["items"])
    with sqlite3.connect(path / "publication_dates.sqlite") as db:
        assert db.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 1000


def test_derived_dates_flow_through_analysis_into_month_and_quarter_layers_with_provenance(env):
    from app import corpus_landscape, landscape
    from app.analytics import analyze
    _, _, answers, _ = env
    source_papers = [paper(identifier, title="Steel fatigue testing " + identifier,
        abstract="Steel microstructure fatigue experiments measure tensile strength and material durability.",
        source_link="https://doi.org/10.1234/" + identifier) for identifier in ["a", "b", "c", "d", "e"]]
    for identifier in ["a", "b"]:
        answers["10.1234/" + identifier] = found("10.1234/" + identifier, candidate("2021-03-12"))
    for identifier in ["c", "d"]:
        answers["10.1234/" + identifier] = found("10.1234/" + identifier, candidate("2021-07", "published-print"))
    answers["10.1234/e"] = {"status": "not_found", "candidates": []}
    source, job = start(source_papers)
    applied = dates.apply_job(job["id"])
    derived = storage.read("datasets", applied["dataset_id"])
    result = analyze(derived["papers"], {"start_year": 2021, "end_year": 2021,
        "n_topics": 1, "topic_model": "kmeans", "embedding": "tfidf", "map_projection": "pca"})
    result.update(id=storage.new_id(), dataset_id=derived["id"])
    storage.save("results", result)
    try:
        monthly = landscape.build_landscape(result["id"], projection="pca", interval="month", scope="full")
        quarterly = landscape.build_landscape(result["id"], projection="pca", interval="quarter", scope="full")
        assert monthly["meta"]["eligible_papers"] == quarterly["meta"]["eligible_papers"] == 4
        assert monthly["meta"]["excluded_date_count"] == quarterly["meta"]["excluded_date_count"] == 1
        assert [(period["id"], period["count"]) for period in monthly["periods"] if period["count"]] == [("2021-03", 2), ("2021-07", 2)]
        assert [(period["id"], period["count"]) for period in quarterly["periods"]] == [("2021-Q1", 2), ("2021-Q2", 0), ("2021-Q3", 2)]
        assert sum(period["count"] == 0 for period in monthly["periods"]) == 3
        assert monthly["projection_id"] == quarterly["projection_id"]
        analyzed = {record["id"]: record for record in storage.read("results", result["id"])["papers"]}
        assert analyzed["a"]["date_enrichment"]["snapshot_id"] == applied["snapshot_id"]
        assert analyzed["c"]["date_enrichment"]["kind"] == "published-print"
        assert analyzed["c"]["date_precision"] == "month"
        assert analyzed["a"]["source_link"] == source_papers[0]["source_link"]
        assert all(not record["publication_date"] for record in storage.read("datasets", source["id"])["papers"])
    finally:
        corpus_landscape._load.cache_clear()
        corpus_landscape._project.cache_clear()
        corpus_landscape._build.cache_clear()


def test_restart_during_preparation_rebuilds_compact_index_without_duplicates(env):
    path, calls, _, _ = env
    _, job = start([paper("a"), paper("b")], batch_size=1)
    with sqlite3.connect(path / "publication_dates.sqlite") as db:
        db.execute("UPDATE jobs SET initialized=0,status='preparing' WHERE id=?", (job["id"],))
    assert dates.get_job(job["id"])["status"] == "paused"
    dates.resume_job(job["id"])
    complete = finish(job["id"])
    assert complete["status"] == "completed" and complete["total_papers"] == 2
    assert complete["cached_dois"] == 1 and calls == ["10.1234/a", "10.1234/b"]


def test_record_pagination_and_csv_preserve_candidates_and_escape_formulas(env):
    _, _, answers, _ = env
    answers["10.1234/a"] = found("10.1234/a", candidate("2020-12-31"), candidate("2021-02", "published-print"))
    _, job = start([paper("a", title="=HYPERLINK(secret)"), paper("b")])
    page = dates.records(job["id"], limit=1, policy="online_first")
    assert len(page["items"]) == 1 and page["total"] == 2
    assert page["items"][0]["proposed"]["candidate"]["date"] == "2020-12-31"
    text = "".join(dates.export_csv(job["id"]))
    rows = list(csv.DictReader(io.StringIO(text.lstrip("\ufeff"))))
    assert rows[0]["title"].startswith("'=HYPERLINK")
    assert {row["candidate_kind"] for row in rows} == {"published-online", "published-print"}


@pytest.mark.parametrize("body", [{"batch_size": 0}, {"batch_size": 20001}, {"batch_size": True},
    {"batch_size": "500"}, {"target": "url_scrape"}, {"contact_email": "invalid"}, {"contact_email": "a@example.org\nsecret"}])
def test_api_rejects_invalid_options_before_start(env, body):
    dataset = storage.create_dataset([paper("a")], "API")
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    response = client.post("/api/publication-date-jobs", json={"dataset_id": dataset["id"], **body})
    assert response.status_code == 422
    assert not dates._ACTIVE


def test_api_contract_and_streaming_export(env):
    dataset = storage.create_dataset([paper("a")], "API")
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    response = client.post("/api/publication-date-jobs", json={"dataset_id": dataset["id"]})
    assert response.status_code == 200
    job = finish(response.json()["id"])
    summary = client.get(f"/api/datasets/{dataset['id']}/publication-dates").json()
    assert summary["jobs"][0]["id"] == job["id"] and summary["coverage"]["missing_month"] == 1
    assert client.get(f"/api/publication-date-jobs/{job['id']}/records?limit=101").status_code == 422
    exported = client.get(f"/api/publication-date-jobs/{job['id']}/export")
    assert exported.status_code == 200 and "candidate_kind" in exported.text
    applied = client.post(f"/api/publication-date-jobs/{job['id']}/apply", json={}).json()
    assert applied["applied_count"] == 1
