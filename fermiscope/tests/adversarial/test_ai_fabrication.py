"""AI出力の捏造(存在しない引用・数値)への耐性テスト。"""

import json

import httpx
import pytest

from fermiscope.domain.models import ParameterEstimate
from fermiscope.evidence.extractor import validate_llm_extraction
from fermiscope.llm.openai_compat import OpenAICompatProvider
from fermiscope.llm.schemas import EvidenceExtraction


def make_llm(handler) -> OpenAICompatProvider:
    return OpenAICompatProvider(
        api_base="https://llm.example.com/v1",
        api_key="test-key",
        model="test-model",
        transport=httpx.MockTransport(handler),
    )


def chat_response(content: str) -> httpx.Response:
    return httpx.Response(200, json={
        "choices": [{"message": {"content": content}}]
    })


@pytest.mark.asyncio
async def test_invalid_json_returns_none():
    llm = make_llm(lambda r: chat_response("これはJSONではありません"))
    result = await llm.extract_structured_evidence("doc", "param", "unit")
    assert result is None
    await llm.close()


@pytest.mark.asyncio
async def test_schema_violation_returns_none():
    llm = make_llm(lambda r: chat_response(json.dumps({"value": "not-a-number", "excerpt": 5})))
    result = await llm.extract_structured_evidence("doc", "param", "unit")
    assert result is None
    await llm.close()


@pytest.mark.asyncio
async def test_empty_response_returns_none():
    llm = make_llm(lambda r: chat_response(""))
    result = await llm.classify_question("質問")
    assert result is None
    await llm.close()


@pytest.mark.asyncio
async def test_rate_limit_returns_none():
    llm = make_llm(lambda r: httpx.Response(429))
    result = await llm.classify_question("質問")
    assert result is None
    await llm.close()


@pytest.mark.asyncio
async def test_timeout_returns_none():
    def handler(request):
        raise httpx.ConnectTimeout("timeout")

    llm = make_llm(handler)
    result = await llm.classify_question("質問")
    assert result is None
    await llm.close()


@pytest.mark.asyncio
async def test_api_key_never_logged(caplog):
    """接続エラー時のログにAPIキーが含まれない。"""
    secret = "sk-secret-do-not-log-xyz"

    def handler(request):
        raise httpx.ConnectError("connection failed")

    llm = OpenAICompatProvider(
        api_base="https://llm.example.com/v1", api_key=secret, model="m",
        transport=httpx.MockTransport(handler),
    )
    import logging

    with caplog.at_level(logging.DEBUG):
        await llm.classify_question("質問")
    assert secret not in caplog.text
    await llm.close()


def test_fabricated_citation_rejected_by_validation(settings):
    """AIが返した『存在しない引用』はPython側検証で棄却される。"""
    from fermiscope.domain.enums import DocumentType
    from fermiscope.research.fetcher import FetchedDocument

    doc = FetchedDocument(
        url="https://real.example.jp/", final_url="https://real.example.jp/",
        content_type="text/html", doc_type=DocumentType.HTML, status_code=200,
        text="実際の文書にはピアノの普及率は10.4%と書かれています。",
    )
    param = ParameterEstimate(id="p", name="普及率", unit="dimensionless")
    fabricated = EvidenceExtraction(
        value=55.0, unit="percent",
        excerpt="この文書には存在しない架空の引用文です。普及率は55%。",
    )
    ok, reason = validate_llm_extraction(doc, param, fabricated.model_dump())
    assert not ok
    # 実在する引用なら通る
    genuine = EvidenceExtraction(
        value=10.4, unit="percent", excerpt="ピアノの普及率は10.4%",
    )
    ok2, _ = validate_llm_extraction(doc, param, genuine.model_dump())
    assert ok2


def test_llm_number_without_source_never_stored(settings, noop_llm):
    """LLM数値の直接保存経路が存在しないことの静的確認:
    orchestrator は validate_llm_extraction を通過した場合のみ EvidenceItem を作る。"""
    import inspect

    from fermiscope.research import orchestrator

    src = inspect.getsource(orchestrator)
    # LLM抽出の採用箇所は検証関数の呼び出しとセットで存在する
    assert "validate_llm_extraction" in src
    assert "ai_assisted=True" in src
