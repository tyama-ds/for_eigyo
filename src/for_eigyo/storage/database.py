"""SQLite ストレージ"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pandas as pd

from for_eigyo.storage.models import Company, SearchResult, AnalysisResult

DEFAULT_DB_PATH = Path.home() / ".for_eigyo" / "data.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS companies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    corporate_number TEXT,
    address TEXT,
    industry TEXT,
    employee_count INTEGER,
    capital INTEGER,
    founded TEXT,
    website TEXT,
    phone TEXT,
    email TEXT,
    description TEXT,
    source TEXT,
    raw_data TEXT,
    collected_at TEXT NOT NULL,
    UNIQUE(name, corporate_number)
);

CREATE TABLE IF NOT EXISTS search_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    query TEXT NOT NULL,
    title TEXT NOT NULL,
    url TEXT NOT NULL,
    snippet TEXT,
    source TEXT NOT NULL,
    raw_data TEXT,
    collected_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS analysis_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    analysis_type TEXT NOT NULL,
    target TEXT NOT NULL,
    result TEXT,
    parameters TEXT,
    analyzed_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_companies_name ON companies(name);
CREATE INDEX IF NOT EXISTS idx_companies_industry ON companies(industry);
CREATE INDEX IF NOT EXISTS idx_search_results_query ON search_results(query);
CREATE INDEX IF NOT EXISTS idx_analysis_type ON analysis_results(analysis_type);
"""


class Database:
    """SQLite ベースのストレージ"""

    def __init__(self, db_path: str | Path | None = None):
        self.db_path = Path(db_path) if db_path else DEFAULT_DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self):
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    # ── Companies ──

    def upsert_company(self, company: Company) -> int:
        with self._connect() as conn:
            cursor = conn.execute(
                """INSERT INTO companies
                   (name, corporate_number, address, industry, employee_count,
                    capital, founded, website, phone, email, description,
                    source, raw_data, collected_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(name, corporate_number) DO UPDATE SET
                    address=excluded.address,
                    industry=excluded.industry,
                    employee_count=excluded.employee_count,
                    capital=excluded.capital,
                    founded=excluded.founded,
                    website=excluded.website,
                    phone=excluded.phone,
                    email=excluded.email,
                    description=excluded.description,
                    source=excluded.source,
                    raw_data=excluded.raw_data,
                    collected_at=excluded.collected_at
                """,
                (
                    company.name,
                    company.corporate_number,
                    company.address,
                    company.industry,
                    company.employee_count,
                    company.capital,
                    company.founded,
                    company.website,
                    company.phone,
                    company.email,
                    company.description,
                    company.source,
                    json.dumps(company.raw_data, ensure_ascii=False),
                    company.collected_at,
                ),
            )
            return cursor.lastrowid  # type: ignore[return-value]

    def upsert_companies(self, companies: list[Company]) -> int:
        count = 0
        for c in companies:
            self.upsert_company(c)
            count += 1
        return count

    def search_companies(
        self,
        *,
        name: str | None = None,
        industry: str | None = None,
        address: str | None = None,
        limit: int = 100,
    ) -> pd.DataFrame:
        conditions: list[str] = []
        params: list[Any] = []
        if name:
            conditions.append("name LIKE ?")
            params.append(f"%{name}%")
        if industry:
            conditions.append("industry LIKE ?")
            params.append(f"%{industry}%")
        if address:
            conditions.append("address LIKE ?")
            params.append(f"%{address}%")

        where = "WHERE " + " AND ".join(conditions) if conditions else ""
        query = f"SELECT * FROM companies {where} ORDER BY collected_at DESC LIMIT ?"
        params.append(limit)

        with self._connect() as conn:
            return pd.read_sql_query(query, conn, params=params)

    def get_all_companies(self, limit: int = 1000) -> pd.DataFrame:
        with self._connect() as conn:
            return pd.read_sql_query(
                "SELECT * FROM companies ORDER BY collected_at DESC LIMIT ?",
                conn,
                params=(limit,),
            )

    # ── Search Results ──

    def save_search_results(self, results: list[SearchResult]) -> int:
        with self._connect() as conn:
            conn.executemany(
                """INSERT INTO search_results
                   (query, title, url, snippet, source, raw_data, collected_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                [
                    (
                        r.query,
                        r.title,
                        r.url,
                        r.snippet,
                        r.source,
                        json.dumps(r.raw_data, ensure_ascii=False),
                        r.collected_at,
                    )
                    for r in results
                ],
            )
        return len(results)

    def get_search_results(self, query: str | None = None, limit: int = 100) -> pd.DataFrame:
        with self._connect() as conn:
            if query:
                return pd.read_sql_query(
                    "SELECT * FROM search_results WHERE query LIKE ? ORDER BY collected_at DESC LIMIT ?",
                    conn,
                    params=(f"%{query}%", limit),
                )
            return pd.read_sql_query(
                "SELECT * FROM search_results ORDER BY collected_at DESC LIMIT ?",
                conn,
                params=(limit,),
            )

    # ── Analysis Results ──

    def save_analysis(self, result: AnalysisResult) -> int:
        with self._connect() as conn:
            cursor = conn.execute(
                """INSERT INTO analysis_results
                   (analysis_type, target, result, parameters, analyzed_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    result.analysis_type,
                    result.target,
                    json.dumps(result.result, ensure_ascii=False),
                    json.dumps(result.parameters, ensure_ascii=False),
                    result.analyzed_at,
                ),
            )
            return cursor.lastrowid  # type: ignore[return-value]

    def get_analyses(
        self, analysis_type: str | None = None, limit: int = 100
    ) -> pd.DataFrame:
        with self._connect() as conn:
            if analysis_type:
                return pd.read_sql_query(
                    "SELECT * FROM analysis_results WHERE analysis_type = ? ORDER BY analyzed_at DESC LIMIT ?",
                    conn,
                    params=(analysis_type, limit),
                )
            return pd.read_sql_query(
                "SELECT * FROM analysis_results ORDER BY analyzed_at DESC LIMIT ?",
                conn,
                params=(limit,),
            )

    # ── Export ──

    def export_companies_csv(self, path: str | Path) -> int:
        df = self.get_all_companies()
        df.to_csv(path, index=False, encoding="utf-8-sig")
        return len(df)
