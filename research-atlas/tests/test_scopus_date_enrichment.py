"""Browser-result import is offline, strict, resumable and dataset preserving."""
from copy import deepcopy
import csv
import io
import json
import sqlite3

import pytest

from app import large_storage, storage
from app import scopus_date_enrichment as dates


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("ATLAS_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(dates, "_today", lambda: "2026-10-14")
    return tmp_path


def paper(number=105048869659, **fields):
    eid = f"2-s2.0-{number}"
    return {"id": eid, "title": "Paper " + str(number), "year": 2021, "publication_date": "", "date_precision": "year", "date_source": "csv:Year", "abstract": "Original abstract", "authors": [], "keywords": [], "citations": 0, "aliases": {"eids": [eid]}, **fields}


def start(papers=None, **options):
    dataset = storage.create_dataset(papers or [paper()], "Original", report={"warnings": ["retained warning"], "source_reports": [{"source": "csv"}]})
    return dataset, dates.create_job(dataset["id"], **options)


def observation(entry, **fields):
    return {"eid": entry["eid"], "url": entry["url"], "status": "found", "date_text": "12 March 2021", "matched_by": "label", "observed_at": "2026-10-14T00:00:00.000Z", **fields}


def result(queue, observations=None):
    return {"schema": dates.RESULT_SCHEMA, "job_id": queue["job_id"], "batch_id": queue["batch_id"], "observations": observations if observations is not None else [observation(entry) for entry in queue["entries"]]}


@pytest.mark.parametrize("url", [
    "https://scopus.com/pages/publications/105048869659?origin=resultslist",
    "https://www.scopus.com/pages/publications/105048869659",
    "https://www.scopus.com/record/display.uri?eid=2-s2.0-105048869659&origin=resultslist",
    "https://www.scopus.com/inward/record.uri?eid=2-s2.0-105048869659&partnerID=40",
])
def test_identity_routes_and_link_only(url):
    assert dates.url_eid(url) == "2-s2.0-105048869659"
    value = paper(id="doi:10.1/example", aliases={}, external_url=url)
    assert dates.paper_identity(value)["eid"] == "2-s2.0-105048869659"


@pytest.mark.parametrize("url", [
    "https://scopus.com.evil.test/pages/publications/105048869659",
    "https://www.scopus.com@evil.test/pages/publications/105048869659",
    "https://user@www.scopus.com/pages/publications/105048869659",
    "http://www.scopus.com/pages/publications/105048869659",
    "javascript:alert(1)", "file:///pages/publications/105048869659",
    "https://www.scopus.com/login?eid=2-s2.0-105048869659",
    "https://www.scopus.com/pages/publications/105048869659?eid=2-s2.0-9",
    "https://www.scopus.com/record/display.uri?eid=2-s2.0-9&eid=2-s2.0-10",
    "https://www.scopus.com:8080/pages/publications/105048869659",
])
def test_identity_rejects_confused_or_unsafe_urls(url):
    assert dates.url_eid(url) is None


def test_conflicting_identifiers_excluded_and_reported():
    dataset, job = start([paper(), paper(11, aliases={"eids": ["2-s2.0-12"]}), paper(13, source_link=dates.canonical_url("2-s2.0-14")), paper(id="doi:only", aliases={})])
    assert job["unique_eids"] == 1 and job["identity_conflicts"] == 2
    assert job["no_eid_papers"] == 1
    coverage = dates.dataset_summary(dataset["id"])["coverage"]
    assert coverage["eligible_identity"] == 1 and coverage["identity_conflicts"] == 2
    rows = dates.records(job["id"])["items"]
    assert [r["proposed"]["reason"] for r in rows[1:]] == ["identity_conflict", "identity_conflict", "no_eid"]


def test_partial_batches_resumable_errors_explicit_and_found_cached(env):
    dataset, job = start([paper(1), paper(2), paper(3), paper(4, id="duplicate-paper", aliases={"eids": ["2-s2.0-1"]})])
    first = dates.queue(job["id"], batch_size=2)
    assert len(first["entries"]) == 2 and job["unique_eids"] == 3
    dates.import_results(job["id"], result(first, [observation(first["entries"][0])]))
    second = dates.queue(job["id"], batch_size=1)
    assert second["entries"][0]["eid"] == "2-s2.0-2"
    dates.import_results(job["id"], result(second, [observation(second["entries"][0], status="requires_access", date_text="", matched_by="", url="https://login.elsevier.com/")]))
    assert [x["eid"] for x in dates.queue(job["id"], batch_size=2)["entries"]] == ["2-s2.0-3"]
    assert [x["eid"] for x in dates.queue(job["id"], batch_size=2, retry_errors=True)["entries"]] == ["2-s2.0-2", "2-s2.0-3"]
    cached = dates.create_job(dataset["id"])
    assert cached["found_eids"] == 1 and cached["pending_eids"] == 2
    assert (env / "scopus_publication_dates.sqlite").exists()
    assert not (env / "publication_dates.sqlite").exists()


def test_import_membership_job_mismatch_and_atomic_rejection():
    _, job = start([paper(1), paper(2)])
    queue = dates.queue(job["id"], batch_size=1)
    invalid = result(queue, [observation(queue["entries"][0]), observation({"eid": "2-s2.0-2", "url": dates.canonical_url("2-s2.0-2")})])
    with pytest.raises(ValueError, match="キュー"):
        dates.import_results(job["id"], invalid)
    assert dates.get_job(job["id"])["processed_eids"] == 0
    with pytest.raises(ValueError):
        dates.import_results(job["id"], {**result(queue), "job_id": "f" * 32})
    with pytest.raises(ValueError):
        dates.import_results(job["id"], result(queue, [observation(queue["entries"][0])] * 2))


def test_idempotent_history_and_error_never_overwrites_found():
    _, job = start()
    queue = dates.queue(job["id"])
    payload = result(queue)
    assert dates.import_results(job["id"], payload)["imported"] == 1
    assert dates.import_results(job["id"], payload)["duplicates"] == 1
    error = result(queue, [observation(queue["entries"][0], status="timeout", date_text="", matched_by="", observed_at="2026-10-14T00:01:00Z")])
    dates.import_results(job["id"], error)
    row = dates.records(job["id"])["items"][0]
    assert row["status"] == "found" and row["proposed"]["apply"]
    audit = json.loads("".join(dates.export_records(job["id"], format="json")))
    assert len(audit["observations"]) == 2
    assert {x["status"] for x in audit["observations"]} == {"found", "timeout"}
    assert dates.queue(job["id"], retry_errors=True)["entries"] == []


@pytest.mark.parametrize("text,reason", [("2021", "year_only"), ("31 February 2021", "invalid_date"), ("03/04/2021", "invalid_date"), ("2020-03-12", "year_conflict"), ("2027-01-01", "future_date")])
def test_bad_coarse_conflicting_future_dates_not_applied(text, reason):
    dataset, job = start()
    queue = dates.queue(job["id"])
    dates.import_results(job["id"], result(queue, [observation(queue["entries"][0], date_text=text)]))
    preview = dates.records(job["id"])
    assert preview["items"][0]["proposed"]["reason"] == reason
    assert not preview["items"][0]["proposed"]["apply"]
    applied = dates.apply_job(job["id"], snapshot_id=preview["snapshot_id"])
    assert applied["applied_count"] == 0
    assert storage.read("datasets", applied["dataset_id"])["papers"] == dataset["papers"]


def test_snapshot_pins_preview_pagination_apply_and_policy():
    dataset, job = start([paper(1), paper(2)])
    queue = dates.queue(job["id"])
    dates.import_results(job["id"], result(queue))
    first = dates.records(job["id"], limit=1)
    dates.import_results(job["id"], result(queue, [observation(row, date_text="15 April 2021", observed_at="2026-10-14T00:01:00Z") for row in queue["entries"]]))
    second = dates.records(job["id"], limit=1, offset=1, snapshot_id=first["snapshot_id"])
    assert second["items"][0]["proposed"]["candidate"]["date"] == "2021-03-12"
    assert dates.records(job["id"])["items"][0]["proposed"]["candidate"]["date"] == "2021-04-15"
    with pytest.raises(dates.SnapshotConflict):
        dates.apply_job(job["id"], snapshot_id=first["snapshot_id"], overwrite_existing=True)
    applied = dates.apply_job(job["id"], snapshot_id=first["snapshot_id"])
    assert applied["applied_count"] == 2
    derived = storage.read("datasets", applied["dataset_id"])
    assert {p["publication_date"] for p in derived["papers"]} == {"2021-03-12"}
    assert dates.apply_job(job["id"], snapshot_id=first["snapshot_id"])["dataset_id"] == applied["dataset_id"]
    assert storage.read("datasets", dataset["id"])["papers"] == dataset["papers"]
    assert derived["report"]["warnings"] == ["retained warning"]


def test_older_import_does_not_replace_newer_observation_or_cache():
    dataset, job = start()
    queue = dates.queue(job["id"])
    dates.import_results(job["id"], result(queue, [observation(queue["entries"][0], date_text="April 2021", observed_at="2026-10-14T01:00:00Z")]))
    dates.import_results(job["id"], result(queue))
    assert dates.records(job["id"])["items"][0]["proposed"]["candidate"]["date"] == "2021-04"
    cached = dates.create_job(dataset["id"])
    assert dates.records(cached["id"])["items"][0]["proposed"]["candidate"]["date"] == "2021-04"


def test_year_override_existing_date_and_provenance():
    dataset, job = start([paper(1, year=2020, publication_date="2020-04", date_precision="month", date_enrichment={"provider": "crossref"})], target="all")
    queue = dates.queue(job["id"])
    dates.import_results(job["id"], result(queue))
    assert dates.records(job["id"])["items"][0]["proposed"]["reason"] == "year_conflict"
    assert dates.records(job["id"], allow_year_change=True)["items"][0]["proposed"]["reason"] == "existing_date_conflict"
    preview = dates.records(job["id"], allow_year_change=True, overwrite_existing=True)
    output = dates.apply_job(job["id"], snapshot_id=preview["snapshot_id"], allow_year_change=True, overwrite_existing=True)
    value = storage.read("datasets", output["dataset_id"])["papers"][0]
    assert value["year"] == 2021 and value["date_source"] == "scopus:publication-date"
    assert value["date_enrichment"] == {"provider": "crossref"}
    assert value["scopus_date_enrichment"]["original"]["publication_date"] == "2020-04"
    assert value["scopus_date_enrichment"]["date_text"] == "12 March 2021"
    assert output["changed_year_count"] == 1


def test_month_can_be_refined_to_day_but_outside_target_preserved():
    _, job = start([paper(1, publication_date="2021-03", date_precision="month"), paper(2, publication_date="2021-03-10", date_precision="day")], target="missing_day")
    queue = dates.queue(job["id"])
    dates.import_results(job["id"], result(queue))
    items = dates.records(job["id"])["items"]
    assert items[0]["proposed"]["apply"]
    assert not items[1]["proposed"]["apply"] and items[1]["proposed"]["reason"] == "outside_target"


def test_wrong_page_or_unmatched_label_cannot_contribute_dates():
    _, job = start([paper(1), paper(2)])
    queue = dates.queue(job["id"])
    rows = [observation(queue["entries"][0], url=dates.canonical_url("2-s2.0-3")), observation(queue["entries"][1], matched_by="")]
    dates.import_results(job["id"], result(queue, rows))
    items = dates.records(job["id"])["items"]
    assert not any(row["proposed"]["apply"] for row in items)
    assert [row["proposed"]["reason"] for row in items] == ["identity_mismatch", "missing_match_method"]


def test_export_keeps_snapshot_and_all_history_and_neutralizes_formula():
    _, job = start([paper(title="=SUM(1,2)")])
    queue = dates.queue(job["id"])
    dates.import_results(job["id"], result(queue))
    preview = dates.records(job["id"])
    output = list(csv.DictReader(io.StringIO("".join(dates.export_records(job["id"], snapshot_id=preview["snapshot_id"])).lstrip("\ufeff"))))
    assert len(output) == 2
    assert output[0]["title"].startswith("'=")
    assert output[0]["snapshot_id"] == preview["snapshot_id"]
    assert output[1]["row_type"] == "observation"


def test_streamed_20000_papers_all_records_immutable_and_no_hydration(monkeypatch):
    records = [paper(index + 1) for index in range(20_000)]
    dataset = storage.create_dataset(records, "20,000 papers")
    raw_read = storage.read
    def no_hydration(kind, identifier, *, include_papers=True):
        assert not include_papers, "must use indexed iteration, never hydrate the corpus"
        return raw_read(kind, identifier, include_papers=False)
    monkeypatch.setattr(storage, "read", no_hydration)
    job = dates.create_job(dataset["id"])
    assert job["total_papers"] == job["unique_eids"] == 20_000
    for _ in range(40):
        queue = dates.queue(job["id"], batch_size=500)
        dates.import_results(job["id"], result(queue))
    assert dates.queue(job["id"])["entries"] == []
    preview = dates.records(job["id"], offset=19_950, limit=50)
    assert len(preview["items"]) == 50 and all(x["proposed"]["apply"] for x in preview["items"])
    applied = dates.apply_job(job["id"], snapshot_id=preview["snapshot_id"])
    assert applied["applied_count"] == 20_000
    original = no_hydration("datasets", dataset["id"], include_papers=False)
    derived = no_hydration("datasets", applied["dataset_id"], include_papers=False)
    assert derived["_large_store"]["count"] == 20_000
    assert all(p["publication_date"] == "" for p in large_storage.iter_papers(original, storage.data_root()))
    assert all(p["publication_date"] == "2021-03-12" for p in large_storage.iter_papers(derived, storage.data_root()))
    assert derived["report"]["date_precision_counts"]["day"] == 20_000
