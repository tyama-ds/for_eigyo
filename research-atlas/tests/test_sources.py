from concurrent.futures import ThreadPoolExecutor
from html import escape
from datetime import date

import httpx
import pytest

from app import sources


def transport(monkeypatch, handler):
    mock = httpx.MockTransport(handler)
    monkeypatch.setattr(sources, "_make_client", lambda *args: httpx.Client(transport=mock))


def ep_record(year, index=0, **changes):
    result = {"id": f"{year}{index}", "source": "MED", "title": f"Battery interfaces {year} {index}",
              "firstPublicationDate": f"{year}-06-01", "pubYear": str(year),
              "abstractText": "<h4>Results</h4><p>Stable <i>ionic</i> transport &amp; storage.</p>",
              "authorList": {"author": [{"firstName": "Jane", "lastName": "Aster", "authorId": {"type": "ORCID", "value": "0000-0002-1825-0097"}}, {"firstName": "John", "lastName": "Boreal"}]},
              "citedByCount": 0, "doi": f"10.1234/{year}.{index}", "journalInfo": {"journal": {"title": "Journal"}}, "keywordList": {"keyword": ["battery", "ionic transport"]}}
    result.update(changes)
    return result


def crossref_record(year=2025, **changes):
    result = {"DOI": "10.1234/ABC", "title": ["Battery interfaces"], "published": {"date-parts": [[year, 4]]},
              "author": [{"given": "Jane", "family": "Aster", "ORCID": "https://orcid.org/0000-0002-1825-0097"}, {"given": "John", "family": "Boreal"}],
              "container-title": ["Journal"], "is-referenced-by-count": 8, "subject": ["Materials"]}
    result.update(changes)
    return result


def atom(year=2025, ids=None, total=None):
    identifiers = ids or [f"{str(year)[2:]}01.12345v2"]
    entries = "".join(f'''<entry><id>http://arxiv.org/abs/{escape(identifier)}</id><title>Quantum
    sensing</title><published>{year}-01-05T00:00:00Z</published><updated>{year}-06-10T00:00:00Z</updated>
    <summary>Quantum spin sensing &amp; measurement.</summary><author><name>John Boreal</name></author>
    <category term="quant-ph"/><arxiv:doi>10.1234/arxiv.{year}</arxiv:doi></entry>''' for identifier in identifiers)
    return f'''<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom" xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/">
    <opensearch:totalResults> {len(identifiers) if total is None else total} </opensearch:totalResults>{entries}</feed>'''


def test_catalog_returns_copy_with_documented_providers():
    catalog = sources.catalog()
    assert {item["id"] for item in catalog} == {"europepmc", "arxiv", "crossref"}
    assert all(item["docs_url"].startswith("https://") and item["limitations"] for item in catalog)
    catalog[0]["name"] = "mutated"
    assert sources.catalog()[0]["name"] == "Europe PMC"


def test_europepmc_per_year_budget_and_missingness(monkeypatch):
    requested, messages = [], []
    def handler(request):
        assert request.url.host == "www.ebi.ac.uk"
        assert request.url.params["resultType"] == "core"
        year = int(request.url.params["query"].split("FIRST_PDATE:[")[1][:4])
        count = int(request.url.params["pageSize"])
        requested.append((year, count))
        records = [ep_record(year, i, citedByCount=None if i else 0) for i in range(count)]
        return httpx.Response(200, json={"hitCount": 100, "resultList": {"result": records}})
    transport(monkeypatch, handler)
    papers, report = sources.discover("europepmc", "battery", 2023, 2025, 5, progress=messages.append)
    assert requested == [(2023, 2), (2024, 2), (2025, 1)]
    assert len(papers) == 5 and report["imported_count"] == 5
    assert report["year_coverage"] == [{"year": y, "total": 100, "imported": n, "allocated": n} for y, n in requested]
    assert report["truncated"] is True
    assert "母集団" in " ".join(report["warnings"])
    assert "同姓同名" in " ".join(report["warnings"])
    assert all(p["citation_history"] == {} for p in papers)
    assert papers[0]["citations"] == 0 and papers[1]["citations"] is None
    assert papers[0]["citation_snapshots"][0]["count"] == 0
    assert papers[1]["citation_snapshots"] == []
    assert papers[0]["authors"][0]["id"] == "orcid:0000-0002-1825-0097"
    assert papers[0]["authors"][1]["id"].startswith("name:")
    assert papers[0]["providers"] == ["europepmc"]
    assert papers[0]["citation_source"] == "europepmc"
    assert "<" not in papers[0]["abstract"] and "&" in papers[0]["abstract"]
    assert len(messages) == 4


