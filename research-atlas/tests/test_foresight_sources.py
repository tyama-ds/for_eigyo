import copy
import json

import httpx
import pytest

from app import foresight_sources as adapter


ASSESSMENT = {"meta": {"start_year": 2025, "end_year": 2025}}
CANDIDATE = {"id": "candidate-1", "label": "Battery interfaces", "original_query": "battery",
             "seed_terms": ["battery"], "terms": ["battery", "ionic transport", "solid state"]}


@pytest.fixture(autouse=True)
def no_live_scopus(monkeypatch):
    for key in ("ELSEVIER_API_KEY", "SCOPUS_API_KEY", "ELSEVIER_INSTTOKEN"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(adapter, "_SCOPUS_INTERVAL", 0)
    monkeypatch.setattr(adapter, "_SCOPUS_LAST_REQUEST", None)


def record(index=0, **changes):
    item = {"eid": f"2-s2.0-{10000 + index}", "dc:identifier": f"SCOPUS_ID:{10000 + index}",
            "dc:title": "Battery interfaces", "prism:coverDate": "2025-06-20",
            "prism:publicationName": "Journal", "prism:doi": f"10.1234/BAT{index}",
            "dc:description": "<p>Battery <b>ionic transport</b>.</p><script>hidden</script>",
            "citedby-count": "0", "authkeywords": "battery | ionic transport; interfaces",
            "affiliation": [{"afid": "123", "affilname": "Synthetic University", "affiliation-city": "Example City"},
                            {"afid": "456", "affilname": "Other Institution"}],
            "author": [{"authid": "12345", "given-name": "Alice", "surname": "Ito", "orcid": "0000-0002-1825-0097", "afid": [{"$": "123"}]},
                       {"authid": "67890", "authname": "Alice Ito"}, {"authname": "Bob Rao", "afid": "456"}],
            "link": [{"@ref": "scopus", "@href": f"https://www.scopus.com/record/display.uri?eid=2-s2.0-{10000 + index}"}]}
    item.update(changes)
    return item


def transport(monkeypatch, handler):
    monkeypatch.setenv("ELSEVIER_API_KEY", "unit-secret-key")
    monkeypatch.setattr(adapter, "_make_scopus_client", lambda: httpx.Client(transport=httpx.MockTransport(handler)))


def test_catalog_configuration_only_and_no_credentials(monkeypatch):
    values = adapter.catalog()
    assert {item["id"] for item in values} == {"europepmc", "arxiv", "crossref", "scopus"}
    assert all(item["available"] for item in values[:3])
    assert values[-1]["available"] is False and "未設定" in values[-1]["reason"]
    monkeypatch.setenv("SCOPUS_API_KEY", "alias-secret")
    assert adapter.catalog()[-1]["available"] is True
    assert "alias-secret" not in json.dumps(adapter.catalog())
    values[0]["name"] = "changed"
    assert adapter.catalog()[0]["name"] == "Europe PMC"


def test_unconfigured_scopus_fails_before_network(monkeypatch):
    monkeypatch.setattr(adapter, "_make_scopus_client", lambda: pytest.fail("unexpected request"))
    with pytest.raises(ValueError, match="未設定"):
        adapter.discover_for_candidate(CANDIDATE, ASSESSMENT, "scopus")


def test_query_keeps_theme_and_treats_text_as_data():
    candidate = {**CANDIDATE, "terms": ["ionic transport", 'bad") OR ALL(*): test', "battery"]}
    before = copy.deepcopy(candidate)
    plan = adapter.query_for_candidate(candidate, ASSESSMENT)
    assert plan["queries"]["europepmc"].startswith('("battery") AND ("ionic transport" OR ')
    assert plan["queries"]["arxiv"].startswith('(all:"battery") AND (all:"ionic transport"')
    assert plan["scopus_query"].startswith('TITLE-ABS-KEY("battery") AND TITLE-ABS-KEY(')
    assert plan["scopus_query"].endswith("AND PUBYEAR > 2024 AND PUBYEAR < 2026")
    assert "ALL(*)" not in plan["scopus_query"] and candidate == before
    assert adapter.query_for_candidate({"keywords": ["organoid"]}, ASSESSMENT)["seed_terms"] == ["organoid"]


@pytest.mark.parametrize("changes,limit,provider", [({}, 0, "crossref"), ({}, 101, "crossref"), ({}, True, "crossref"),
    ({}, 2, "unknown"), ({"meta": {}}, 2, "crossref"), ({"meta": {"start_year": 2000, "end_year": 2025}}, 2, "crossref"),
    ({"meta": {"start_year": 2025, "end_year": 2200}}, 2, "crossref")])
def test_invalid_requests_fail_without_network(changes, limit, provider, monkeypatch):
    monkeypatch.setattr(adapter.sources, "discover", lambda *args, **kwargs: pytest.fail("network"))
    with pytest.raises(ValueError):
        adapter.discover_for_candidate(CANDIDATE, changes or ASSESSMENT, provider, limit)
    with pytest.raises(ValueError, match="研究テーマ"):
        adapter.query_for_candidate({}, ASSESSMENT)


def test_public_reuse_trace_and_input_immutability(monkeypatch):
    observed = {}
    paper = {"id": "europepmc:MED:1", "title": "Battery research", "year": 2025, "date_precision": "year", "citations": None, "citation_history": {}}
    original_report = {"warnings": ["Existing warning"], "year_coverage": [{"year": 2025, "total": 100, "imported": 1, "allocated": 4}], "retrieved_count": 1}
    def discover(*args, **kwargs):
        observed.update(args=args, kwargs=kwargs)
        return [paper], original_report
    monkeypatch.setattr(adapter.sources, "discover", discover)
    messages = []
    papers, report = adapter.discover_for_candidate(CANDIDATE, ASSESSMENT, "europepmc", 4, messages.append)
    assert observed["args"] == ("europepmc", '("battery") AND ("ionic transport" OR "solid state")', 2025, 2025, 4)
    assert observed["kwargs"]["progress"] == messages.append
    assert report["annual_comparison_justified"] is False and report["status"] == "completed"
    assert "年次比較" in " ".join(report["warnings"])
    assert report["scopus_query"] and report["collection_purpose"] == "candidate_refinement"
    assert papers[0]["citations"] is None and papers[0]["citation_history"] == {}
    report["year_coverage"][0]["total"] = 99
    papers[0]["title"] = "mutated"
    assert original_report["year_coverage"][0]["total"] == 100 and paper["title"] == "Battery research"


def test_crossref_filters_off_theme_and_preserves_retrieved_counts(monkeypatch):
    papers = [{"id": str(i), "title": title, "abstract": "", "keywords": [], "year": 2025, "citations": None}
              for i, title in enumerate(["Battery interfaces", "Quantum computing", "solid-state battery"])]
    monkeypatch.setattr(adapter.sources, "discover", lambda *args, **kwargs: (papers,
        {"retrieved_count": 3, "year_coverage": [{"year": 2025, "total": 200, "imported": 3, "allocated": 3}], "warnings": []}))
    result, report = adapter.discover_for_candidate(CANDIDATE, ASSESSMENT, "crossref", 3)
    assert len(result) == 2 and report["filtered_off_theme"] == 1
    assert report["retrieved_count"] == 3 and report["imported_count"] == 2
    assert report["year_coverage"][0]["retrieved_before_theme_filter"] == 3
    assert report["year_coverage"][0]["imported"] == 2
    assert len(papers) == 3 and report["truncated"] is True
    assert not adapter._contains_seed({"title": "training", "abstract": "", "keywords": []}, ["AI"])
    assert adapter._contains_seed({"title": "人工知能による予測", "keywords": []}, ["人工知能"])


def test_public_empty_is_distinct_from_failure(monkeypatch):
    def empty(*args, **kwargs):
        raise ValueError(adapter._EMPTY_PUBLIC + "条件を変更してください。")
    monkeypatch.setattr(adapter.sources, "discover", empty)
    papers, report = adapter.discover_for_candidate(CANDIDATE, ASSESSMENT, "arxiv", 2)
    assert papers == [] and report["status"] == "no_results"
    assert report["year_coverage"] == [] and report["truncated"] is None
    def failure(*args, **kwargs):
        raise RuntimeError("公開 API はタイムアウトしました。")
    monkeypatch.setattr(adapter.sources, "discover", failure)
    with pytest.raises(RuntimeError, match="タイムアウト"):
        adapter.discover_for_candidate(CANDIDATE, ASSESSMENT, "arxiv", 2)


def test_scopus_schema_missingness_and_headers_only(monkeypatch):
    requests = []
    def handler(request):
        requests.append(request)
        assert request.url.host == "api.elsevier.com" and request.url.path == "/content/search/scopus"
        assert request.headers["X-ELS-APIKey"] == "unit-secret-key"
        assert request.headers["X-ELS-Insttoken"] == "unit-secret-inst"
        assert "unit-secret" not in str(request.url)
        assert request.url.params["view"] == "COMPLETE" and request.url.params["content"] == "core"
        return httpx.Response(200, json={"search-results": {"opensearch:totalResults": "2", "entry": [record(), record(1, **{"citedby-count": None, "dc:description": None, "author": None, "affiliation": None})]}})
    transport(monkeypatch, handler)
    monkeypatch.setenv("ELSEVIER_INSTTOKEN", "unit-secret-inst")
    papers, report = adapter.discover_for_candidate(CANDIDATE, ASSESSMENT, "scopus", 2)
    assert len(requests) == 1 and len(papers) == 2
    first, missing = papers
    assert first["id"] == "2-s2.0-10000" and first["doi"] == "10.1234/bat0"
    assert first["abstract"] == "Battery ionic transport." and first["citations"] == 0
    assert first["citation_snapshots"][0]["count"] == 0
    assert first["authors"][0]["id"] == "scopus:12345"
    assert first["authors"][0]["aliases"] == ["orcid:0000-0002-1825-0097"]
    assert first["authors"][0]["affiliations"] == ["Synthetic University, Example City"]
    assert first["authors"][1]["id"] == "scopus:67890" and "affiliations" not in first["authors"][1]
    assert first["authors"][2]["affiliations"] == ["Other Institution"]
    assert missing["abstract"] == "" and missing["citations"] is None and missing["authors"] == []
    assert missing["citation_snapshots"] == [] and missing["references"] == []
    assert all(p["citation_history"] == {} and p["references_status"] == "not_provided" for p in papers)
    assert report["year_coverage"] == [{"year": 2025, "total": 2, "imported": 2, "allocated": 2}]
    assert "unit-secret" not in json.dumps([papers, report], ensure_ascii=False)


def test_scopus_complete_entitlement_fallback_is_explicit(monkeypatch):
    views = []
    def handler(request):
        views.append(request.url.params["view"])
        if views[-1] == "COMPLETE":
            return httpx.Response(403, json={"error": "sensitive body unit-secret-key"})
        return httpx.Response(200, json={"search-results": {"opensearch:totalResults": "1", "entry": [record(author=None, **{"dc:creator": "Alice Ito", "dc:description": None, "authkeywords": None})]}})
    transport(monkeypatch, handler)
    papers, report = adapter.discover_for_candidate(CANDIDATE, ASSESSMENT, "scopus", 1)
    assert views == ["COMPLETE", "STANDARD"] and report["view"] == "STANDARD"
    assert papers[0]["author_metadata_scope"] == "first_author_only"
    assert papers[0]["abstract"] == "" and papers[0]["keywords"] == []
    assert "STANDARD" in " ".join(report["warnings"]) and "筆頭著者" in " ".join(report["warnings"])


@pytest.mark.parametrize("status,expected", [(401, "認証"), (403, "認証"), (429, "上限"), (500, "HTTP 500"), (400, "検索式")])
def test_scopus_errors_do_not_expose_response_or_key(monkeypatch, status, expected):
    transport(monkeypatch, lambda request: httpx.Response(status, text="unit-secret-key server traceback"))
    with pytest.raises((RuntimeError, ValueError), match=expected) as exc:
        adapter.discover_for_candidate(CANDIDATE, ASSESSMENT, "scopus", 1)
    assert "unit-secret" not in str(exc.value) and "traceback" not in str(exc.value)


def test_scopus_bad_json_and_timeout_are_sanitized(monkeypatch):
    transport(monkeypatch, lambda request: httpx.Response(200, text="unit-secret-key"))
    with pytest.raises(RuntimeError, match="読み取れません"):
        adapter.discover_for_candidate(CANDIDATE, ASSESSMENT, "scopus", 1)
    def timeout(request):
        raise httpx.ReadTimeout("unit-secret-key", request=request)
    transport(monkeypatch, timeout)
    with pytest.raises(RuntimeError, match="タイムアウト") as exc:
        adapter.discover_for_candidate(CANDIDATE, ASSESSMENT, "scopus", 1)
    assert "unit-secret" not in str(exc.value)


def test_scopus_pages_never_exceed_request_limit_and_empty_is_known(monkeypatch):
    pages = []
    def handler(request):
        offset, count = int(request.url.params["start"]), int(request.url.params["count"])
        pages.append((offset, count))
        return httpx.Response(200, json={"search-results": {"opensearch:totalResults": "200", "entry": [record(i) for i in range(offset, offset + count)]}})
    transport(monkeypatch, handler)
    papers, report = adapter.discover_for_candidate(CANDIDATE, ASSESSMENT, "scopus", 40)
    assert pages == [(0, 25), (25, 15)] and len(papers) == 40
    assert report["truncated"] is True and report["retrieved_count"] == 40
    transport(monkeypatch, lambda request: httpx.Response(200, json={"search-results": {"opensearch:totalResults": "0", "entry": [{"error": "RESULTS_NOT_FOUND"}]}}))
    papers, report = adapter.discover_for_candidate(CANDIDATE, ASSESSMENT, "scopus", 2)
    assert papers == [] and report["status"] == "no_results"
    assert report["year_coverage"][0]["total"] == 0 and report["truncated"] is False


def test_scopus_zero_allocation_is_unknown_not_zero(monkeypatch):
    transport(monkeypatch, lambda request: httpx.Response(200, json={"search-results": {"opensearch:totalResults": "0", "entry": []}}))
    _, report = adapter.discover_for_candidate(CANDIDATE, {"meta": {"start_year": 2024, "end_year": 2025}}, "scopus", 1)
    assert report["year_coverage"][1] == {"year": 2025, "total": None, "imported": 0, "allocated": 0}
    assert report["truncated"] is True and len(report["executed_queries"]) == 1


def test_scopus_malformed_metadata_aborts_instead_of_partial_success(monkeypatch):
    transport(monkeypatch, lambda request: httpx.Response(200, json={"search-results": {"opensearch:totalResults": "2", "entry": [record(), record(1, **{"prism:coverDate": "2024-01-01"})]}}))
    with pytest.raises(RuntimeError, match="検索年"):
        adapter.discover_for_candidate(CANDIDATE, ASSESSMENT, "scopus", 2)


def initial_scope(query, **extra):
    return {"meta": {"start_year": 2025, "end_year": 2025, "source_reports": [
        {"provider": "europepmc", "query": query, "discovery_request": {"query": query}, **extra}]}}


def test_original_public_query_is_mandatory_even_when_topic_words_are_generic():
    assessment = initial_scope("stainless steel tensile strength")
    candidate = {"seed_terms": ["steel", "strength", "stainless"], "terms": ["treatment temperature", "concrete beams"]}
    before = copy.deepcopy(assessment)
    plan = adapter.query_for_candidate(candidate, assessment)
    assert plan["domain_anchor_applied"] is True
    assert plan["original_search_queries"] == ["stainless steel tensile strength"]
    assert plan["domain_anchor_policy"] == "initial_queries_union_then_topic_intersection"
    ep_query = plan["queries"]["europepmc"]
    assert '"stainless" AND "steel"' in ep_query and 'AND "tensile"' in ep_query and 'AND "strength"' in ep_query
    assert ep_query.index('"stainless"') < ep_query.index('"concrete beams"')
    assert 'all:"stainless" AND all:"steel"' in plan["queries"]["arxiv"]
    assert plan["scopus_query"].startswith('TITLE-ABS-KEY(') and 'AND "tensile"' in plan["scopus_query"]
    assert assessment == before


def test_domain_anchor_uses_initial_request_and_never_round_discovery():
    assessment = initial_scope("original subject")
    report = assessment["meta"]["source_reports"][0]
    report["query"] = "stale display value"
    assessment["discovery_reports"] = [{"query": "recursive generic strength"}]
    assessment["meta"]["source_reports"].append({"query": "adaptive search", "collection_purpose": "candidate_refinement"})
    plan = adapter.query_for_candidate(CANDIDATE, assessment)
    assert plan["original_search_queries"] == ["original subject"]
    assert "adaptive" not in plan["queries"]["europepmc"] and "stale" not in plan["queries"]["europepmc"]


def test_original_boolean_phrases_and_multiple_collection_scopes_keep_union():
    assessment = initial_scope('("lithium battery" OR "sodium battery") AND recycling')
    assessment["meta"]["source_reports"].append({"provider": "crossref", "query": "battery recovery"})
    plan = adapter.query_for_candidate(CANDIDATE, assessment)
    assert '("lithium battery" OR "sodium battery") AND "recycling"' in plan["queries"]["europepmc"]
    assert ' OR ("battery" AND "recovery")' in plan["queries"]["europepmc"]
    tree = adapter._domain_tree(assessment["meta"]["source_reports"][0]["query"])
    assert adapter._domain_matches({"title": "Recycling sodium battery", "keywords": []}, tree)
    assert not adapter._domain_matches({"title": "Lithium battery performance", "keywords": []}, tree)


@pytest.mark.parametrize("query", ['TITLE-ABS-KEY(steel)', 'steel NOT concrete', 'all:steel', 'steel*',
    'steel AND', 'steel OR (concrete', 'steel" OR ALL(*)', '"unclosed phrase'])
def test_untranslatable_domain_syntax_fails_without_silently_widening(monkeypatch, query):
    monkeypatch.setattr(adapter.sources, "discover", lambda *args, **kwargs: pytest.fail("network should not be called"))
    with pytest.raises(ValueError, match="元の公開検索式"):
        adapter.discover_for_candidate(CANDIDATE, initial_scope(query), "europepmc", 2)


def test_crossref_rejects_generic_steel_beams_outside_initial_domain(monkeypatch):
    papers = [
        {"id": "1", "title": "Stainless steel tensile strength under heat treatment", "abstract": "", "keywords": [], "year": 2025},
        {"id": "2", "title": "Seismic strength of steel reinforced concrete beams", "abstract": "Treatment temperature", "keywords": [], "year": 2025},
        {"id": "3", "title": "Tensile strength of carbon steel", "abstract": "Stainless coating was also tested.", "keywords": [], "year": 2025},
    ]
    original_report = {"retrieved_count": 3, "year_coverage": [{"year": 2025, "total": 1000, "imported": 3, "allocated": 3}], "warnings": []}
    monkeypatch.setattr(adapter.sources, "discover", lambda *args, **kwargs: (papers, original_report))
    candidate = {"seed_terms": ["steel", "strength"], "terms": ["treatment temperature"]}
    result, report = adapter.discover_for_candidate(candidate, initial_scope("stainless steel tensile strength"), "crossref", 3)
    assert [paper["id"] for paper in result] == ["1", "3"]
    assert report["filtered_off_theme"] == report["filtered_outside_initial_scope"] == 1
    assert report["retrieved_count"] == 3 and report["imported_count"] == 2
    assert report["domain_anchor_applied"] is True and report["original_search_queries"] == ["stainless steel tensile strength"]
    assert original_report["year_coverage"][0]["imported"] == 3


def test_scopus_executes_anchored_scope_not_just_preview(monkeypatch):
    requests = []
    def handler(request):
        requests.append(request.url.params["query"])
        return httpx.Response(200, json={"search-results": {"opensearch:totalResults": "0", "entry": []}})
    transport(monkeypatch, handler)
    _, report = adapter.discover_for_candidate(CANDIDATE, initial_scope("battery recycling"), "scopus", 1)
    assert '"battery" AND "recycling"' in requests[0]
    assert report["domain_anchor_applied"] is True and report["status"] == "no_results"


@pytest.mark.parametrize("provider", ["europepmc", "arxiv", "crossref", "scopus"])
def test_every_provider_checks_visible_initial_domain_not_fulltext_mentions(monkeypatch, provider):
    papers = [{"id": str(index), "title": title, "abstract": abstract, "keywords": [], "year": 2025}
              for index, (title, abstract) in enumerate([
                  ("Stainless steel tensile strength under heat treatment", "A measured experiment."),
                  ("Mechanical properties of TiC titanium composites", "High tensile strength of titanium alloy."),
                  ("Stainless steel mechanical testing", "UTS was measured under tensile testing."),
                  ("Heat treatment", "Stainless steel was prepared. Its tensile strength was measured.")])]
    original_report = {"retrieved_count": 4, "year_coverage": [{"year": 2025, "total": 1000, "imported": 4, "allocated": 4}], "warnings": []}
    before = copy.deepcopy([papers, original_report])
    if provider == "scopus":
        monkeypatch.setattr(adapter, "_scopus_discover", lambda *args, **kwargs: (papers, original_report))
    else:
        monkeypatch.setattr(adapter.sources, "discover", lambda *args, **kwargs: (papers, original_report))
    result, report = adapter.discover_for_candidate({"seed_terms": ["steel", "strength"], "terms": ["heat treatment"]},
                                                   initial_scope("stainless steel tensile strength"), provider, 4)
    assert [paper["id"] for paper in result] == ["0", "3"]
    assert report["retrieved_count"] == 4 and report["imported_count"] == 2
    assert report["filtered_outside_initial_scope"] == report["filtered_off_theme"] == 2
    assert report["year_coverage"][0] == {"year": 2025, "total": 1000, "imported": 2, "allocated": 4, "retrieved_before_theme_filter": 4}
    assert report["visible_scope_verification"]["applied"] is True
    assert report["visible_scope_verification"]["checked_count"] == 4
    assert report["visible_scope_verification"]["passed_count"] == 2
    assert "API の検索一致と可視書誌の一致は別" in " ".join(report["warnings"])
    assert "同義語・略語" in " ".join(report["warnings"])
    assert [papers, original_report] == before


def test_visible_domain_filter_can_finish_with_zero_adopted_and_known_retrieval(monkeypatch):
    paper = {"id": "ti", "title": "Titanium strength", "abstract": "", "keywords": [], "year": 2025}
    monkeypatch.setattr(adapter.sources, "discover", lambda *args, **kwargs: ([paper],
        {"retrieved_count": 1, "warnings": [], "year_coverage": [{"year": 2025, "total": 12, "allocated": 1, "imported": 1}]}))
    result, report = adapter.discover_for_candidate({"seed_terms": ["strength"]}, initial_scope("stainless steel"), "europepmc", 1)
    assert result == [] and report["status"] == "no_results"
    assert report["retrieved_count"] == 1 and report["imported_count"] == 0
    assert report["year_coverage"][0]["total"] == 12 and report["year_coverage"][0]["imported"] == 0
    assert report["visible_scope_verification"]["rejected_count"] == 1
