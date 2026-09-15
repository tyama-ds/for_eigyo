import json

import pytest

from app import large_paging, large_storage


@pytest.fixture
def indexed(tmp_path, monkeypatch):
    monkeypatch.setattr(large_storage, "LARGE_CORPUS_THRESHOLD", 2)
    papers = [
        {"id": "a", "title": "Steel Strength", "abstract": "fracture steel", "year": 2022, "citations": 10, "topic_id": "t1"},
        {"id": "b", "title": "Quantum Computer", "abstract": "qubit", "year": 2024, "citations": 0, "topic_id": "t2"},
        {"id": "c", "title": "Steel Fatigue", "abstract": "fatigue steel", "year": 2024, "citations": 20, "topic_id": "t1"},
        {"id": "d", "title": "Solar Panel", "abstract": "energy", "year": None, "citations": None, "topic_id": "t2"},
        {"id": "e", "title": "Zinc Steel", "abstract": "alloy steel", "year": 2020, "citations": 20, "topic_id": "t1"},
    ]
    manifest = large_storage.externalize({"papers": papers}, tmp_path)
    db = large_storage._database(tmp_path)
    owner = manifest["_large_store"]["owner"]
    large_paging.ensure_owner(db, owner, len(papers))
    yield db, owner, papers
    db.close()


def page(indexed, **options):
    db, owner, papers = indexed
    return large_paging.page(db, owner, len(papers), **{
        "offset": 0, "limit": 2, "query": "", "topic_id": None, "year": None,
        "sort": "year", **options})


@pytest.mark.parametrize("sort,expected", [("year", ["b", "c", "a", "e", "d"]),
                                          ("citations", ["c", "e", "a", "b", "d"]),
                                          ("title", ["b", "d", "c", "a", "e"])])
def test_first_last_pages_follow_complete_order_without_missing_unknown_values(indexed, sort, expected):
    first = page(indexed, sort=sort)
    last = page(indexed, sort=sort, offset=3)
    assert first["total"] == last["total"] == 5
    assert [p["id"] for p in first["items"]] == expected[:2]
    assert [p["id"] for p in last["items"]] == expected[3:]
    assert page(indexed, sort=sort, offset=100)["items"] == []


def test_combined_text_topic_year_filters_and_literal_query(indexed):
    result = page(indexed, query="STEEL", topic_id="t1", year=2024)
    assert result["total"] == 1
    assert result["items"][0]["id"] == "c"
    assert page(indexed, query="%' OR 1=1 --")["total"] == 0
    assert page(indexed, query="grain")["items"] == []


def test_canonical_author_filter_is_an_exact_paper_subset(indexed):
    db, _, _ = indexed
    db.execute("CREATE TEMP TABLE canonical_author_test(author TEXT,paper_id TEXT)")
    db.executemany("INSERT INTO canonical_author_test VALUES (?,?)", [("author1", "a"), ("author1", "c")])
    result = page(indexed, author_filter=("SELECT paper_id FROM canonical_author_test WHERE author=?", ["author1"]))
    assert result["total"] == 2
    assert [p["id"] for p in result["items"]] == ["c", "a"]
    assert page(indexed, author_filter=("SELECT paper_id FROM canonical_author_test WHERE author=?", ["unknown"]))["total"] == 0


def test_only_requested_payloads_are_read_and_unfiltered_count_uses_manifest(indexed):
    db, _, _ = indexed
    statements = []
    db.set_trace_callback(statements.append)
    result = page(indexed, sort="citations", limit=1)
    assert result["items"][0]["id"] == "c"
    assert not any("SELECT COUNT(*)" in statement for statement in statements)
    payload_reads = [statement for statement in statements if "SELECT digest,payload" in statement]
    assert len(payload_reads) == 1
    assert "WHERE digest IN (" in payload_reads[0]
    assert "ORDER BY" not in payload_reads[0]


def test_owner_backfill_detects_missing_stored_papers(indexed):
    db, owner, _ = indexed
    with pytest.raises(ValueError, match="論文件数"):
        large_paging.ensure_owner(db, owner, 6)
    # Failed migration is rolled back; the original immutable revision survives.
    assert len(page(indexed, limit=10)["items"]) == 5
