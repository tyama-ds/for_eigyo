"""データベースのテスト"""

import tempfile
from pathlib import Path

from for_eigyo.storage.database import Database
from for_eigyo.storage.models import Company, SearchResult, AnalysisResult


def _tmp_db():
    return Database(db_path=Path(tempfile.mktemp(suffix=".db")))


def test_upsert_and_search_company():
    db = _tmp_db()
    c = Company(name="テスト株式会社", industry="IT", address="東京都渋谷区")
    db.upsert_company(c)
    df = db.search_companies(name="テスト")
    assert len(df) == 1
    assert df.iloc[0]["name"] == "テスト株式会社"


def test_upsert_companies_batch():
    db = _tmp_db()
    companies = [
        Company(name="A社", industry="IT"),
        Company(name="B社", industry="製造"),
    ]
    count = db.upsert_companies(companies)
    assert count == 2
    df = db.get_all_companies()
    assert len(df) == 2


def test_search_results():
    db = _tmp_db()
    results = [
        SearchResult(query="SaaS", title="Result 1", url="https://a.com", snippet="test", source="ddg"),
        SearchResult(query="SaaS", title="Result 2", url="https://b.com", snippet="test", source="ddg"),
    ]
    db.save_search_results(results)
    df = db.get_search_results(query="SaaS")
    assert len(df) == 2


def test_analysis_results():
    db = _tmp_db()
    a = AnalysisResult(
        analysis_type="keywords",
        target="test_company",
        result={"keywords": ["python"]},
    )
    db.save_analysis(a)
    df = db.get_analyses(analysis_type="keywords")
    assert len(df) == 1


def test_export_csv():
    db = _tmp_db()
    db.upsert_company(Company(name="Export社", industry="テスト"))
    with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as f:
        path = f.name
    count = db.export_companies_csv(path)
    assert count == 1
    assert Path(path).exists()
