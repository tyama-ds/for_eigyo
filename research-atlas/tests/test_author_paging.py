"""Author-node browsing uses computed membership, not rejected alias unions."""
from copy import deepcopy

from fastapi.testclient import TestClient
import pytest

from app import large_storage, main, storage
from app.author_network import build_author_network


@pytest.fixture(params=[False, True], ids=["inline", "indexed"])
def saved(request, tmp_path, monkeypatch):
    monkeypatch.setenv("ATLAS_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(large_storage, "LARGE_CORPUS_THRESHOLD", 2 if request.param else 100)
    authors = [
        {"id": "scopus:42", "name": "Alice", "aliases": ["orcid:alpha"]},
        {"id": "scopus:43", "name": "Alice", "aliases": ["orcid:alpha"]},
        {"id": "orcid:other", "name": "Same Name", "aliases": ["name:shared"]},
        {"id": "orcid:a", "name": "Same Name", "aliases": ["name:shared"]},
        {"id": "name:shared", "name": "Same Name"},
        {"name": "Anonymous User"},
        {"id": "name:old-hash", "name": "Anonymous User"},
        {"id": "scopus:421", "name": "Another", "aliases": ["external:only"]},
    ]
    papers = [{"id": f"p{index + 1}", "title": f"Paper {index + 1}", "year": 2025,
               "citations": index, "authors": [author], "topic_id": "topic-a"}
              for index, author in enumerate(authors)]
    result = {"id": storage.new_id(), "papers": papers, "network": build_author_network(papers), "meta": {"paper_count": 8}}
    storage.save("results", result)
    return result


def ids(response):
    assert response.status_code == 200, response.text
    return {paper["id"] for paper in response.json()["items"]}


def test_canonical_orcid_unites_multiple_raw_scopus_ids_and_raw_fallback_is_exact(saved):
    with TestClient(main.app) as client:
        url = f"/api/results/{saved['id']}/papers"
        assert ids(client.get(url, params={"author_id": "orcid:alpha"})) == {"p1", "p2"}
        assert ids(client.get(url, params={"author_id": "scopus:42"})) == {"p1"}
        assert ids(client.get(url, params={"author_id": "scopus:4"})) == set()
        assert ids(client.get(url, params={"author_id": "external:only"})) == {"p8"}


def test_ambiguous_name_aliases_never_leak_between_canonical_people(saved):
    assert saved["network"]["stats"]["ambiguous_aliases"] > 0
    with TestClient(main.app) as client:
        url = f"/api/results/{saved['id']}/papers"
        assert ids(client.get(url, params={"author_id": "orcid:a"})) == {"p4"}
        assert ids(client.get(url, params={"author_id": "orcid:other"})) == {"p3"}
        node = next(node for node in saved["network"]["nodes"] if node["paper_ids"] == ["p5"])
        assert ids(client.get(url, params={"author_id": node["id"]})) == {"p5"}


def test_name_estimate_node_has_all_underlying_missing_and_legacy_id_papers(saved):
    node = next(node for node in saved["network"]["nodes"] if node["paper_ids"] == ["p6", "p7"])
    assert node["id"].startswith("name:")
    with TestClient(main.app) as client:
        response = client.get(f"/api/results/{saved['id']}/papers", params={"author_id": node["id"], "sort": "title"})
        assert ids(response) == {"p6", "p7"}


def test_canonical_membership_is_revision_scoped_and_not_limited_by_display_lists(saved, monkeypatch):
    changed = deepcopy(saved)
    changed["id"] = storage.new_id()
    changed["papers"][0]["authors"] = [{"id": "orcid:other", "name": "Different Person"}]
    changed["network"] = build_author_network(changed["papers"])
    storage.save("results", changed)
    original_read = storage.read
    def read(kind, identifier, **kwargs):
        assert kwargs.get("include_papers") is False
        result = original_read(kind, identifier, **kwargs)
        if result.get("_large_store"):
            # Browser summaries can have omitted or truncated evidence lists.
            result["network"]["nodes"] = []
        return result
    monkeypatch.setattr(storage, "read", read)
    with TestClient(main.app) as client:
        assert ids(client.get(f"/api/results/{saved['id']}/papers", params={"author_id": "orcid:alpha"})) == {"p1", "p2"}
        assert ids(client.get(f"/api/results/{changed['id']}/papers", params={"author_id": "orcid:alpha"})) == {"p2"}


def test_internal_author_union_uses_a_single_bound_array_and_exact_aliases(saved):
    manifest = storage.read("results", saved["id"], include_papers=False)
    wanted = [f"missing:{index}" for index in range(1200)] + ["scopus:42", "external:only", "' OR 1=1 --"]
    response = large_storage.paper_page(manifest, storage.data_root(), author_ids=wanted)
    assert {paper["id"] for paper in response["items"]} == {"p1", "p8"}
