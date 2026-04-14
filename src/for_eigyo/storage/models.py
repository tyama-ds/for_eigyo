"""データモデル定義"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field, asdict
from typing import Any


@dataclass
class Company:
    """企業情報"""

    name: str
    corporate_number: str | None = None  # 法人番号
    address: str | None = None
    industry: str | None = None
    employee_count: int | None = None
    capital: int | None = None  # 資本金（円）
    founded: str | None = None
    website: str | None = None
    phone: str | None = None
    email: str | None = None
    description: str | None = None
    source: str | None = None
    raw_data: dict[str, Any] = field(default_factory=dict)
    collected_at: str = field(default_factory=lambda: dt.datetime.now().isoformat())

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("raw_data", None)
        return d


@dataclass
class SearchResult:
    """検索結果"""

    query: str
    title: str
    url: str
    snippet: str
    source: str  # duckduckgo, gbizinfo, web, etc.
    collected_at: str = field(default_factory=lambda: dt.datetime.now().isoformat())
    raw_data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("raw_data", None)
        return d


@dataclass
class AnalysisResult:
    """分析結果"""

    analysis_type: str  # keywords, sentiment, ner, cluster, similarity, scoring
    target: str  # 分析対象の識別子
    result: dict[str, Any] = field(default_factory=dict)
    parameters: dict[str, Any] = field(default_factory=dict)
    analyzed_at: str = field(default_factory=lambda: dt.datetime.now().isoformat())

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class LeadScore:
    """リードスコア"""

    company_name: str
    score: float  # 0.0 - 1.0
    factors: dict[str, float] = field(default_factory=dict)
    rank: str = ""  # A, B, C, D
    scored_at: str = field(default_factory=lambda: dt.datetime.now().isoformat())

    def __post_init__(self):
        if not self.rank:
            if self.score >= 0.8:
                self.rank = "A"
            elif self.score >= 0.6:
                self.rank = "B"
            elif self.score >= 0.4:
                self.rank = "C"
            else:
                self.rank = "D"
