"""ストレージモデルのテスト"""

from for_eigyo.storage.models import Company, SearchResult, AnalysisResult, LeadScore


def test_company_to_dict():
    c = Company(name="テスト株式会社", industry="IT", website="https://example.com")
    d = c.to_dict()
    assert d["name"] == "テスト株式会社"
    assert d["industry"] == "IT"
    assert "raw_data" not in d


def test_search_result_to_dict():
    r = SearchResult(
        query="test",
        title="Result 1",
        url="https://example.com",
        snippet="A test result",
        source="duckduckgo",
    )
    d = r.to_dict()
    assert d["query"] == "test"
    assert "raw_data" not in d


def test_analysis_result_to_dict():
    a = AnalysisResult(
        analysis_type="keywords",
        target="test",
        result={"keywords": ["python", "ai"]},
    )
    d = a.to_dict()
    assert d["analysis_type"] == "keywords"
    assert d["result"]["keywords"] == ["python", "ai"]


def test_lead_score_rank():
    assert LeadScore(company_name="A", score=0.9).rank == "A"
    assert LeadScore(company_name="B", score=0.7).rank == "B"
    assert LeadScore(company_name="C", score=0.5).rank == "C"
    assert LeadScore(company_name="D", score=0.2).rank == "D"
