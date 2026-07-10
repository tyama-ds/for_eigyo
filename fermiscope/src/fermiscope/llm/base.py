"""LLMProvider 抽象インターフェース。"""

from __future__ import annotations

from fermiscope.llm.schemas import (
    CritiqueProposal,
    DecompositionProposal,
    EvidenceExtraction,
    ModelProposal,
    QueryProposal,
    QuestionClassification,
)


class LLMProviderError(RuntimeError):
    pass


class LLMProvider:
    """生成AIプロバイダの共通インターフェース。

    すべてのメソッドは「利用不能・失敗時に None を返す」契約とする。
    呼び出し側は None を受けたら値を捏造せず、未解決として処理を続行する。
    """

    name: str = "abstract"
    available: bool = False

    async def classify_question(self, question: str) -> QuestionClassification | None:
        return None

    async def propose_models(
        self, question_summary: str, target_unit: str
    ) -> list[ModelProposal] | None:
        return None

    async def propose_queries(
        self, parameter_name: str, description: str, geography: str
    ) -> QueryProposal | None:
        return None

    async def extract_structured_evidence(
        self, wrapped_document: str, parameter_name: str, unit_hint: str
    ) -> EvidenceExtraction | None:
        """wrapped_document は security.boundary.wrap_untrusted 済みであること。"""
        return None

    async def propose_critique(
        self, parameter_name: str, definition: str, evidence_summary: str
    ) -> list[CritiqueProposal] | None:
        return None

    async def propose_decomposition(
        self, parameter_name: str, unit: str, description: str
    ) -> DecompositionProposal | None:
        return None

    async def draft_explanation(self, project_summary: str) -> str | None:
        return None

    async def close(self) -> None:  # noqa: B027
        pass
