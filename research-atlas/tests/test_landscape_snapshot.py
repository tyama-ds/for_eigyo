"""Annual child reports reuse one validated landscape without mutating it."""
from copy import deepcopy

import pytest

from app import centroid_reports, landscape, landscape_reports, large_storage, storage


@pytest.fixture
def saved_snapshot(tmp_path, monkeypatch):
    monkeypatch.setenv("ATLAS_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(large_storage, "LARGE_CORPUS_THRESHOLD", 2)
    papers = [{"id": f"p{i}", "title": f"Steel treatment {i}",
               "year": 2023 if i < 8 else 2024, "topic_id": "steel",
               "abstract": f"Laser treatment study {i} measured strength and fatigue."}
              for i in range(16)]
    result = {"id": storage.new_id(), "meta": {}, "papers": papers,
              "topics": [{"id": "steel", "label": "Steel"}]}
    storage.save("results", result)
    manifest = storage.read("results", result["id"], include_papers=False)
    assert manifest["papers"] == [] and manifest["_large_store"]["count"] == 16
    center = {"topic_id": "steel", "period_id": "2024", "count": 8,
              "x": .4, "y": .5, "paper_ids": [f"p{i}" for i in range(8, 16)],
              "evidence_ids": ["p15", "p14", "p15", "p13", "p12", "p11", "p10", "p9"],
              "terms": [{"term": "laser", "count": 8}], "period_count": 8,
              "share_of_period": 1, "valid_vector_count": 8, "dispersion": .03}
    movement = {"id": "movement", "topic_id": "steel", "topic_label": "Steel",
                "from_period": "2023", "to_period": "2024", "from_count": 8, "to_count": 8,
                "distance_2d": .1, "cosine_distance": .05, "status": "stable",
                "evidence_before": ["p7", "p6", "p7", "p5", "p4", "p3", "p2", "p1"],
                "evidence_after": list(center["evidence_ids"]),
                "from_terms": [{"term": "heat", "count": 5}],
                "to_terms": deepcopy(center["terms"])}
    snapshot = {"result_id": result["id"], "projection_id": "shared-projection",
                "map": {"projection": {"requested_method": "pca", "algorithm": "pca"}},
                "meta": {"interval": "year", "scope": "full", "analysis_papers": 16},
                "topics": deepcopy(result["topics"]), "centroids": [center],
                "movements": [movement], "warnings": ["Source coverage is incomplete."],
                "interpretation": {"limitations": ["Imported papers only."]}}
    monkeypatch.setattr(landscape, "build_landscape",
                        lambda *args, **kwargs: pytest.fail("A supplied snapshot must not be rebuilt"))
    return result, snapshot


def prepare(kind, result_id, snapshot, *, scope="full", projection="pca"):
    options = {"projection_id": "shared-projection", "scope": scope,
               "landscape_snapshot": snapshot}
    if kind == "centroid":
        return centroid_reports.prepare_report(result_id, projection, "year", "steel", "2024", **options)
    return landscape_reports.prepare_report(result_id, projection, "year", "movement", **options)


@pytest.mark.parametrize("kind", ["centroid", "movement"])
@pytest.mark.parametrize("scope", ["sample", "full"])
@pytest.mark.parametrize("projection", ["pca", "auto"])
def test_prepared_reports_reuse_snapshot_and_bounded_indexed_papers_without_mutation(
        saved_snapshot, monkeypatch, kind, scope, projection):
    result, snapshot = saved_snapshot
    snapshot["meta"]["scope"] = scope
    # Automatic projection can select PCA; validation concerns the requested method.
    snapshot["map"]["projection"]["requested_method"] = projection
    original = deepcopy(snapshot)
    lookups, original_lookup = [], large_storage.papers_by_ids

    def lookup(value, root, identifiers):
        lookups.append(list(identifiers))
        return original_lookup(value, root, identifiers)

    monkeypatch.setattr(large_storage, "papers_by_ids", lookup)
    report = prepare(kind, result["id"], snapshot, scope=scope, projection=projection)
    expected_after = ["p15", "p14", "p13", "p12", "p11", "p10"]
    expected = expected_after if kind == "centroid" else ["p7", "p6", "p5", "p4", "p3", "p2", *expected_after]
    assert lookups == [expected]
    assert {p["id"] for p in report["evidence_papers"]} == set(expected)
    originals = {p["id"]: p for p in result["papers"]}
    assert all(p["abstract"] == originals[p["id"]]["abstract"] for p in report["evidence_papers"])
    assert report["scope"] == scope and report["projection"] == projection
    assert report["projection_id"] == "shared-projection"
    assert snapshot == original
    if kind == "centroid":
        assert "paper_ids" not in report["centroid"]
        report["centroid"]["terms"][0]["count"] = 999
    else:
        assert report["movement"]["evidence_after"] == expected_after
        report["movement"]["to_terms"][0]["count"] = 999
    report["meta"]["analysis_papers"] = 999
    report["limitations"].append("Report-only note")
    assert snapshot == original
    assert storage.read("results", result["id"])["papers"] == result["papers"]


@pytest.mark.parametrize("kind", ["centroid", "movement"])
@pytest.mark.parametrize("mismatch", ["result", "interval", "scope", "requested_projection", "projection_id"])
def test_snapshot_context_mismatch_is_rejected_before_reading_evidence(
        saved_snapshot, monkeypatch, kind, mismatch):
    result, snapshot = saved_snapshot
    if mismatch == "result":
        snapshot["result_id"] = storage.new_id()
    elif mismatch == "interval":
        snapshot["meta"]["interval"] = "month"
    elif mismatch == "scope":
        snapshot["meta"]["scope"] = "sample"
    elif mismatch == "requested_projection":
        snapshot["map"]["projection"]["requested_method"] = "umap"
    else:
        snapshot["projection_id"] = "different-projection"
    original = deepcopy(snapshot)
    monkeypatch.setattr(storage, "read",
                        lambda *args, **kwargs: pytest.fail("Reject the mismatched snapshot before reading evidence"))
    with pytest.raises(ValueError, match="一致|版"):
        prepare(kind, result["id"], snapshot)
    assert snapshot == original