def test_crossref_missing_abstract_zero_citations_and_common_author_ids(monkeypatch):
    def handler(request):
        assert request.url.host == "api.crossref.org"
        assert request.url.params["filter"] == "from-pub-date:2025-01-01,until-pub-date:2025-12-31"
        assert request.url.params["sort"] == "relevance"
        return httpx.Response(200, json={"message": {"total-results": 1, "items": [crossref_record(**{"is-referenced-by-count": None})]}})
    transport(monkeypatch, handler)
    papers, report = sources.discover("crossref", "battery", 2025, 2025, 1)
    assert papers[0]["abstract"] == ""
    assert papers[0]["citations"] is None
    assert papers[0]["doi"] == "10.1234/abc"
    assert papers[0]["id"] == "crossref:10.1234/abc"
    assert papers[0]["authors"][1] == sources._author("John Boreal")
    assert papers[0]["external_url"] == "https://doi.org/10.1234/abc"
    assert report["truncated"] is False
    assert any("抄録" in warning for warning in report["warnings"])


def test_jats_html_abstract_is_text_and_script_body_is_removed(monkeypatch):
    record = crossref_record(abstract='<jats:p>Strong <jats:italic>result</jats:italic>.</jats:p><script>private_script()</script><p>Next &amp; final.</p>')
    transport(monkeypatch, lambda request: httpx.Response(200, json={"message": {"total-results": 1, "items": [record]}}))
    papers, _ = sources.discover("crossref", "battery", 2025, 2025, 1)
    assert papers[0]["abstract"] == "Strong result. Next & final."
    assert "private_script" not in papers[0]["abstract"]


def test_arxiv_versions_collapsed_and_no_citations_fabricated(monkeypatch):
    monkeypatch.setattr(sources, "_ARXIV_LAST_REQUEST", None)
    def handler(request):
        assert request.url.host == "export.arxiv.org"
        assert 'all:"quantum" AND all:"sensing"' in request.url.params["search_query"]
        assert "submittedDate:[202501010000 TO 202512312359]" in request.url.params["search_query"]
        return httpx.Response(200, text=atom(ids=["2501.12345v1", "2501.12345v2"], total=20))
    transport(monkeypatch, handler)
    papers, report = sources.discover("arxiv", "quantum sensing", 2025, 2025, 2)
    assert len(papers) == 1
    assert papers[0]["id"] == "arxiv:2501.12345"
    assert papers[0]["external_url"] == "https://arxiv.org/abs/2501.12345"
    assert papers[0]["year"] == 2025
    assert papers[0]["title"] == "Quantum sensing"
    assert papers[0]["citations"] is None and papers[0]["citation_history"] == {}
    assert papers[0]["citation_snapshots"] == []
    assert papers[0]["keywords"] == ["quant-ph"]
    assert report["duplicates_removed"] == 1


def test_arxiv_global_lock_enforces_three_second_interval_across_jobs(monkeypatch):
    clock = {"now": 100.0}
    request_times = []
    monkeypatch.setattr(sources, "_ARXIV_LAST_REQUEST", None)
    monkeypatch.setattr(sources.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(sources.time, "sleep", lambda seconds: clock.update(now=clock["now"] + seconds))
    def handler(request):
        request_times.append(clock["now"])
        return httpx.Response(200, text=atom())
    transport(monkeypatch, handler)
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(sources.discover, "arxiv", "quantum", 2025, 2025, 1) for _ in range(2)]
        assert all(future.result()[0] for future in futures)
    assert request_times == [100.0, 103.0]


def test_zero_budget_year_still_has_reported_total_and_never_exceeds_limit(monkeypatch):
    def handler(request):
        year = int(request.url.params["query"].split("FIRST_PDATE:[")[1][:4])
        return httpx.Response(200, json={"hitCount": 10, "resultList": {"result": [ep_record(year)]}})
    transport(monkeypatch, handler)
    papers, report = sources.discover("europepmc", "battery", 2024, 2025, 1)
    assert len(papers) == 1
    assert report["year_coverage"][1] == {"year": 2025, "total": 10, "imported": 0, "allocated": 0}


@pytest.mark.parametrize("status,error_type", [(429, RuntimeError), (503, RuntimeError), (302, RuntimeError), (400, ValueError)])
def test_http_errors_do_not_expose_response_or_query(monkeypatch, status, error_type):
    transport(monkeypatch, lambda request: httpx.Response(status, text="private-server-body", headers={"Location": "https://untrusted.example"}))
    with pytest.raises(error_type) as error:
        sources.discover("crossref", "private-query", 2025, 2025, 1)
    assert "private" not in str(error.value)


def test_failed_later_year_raises_instead_of_returning_partial_success(monkeypatch):
    def handler(request):
        if "2025" in request.url.params["filter"]:
            raise httpx.ReadTimeout("private upstream details", request=request)
        return httpx.Response(200, json={"message": {"total-results": 1, "items": [crossref_record(2024)]}})
    transport(monkeypatch, handler)
    with pytest.raises(RuntimeError, match="取得途中の結果は保存していません") as error:
        sources.discover("crossref", "private query", 2024, 2025, 2)
    assert "private" not in str(error.value)


