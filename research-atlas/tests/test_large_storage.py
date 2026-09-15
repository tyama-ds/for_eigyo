"""Indexed revisions retain complete data while pages and exports stay bounded."""

from copy import deepcopy
from datetime import date
import json
import sqlite3

import pytest

from app import large_storage, storage


@pytest.fixture
def indexed_root(tmp_path, monkeypatch):
    monkeypatch.setenv("ATLAS_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(large_storage, "LARGE_CORPUS_THRESHOLD", 2)
    return tmp_path


def papers():
    return [
        {"id": "p1", "title": "Zinc 日本語", "abstract": "測定結果, quoted \"text\"\nsecond line",
         "year": 2000, "citations": 0, "doi": "10.1234/a", "topic_id": "topic-a",
         "keywords": ["ALPHA", "100%"], "authors": [{"id": "scopus:12", "name": "Alice",
             "aliases": ["orcid:12"], "affiliations": ["大学 A"]}],
         "references": [{"doi": "10.1234/source", "id": "pmid:42"}],
         "citation_history": {"2024": 0, "2025": 3}, "custom": {"raw": [None, {"unit": "MPa"}]}},
        {"id": "p2", "title": "Alpha", "abstract": "battery result", "year": 2025,
         "citations": 9, "topic_id": "topic-b", "keywords": [],
         "authors": [{"id": "scopus:123", "name": "Bob"}], "references": []},
        {"id": "p3", "title": "Beta", "abstract": "new battery result", "year": 2025,
         "citations": None, "topic_id": "topic-a", "keywords": ["process"],
         "authors": [{"id": "scopus:12", "name": "Alice"}], "references": [{"doi": "10.1234/a"}]},
    ]


def network():
    return {"nodes": [{"id": "scopus:12", "paper_count": 100, "paper_ids": [f"p{i}" for i in range(100)]}],
            "edges": [{"source": "scopus:12", "target": "scopus:123", "weight": 80,
                       "shared_paper_ids": [f"p{i}" for i in range(80)]}],
            "export_data": {"full_edges": ["retained"]}}


def payload(records=None):
    records = papers() if records is None else records
    return {"id": storage.new_id(), "created_at": "2026-09-15T00:00:00Z", "name": "多年データ",
            "is_demo": False, "paper_count": len(records), "papers": records,
            "report": {"warnings": ["source note"]}, "network": network(),
            "export_data": {"authors": [{"id": f"a{i}"} for i in range(4)], "institutions": ["大学 A"]}}


def descriptor(value, root):
    return json.loads((root / "datasets" / f"{value['id']}.json").read_text(encoding="utf-8"))


def test_indexed_dataset_roundtrip_retains_complete_nested_papers_and_network(indexed_root):
    original = payload()
    before = deepcopy(original)
    saved = storage.save("datasets", original)
    assert saved == before and original == before
    manifest = descriptor(saved, indexed_root)
    assert manifest["papers"] == [] and manifest["_large_store"]["count"] == 3
    node = manifest["network"]["nodes"][0]
    assert len(node["paper_ids"]) == 50 and node["paper_ids_total"] == 100
    assert node["paper_count"] == 100 and node["paper_ids_truncated"] is True
    assert manifest["export_data"] == {} and "export_data" not in manifest["network"]
    assert storage.read("datasets", saved["id"]) == before
    assert list(large_storage.iter_papers(manifest, indexed_root)) == before["papers"]


def test_append_versions_reuse_content_and_keep_old_members_immutable(indexed_root):
    first = storage.create_dataset(papers(), "first")
    old = deepcopy(first)
    appended = deepcopy(papers()) + [{"id": "p4", "title": "New paper", "year": 2026, "authors": [], "references": []}]
    second = storage.create_dataset(appended, "second")
    with sqlite3.connect(indexed_root / "large" / "papers.sqlite3") as db:
        assert db.execute("SELECT COUNT(*) FROM papers").fetchone()[0] == 4
        assert db.execute("SELECT COUNT(*) FROM members").fetchone()[0] == 7
        assert db.execute("SELECT COUNT(DISTINCT owner) FROM members").fetchone()[0] == 2
    appended[0]["custom"]["raw"][1]["unit"] = "changed"
    assert storage.read("datasets", first["id"]) == old
    assert storage.read("datasets", second["id"])["papers"][0] == old["papers"][0]
    first_manifest = storage.read("datasets", first["id"], include_papers=False)
    with pytest.raises(KeyError):
        large_storage.paper_by_id(first_manifest, indexed_root, "p4")


def test_cached_summary_and_listing_do_not_restore_full_corpus(indexed_root, monkeypatch):
    saved = storage.create_dataset(papers(), "summary dataset")
    expected = storage.dataset_summary(saved)
    def forbidden(*args, **kwargs):
        raise AssertionError("summary must not load indexed papers")
    monkeypatch.setattr(large_storage, "iter_papers", forbidden)
    manifest = storage.read("datasets", saved["id"], include_papers=False)
    assert manifest["papers"] == []
    assert storage.dataset_summary(manifest) == expected
    assert storage.list_datasets() == [expected]
    assert expected["analysis_defaults"] == {"start_year": 2000, "end_year": min(date.today().year, 2025)}


@pytest.mark.parametrize("options", [
    {"sort": "year"}, {"sort": "citations"}, {"sort": "title"},
    {"offset": 1, "limit": 1}, {"query": "日本語"}, {"query": "ALPHA"},
    {"query": "100%"}, {"query": "10.1234/a"}, {"query": "Alice"},
    {"topic_id": "topic-a", "year": 2025}, {"author_id": "scopus:12"},
    {"author_id": "scopus:1"}, {"query": "nonexistent"}, {"offset": 99},
])
def test_indexed_page_matches_inline_filter_sort_and_pagination(indexed_root, options):
    original = payload()
    manifest = large_storage.externalize(original, indexed_root)
    expected = large_storage.paper_page(original, indexed_root, **options)
    assert large_storage.paper_page(manifest, indexed_root, **options) == expected


def test_exact_author_identity_and_single_paper_stay_isolated_across_revisions(indexed_root):
    first = large_storage.externalize(payload(), indexed_root)
    changed = payload()
    changed["papers"][0]["citations"] = 999
    changed["papers"][0]["authors"] = [{"id": "scopus:123", "name": "Different"}]
    second = large_storage.externalize(changed, indexed_root)
    assert large_storage.paper_by_id(first, indexed_root, "p1")["citations"] == 0
    assert large_storage.paper_by_id(second, indexed_root, "p1")["citations"] == 999
    first_page = large_storage.paper_page(first, indexed_root, author_id="scopus:12")
    second_page = large_storage.paper_page(second, indexed_root, author_id="scopus:12")
    assert {paper["id"] for paper in first_page["items"]} == {"p1", "p3"}
    assert {paper["id"] for paper in second_page["items"]} == {"p3"}
    assert large_storage.papers_by_ids(first, indexed_root, ["p3", "p1", "p3", "missing"]) == [papers()[2], papers()[0]]
    with pytest.raises(KeyError):
        large_storage.paper_by_id(first, indexed_root, "missing")


def test_network_only_storage_retains_inline_papers_and_complete_auxiliaries(indexed_root):
    original = payload(papers()[:2])
    manifest = large_storage.externalize(original, indexed_root)
    assert manifest["papers"] == original["papers"] and manifest["_large_store"]["count"] == 0
    assert large_storage.restore(deepcopy(manifest), indexed_root) == original
    assert json.loads(b"".join(large_storage.export_json(manifest, indexed_root))) == original


@pytest.mark.parametrize("indexed", [False, True])
def test_json_export_is_lossless_bounded_unicode_and_excludes_internal_markers(indexed_root, indexed, monkeypatch):
    original = payload()
    original["papers"][0]["abstract"] = "日本語😀\n" * 20_000
    value = large_storage.externalize(original, indexed_root) if indexed else deepcopy(original)
    value["_dataset_summary"] = {"internal": "do not export"}
    def forbidden(*args, **kwargs):
        raise AssertionError("JSON download must stream payloads instead of restoring the corpus")
    monkeypatch.setattr(large_storage, "restore", forbidden)
    monkeypatch.setattr(large_storage, "iter_papers", forbidden)
    chunks = list(large_storage.export_json(value, indexed_root))
    assert len(chunks) > 2 and all(isinstance(chunk, bytes) and 0 < len(chunk) <= 65536 for chunk in chunks)
    exported = json.loads(b"".join(chunks))
    assert exported == original
    assert "_large_store" not in exported and "_dataset_summary" not in exported
    assert len(exported["network"]["nodes"][0]["paper_ids"]) == 100
    assert len(exported["export_data"]["authors"]) == 4


def test_missing_members_never_silently_restore_or_export_partial_corpus(indexed_root):
    manifest = large_storage.externalize(payload(), indexed_root)
    with sqlite3.connect(indexed_root / "large" / "papers.sqlite3") as db:
        db.execute("DELETE FROM members WHERE owner=? AND ordinal=0", (manifest["_large_store"]["owner"],))
    with pytest.raises(ValueError, match="論文件数"):
        large_storage.restore(deepcopy(manifest), indexed_root)
    with pytest.raises(ValueError, match="論文件数"):
        b"".join(large_storage.export_json(manifest, indexed_root))


def test_auxiliary_path_cannot_escape_large_store(indexed_root):
    manifest = large_storage.externalize(payload(), indexed_root)
    manifest["_large_store"]["network"] = "../outside.json"
    with pytest.raises(ValueError, match="参照が不正"):
        large_storage.restore(deepcopy(manifest), indexed_root)
    with pytest.raises(ValueError, match="参照が不正"):
        b"".join(large_storage.export_json(manifest, indexed_root))
