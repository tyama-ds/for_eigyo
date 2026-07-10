"""LLM出力の検証スキーマ(Pydantic)。

LLM出力は必ずここで検証してから使用する。検証失敗はフォールバック失敗として
扱い、値を捏造せず「未解決」に落とす。
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class QuestionClassification(BaseModel):
    subject: str = ""
    geography: str = ""
    reference_date: str = ""
    time_period: str = ""
    stock_or_flow: str = "unknown"
    target_metric: str = ""
    target_unit: str = ""
    inclusions: list[str] = Field(default_factory=list)
    exclusions: list[str] = Field(default_factory=list)
    template_hints: list[str] = Field(default_factory=list)


class ParameterProposal(BaseModel):
    id: str
    name: str
    unit: str = "dimensionless"
    description: str = ""
    search_terms_ja: list[str] = Field(default_factory=list)
    search_terms_en: list[str] = Field(default_factory=list)


class ModelProposal(BaseModel):
    name: str
    approach: str = ""
    expression: str
    description: str = ""
    parameters: list[ParameterProposal] = Field(default_factory=list)


class QueryProposal(BaseModel):
    queries_ja: list[str] = Field(default_factory=list)
    queries_en: list[str] = Field(default_factory=list)


class EvidenceExtraction(BaseModel):
    value: float
    unit: str = ""
    low: float | None = None
    high: float | None = None
    excerpt: str
    locator: str = ""
    time_period: str = ""
    population: str = ""
    definition: str = ""


class CritiqueProposal(BaseModel):
    issue_type: str = "ai_hypothesis"
    claim: str
    severity: float = 0.3
    likely_direction_of_bias: str = "unknown"
    recommended_action: str = ""


class DecompositionProposal(BaseModel):
    expression: str
    parameters: list[ParameterProposal] = Field(default_factory=list)
    rationale: str = ""
