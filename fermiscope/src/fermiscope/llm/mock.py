"""MockLLMProvider — テスト・デモ用の決定論的LLM。

コンストラクタで応答を注入できる。既定では簡単なヒューリスティック応答を返す。
AIフォールバック経路(成功・失敗・捏造検出)のテストに使用する。
"""

from __future__ import annotations

import re
from typing import Any

from fermiscope.llm.base import LLMProvider
from fermiscope.llm.schemas import (
    CritiqueProposal,
    DecompositionProposal,
    EvidenceExtraction,
    ModelProposal,
    QueryProposal,
    QuestionClassification,
)


class MockLLMProvider(LLMProvider):
    name = "mock"
    available = True

    def __init__(self, canned: dict[str, Any] | None = None) -> None:
        """canned のキー: classify / models / queries / extract / critique / decompose / explain"""
        self.canned = canned or {}
        self.calls: list[tuple[str, dict]] = []

    async def classify_question(self, question: str) -> QuestionClassification | None:
        self.calls.append(("classify", {"question": question}))
        if "classify" in self.canned:
            v = self.canned["classify"]
            return QuestionClassification(**v) if isinstance(v, dict) else v
        return None

    async def propose_models(
        self, question_summary: str, target_unit: str
    ) -> list[ModelProposal] | None:
        self.calls.append(("models", {"summary": question_summary}))
        if "models" in self.canned:
            return [ModelProposal(**m) if isinstance(m, dict) else m for m in self.canned["models"]]
        return None

    async def propose_queries(
        self, parameter_name: str, description: str, geography: str
    ) -> QueryProposal | None:
        self.calls.append(("queries", {"parameter": parameter_name}))
        if "queries" in self.canned:
            v = self.canned["queries"]
            return QueryProposal(**v) if isinstance(v, dict) else v
        return QueryProposal(
            queries_ja=[f"{parameter_name} {geography} 統計".strip()],
            queries_en=[f"{parameter_name} statistics {geography}".strip()],
        )

    async def extract_structured_evidence(
        self, wrapped_document: str, parameter_name: str, unit_hint: str
    ) -> EvidenceExtraction | None:
        self.calls.append(("extract", {"parameter": parameter_name}))
        if "extract" in self.canned:
            v = self.canned["extract"]
            if v is None:
                return None
            return EvidenceExtraction(**v) if isinstance(v, dict) else v
        # 既定ヒューリスティック: 境界内の最初の「数値+単位らしき」並びを返す
        m = re.search(r"([0-9][0-9,]*(?:\.[0-9]+)?)\s*(世帯|人|台|件|回|%|円)", wrapped_document)
        if not m:
            return None
        start = max(0, m.start() - 40)
        return EvidenceExtraction(
            value=float(m.group(1).replace(",", "")),
            unit=m.group(2),
            excerpt=wrapped_document[start : m.end() + 40],
            locator="mock-llm",
        )

    async def propose_critique(
        self, parameter_name: str, definition: str, evidence_summary: str
    ) -> list[CritiqueProposal] | None:
        self.calls.append(("critique", {"parameter": parameter_name}))
        if "critique" in self.canned:
            return [
                CritiqueProposal(**c) if isinstance(c, dict) else c for c in self.canned["critique"]
            ]
        return [
            CritiqueProposal(
                claim=f"{parameter_name} の証拠は時点・母集団の代表性に限界がある可能性(AI仮説)",
                severity=0.3,
            )
        ]

    async def propose_decomposition(
        self, parameter_name: str, unit: str, description: str
    ) -> DecompositionProposal | None:
        self.calls.append(("decompose", {"parameter": parameter_name}))
        if "decompose" in self.canned:
            v = self.canned["decompose"]
            if v is None:
                return None
            return DecompositionProposal(**v) if isinstance(v, dict) else v
        return None

    async def draft_explanation(self, project_summary: str) -> str | None:
        self.calls.append(("explain", {}))
        return self.canned.get("explain")