@pytest.mark.parametrize("content", ["broken XML", '<!DOCTYPE feed [<!ENTITY x "unsafe">]><feed/>', '<feed xmlns="http://www.w3.org/2005/Atom"><entry><id>http://arxiv.org/api/errors</id></entry></feed>'])
def test_arxiv_malformed_or_error_feed_is_rejected(monkeypatch, content):
    monkeypatch.setattr(sources, "_ARXIV_LAST_REQUEST", None)
    transport(monkeypatch, lambda request: httpx.Response(200, text=content))
    with pytest.raises((ValueError, RuntimeError)):
        sources.discover("arxiv", "quantum", 2025, 2025, 1)


def test_wrong_year_response_rejected_instead_of_contaminating_cohort(monkeypatch):
    transport(monkeypatch, lambda request: httpx.Response(200, json={"message": {"total-results": 1, "items": [crossref_record(2024)]}}))
    with pytest.raises(RuntimeError, match="検索年と異なる"):
        sources.discover("crossref", "battery", 2025, 2025, 1)


def test_empty_search_readable_and_no_network_for_invalid_options(monkeypatch):
    transport(monkeypatch, lambda request: httpx.Response(200, json={"message": {"total-results": 0, "items": []}}))
    with pytest.raises(ValueError, match="見つかりません"):
        sources.discover("crossref", "nonexistent", 2025, 2025, 1)
    monkeypatch.setattr(sources, "_make_client", lambda *args: pytest.fail("Network must not be called"))
    for args in [("unknown", "x", 2025, 2025, 1), ("crossref", "", 2025, 2025, 1), ("crossref", "x", 2000, 2025, 1), ("crossref", "x", 2025, 2024, 1), ("crossref", "x", 2025, 2025, 1001), ("crossref", "x", 2025, 2025, True)]:
        with pytest.raises(ValueError):
            sources.discover(*args)


def test_non_json_and_malformed_metadata_rejected(monkeypatch):
    transport(monkeypatch, lambda request: httpx.Response(200, text="private-html-page"))
    with pytest.raises(RuntimeError, match="応答形式"):
        sources.discover("crossref", "battery", 2025, 2025, 1)
    transport(monkeypatch, lambda request: httpx.Response(200, json={"message": {"total-results": 1, "items": [crossref_record(title=[])]}}))
    with pytest.raises(RuntimeError, match="メタデータ"):
        sources.discover("crossref", "battery", 2025, 2025, 1)


@pytest.mark.parametrize("parts,expected,precision", [([2025], "", "year"), ([2025, 4], "2025-04", "month"), ([2025, 4, 12], "2025-04-12", "day")])
def test_crossref_date_parts_precision_is_preserved(monkeypatch, parts, expected, precision):
    record = crossref_record(published={"date-parts": [parts]})
    transport(monkeypatch, lambda request: httpx.Response(200, json={"message": {"total-results": 1, "items": [record]}}))
    papers, report = sources.discover("crossref", "battery", 2025, 2025, 1)
    assert papers[0]["publication_date"] == expected and papers[0]["date_precision"] == precision
    assert papers[0]["date_source"] == "crossref:published"
    assert report["month_coverage"] == [] and report["start_month"] is None
    assert report["date_pipeline_version"] == 2


def test_crossref_issued_fallback_keeps_date_source(monkeypatch):
    record = crossref_record(published=None, issued={"date-parts": [[2025, 4]]})
    transport(monkeypatch, lambda request: httpx.Response(200, json={"message": {"total-results": 1, "items": [record]}}))
    papers, _ = sources.discover("crossref", "battery", 2025, 2025, 1)
    assert papers[0]["publication_date"] == "2025-04" and papers[0]["date_source"] == "crossref:issued"


def test_europepmc_monthly_budget_leap_day_and_year_aggregation(monkeypatch):
    requests = []
    def handler(request):
        value = request.url.params["query"].split("FIRST_PDATE:[")[1].split("]")[0]
        first, last = value.split(" TO ")
        requests.append((first, last, int(request.url.params["pageSize"])))
        count = int(request.url.params["pageSize"])
        records = [ep_record(2024, i, id=f"{first}-{i}", firstPublicationDate=first) for i in range(count)]
        return httpx.Response(200, json={"hitCount": 100, "resultList": {"result": records}})
    transport(monkeypatch, handler)
    papers, report = sources.discover("europepmc", "battery", 2024, 2024, 3, start_month="2024-02", end_month="2024-03")
    assert requests == [("2024-02-01", "2024-02-29", 2), ("2024-03-01", "2024-03-31", 1)]
    assert report["month_coverage"] == [{"month": "2024-02", "total": 100, "imported": 2, "allocated": 2}, {"month": "2024-03", "total": 100, "imported": 1, "allocated": 1}]
    assert report["year_coverage"] == [{"year": 2024, "total": 200, "imported": 3, "allocated": 3}]
    assert report["sampling"] == "per_month_relevance_top" and report["date_usable_count"] == 3
    assert any("母集団の月次増加率" in warning for warning in report["warnings"])
    assert "補完" in papers[0]["date_source"]
    assert papers[0]["publication_date"] == "2024-02-01" and papers[0]["date_precision"] == "day"


