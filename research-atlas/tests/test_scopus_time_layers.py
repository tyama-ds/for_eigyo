"""Scopus observations survive reanalysis, time layers, and corpus append."""
from copy import deepcopy

import pytest

from app import corpus_landscape, landscape, storage
from app import scopus_date_enrichment as dates
from app.analytics import analyze, _normalize_papers
from app.merge import merge_papers


def test_scopus_dates_reach_month_quarter_layers_with_immutable_provenance(tmp_path, monkeypatch):
    monkeypatch.setenv("ATLAS_DATA_DIR", str(tmp_path))
    papers = [{"id": f"2-s2.0-{100 + index}", "title": f"Steel fatigue testing study {index}",
               "year": 2021, "publication_date": "", "date_precision": "year", "date_source": "csv:Year",
               "abstract": "Steel microstructure fatigue experiments measure tensile strength and material durability.",
               "authors": [], "keywords": ["steel", "fatigue"], "doi": "", "citations": 0}
              for index in range(5)]
    source = storage.create_dataset(papers, "Scopus time layers test")
    original = deepcopy(storage.read("datasets", source["id"]))
    job = dates.create_job(source["id"])
    queue = dates.queue(job["id"])
    observations = [{"eid": entry["eid"], "url": entry["url"], "status": "found", "matched_by": "label",
                     "date_text": "12 March 2021" if index < 2 else "July 2021", "observed_at": storage.now()}
                    for index, entry in enumerate(queue["entries"][:4])]
    dates.import_results(job["id"], {"schema": dates.RESULT_SCHEMA, "job_id": job["id"],
                                    "batch_id": queue["batch_id"], "observations": observations})
    preview = dates.records(job["id"])
    applied = dates.apply_job(job["id"], snapshot_id=preview["snapshot_id"])
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
        assert [(p["id"], p["count"]) for p in monthly["periods"] if p["count"]] == [("2021-03", 2), ("2021-07", 2)]
        assert [(p["id"], p["count"]) for p in quarterly["periods"] if p["count"]] == [("2021-Q1", 2), ("2021-Q3", 2)]
        assert monthly["projection_id"] == quarterly["projection_id"]
        analyzed = storage.read("results", result["id"])["papers"]
        assert analyzed[0]["scopus_date_enrichment"]["snapshot_id"] == preview["snapshot_id"]
        assert analyzed[0]["date_source"] == "scopus:publication-date"
        assert storage.read("datasets", source["id"]) == original
    finally:
        corpus_landscape._load.cache_clear()
        corpus_landscape._project.cache_clear()
        corpus_landscape._build.cache_clear()


@pytest.mark.parametrize("existing_date", ["", "2024-05-16", "2024-03"])
def test_scopus_audit_follows_adopted_date_when_merging(existing_date):
    base = {"id": "2-s2.0-11", "title": "Study", "year": 2024, "doi": "10.1234/study",
            "publication_date": existing_date, "date_precision": "year" if not existing_date else "day" if len(existing_date) == 10 else "month"}
    if existing_date:
        base["scopus_date_enrichment"] = {"snapshot_id": "old", "original": {"year": 2024}}
    incoming = {**base, "id": "p2", "publication_date": "2024-05-16", "date_precision": "day",
                "scopus_date_enrichment": {"snapshot_id": "new", "original": {"year": 2024}}}
    originals = deepcopy((base, incoming))
    merged, _ = merge_papers([base], [incoming])
    assert merged[0]["scopus_date_enrichment"]["snapshot_id"] == ("old" if existing_date else "new")
    normalized = _normalize_papers(merged, 2020, 2025, [])
    normalized[0]["scopus_date_enrichment"]["original"]["year"] = 2000
    assert merged[0]["scopus_date_enrichment"]["original"]["year"] == 2024
    assert (base, incoming) == originals