def test_crossref_month_mode_spans_year_boundary(monkeypatch):
    requested = []
    def handler(request):
        value = request.url.params["filter"]
        requested.append(value)
        first = value.split("from-pub-date:")[1].split(",")[0]
        year, month = map(int, first.split("-")[:2])
        record = crossref_record(year, DOI=f"10.1234/{year}.{month}", published={"date-parts": [[year, month]]})
        return httpx.Response(200, json={"message": {"total-results": 1, "items": [record]}})
    transport(monkeypatch, handler)
    papers, report = sources.discover("crossref", "battery", 2024, 2025, 2, start_month="2024-12", end_month="2025-01")
    assert requested == ["from-pub-date:2024-12-01,until-pub-date:2024-12-31", "from-pub-date:2025-01-01,until-pub-date:2025-01-31"]
    assert [p["publication_date"] for p in papers] == ["2024-12", "2025-01"]
    assert [item["year"] for item in report["year_coverage"]] == [2024, 2025]


def test_current_month_is_clipped_to_today_and_marked_partial(monkeypatch):
    class FixedDate(date):
        @classmethod
        def today(cls):
            return cls(2026, 9, 11)
    monkeypatch.setattr(sources, "date", FixedDate)
    def handler(request):
        assert request.url.params["filter"] == "from-pub-date:2026-09-01,until-pub-date:2026-09-11"
        return httpx.Response(200, json={"message": {"total-results": 1, "items": [crossref_record(2026, published={"date-parts": [[2026, 9, 11]]})]}})
    transport(monkeypatch, handler)
    _, report = sources.discover("crossref", "battery", 2026, 2026, 1, start_month="2026-09", end_month="2026-09")
    assert any("月途中" in warning for warning in report["warnings"])


def test_arxiv_month_bounds_and_first_published_date(monkeypatch):
    monkeypatch.setattr(sources, "_ARXIV_LAST_REQUEST", None)
    def handler(request):
        assert "submittedDate:[202402010000 TO 202402292359]" in request.url.params["search_query"]
        return httpx.Response(200, text=atom(2024).replace("2024-01-05", "2024-02-11"))
    transport(monkeypatch, handler)
    papers, _ = sources.discover("arxiv", "quantum", 2024, 2024, 1, start_month="2024-02", end_month="2024-02")
    assert papers[0]["publication_date"] == "2024-02-11"
    assert papers[0]["date_precision"] == "day" and "初回投稿日" in papers[0]["date_source"]


@pytest.mark.parametrize("parts", [[2025], [2025, 13], [2025, 4, 12]])
def test_month_query_does_not_invent_or_override_record_month(monkeypatch, parts):
    record = crossref_record(published={"date-parts": [parts]})
    transport(monkeypatch, lambda request: httpx.Response(200, json={"message": {"total-results": 1, "items": [record]}}))
    papers, report = sources.discover("crossref", "battery", 2025, 2025, 1, start_month="2025-03", end_month="2025-03")
    assert papers[0]["publication_date"] == "" and papers[0]["date_precision"] == "year"
    assert papers[0]["year"] == 2025 and report["date_usable_count"] == 0
    assert any("月" in warning for warning in report["warnings"])


@pytest.mark.parametrize("start,end,first_year,last_year", [
    ("2024-01", None, 2024, 2024), ("2024-1", "2024-03", 2024, 2024),
    ("2024-03", "2024-02", 2024, 2024), ("2024-01", "2025-01", 2024, 2024),
    ("2023-01", "2025-01", 2023, 2025), ("2026-10", "2026-10", 2026, 2026),
])
def test_invalid_month_ranges_rejected_before_network(monkeypatch, start, end, first_year, last_year):
    class FixedDate(date):
        @classmethod
        def today(cls):
            return cls(2026, 9, 11)
    monkeypatch.setattr(sources, "date", FixedDate)
    monkeypatch.setattr(sources, "_make_client", lambda *args: pytest.fail("Must validate before network"))
    with pytest.raises(ValueError):
        sources.discover("crossref", "battery", first_year, last_year, 2, start_month=start, end_month=end)
