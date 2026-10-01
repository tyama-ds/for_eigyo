from copy import deepcopy
import json

import httpx
from pydantic import BaseModel
import pytest

from app import field_llm, landscape_evidence, llm_context, local_llm_stream
from app.connection_settings import ConnectionSettings, settings_context


class Answer(BaseModel):
    answer: str


def papers_payload():
    papers = [{"id": f"p-{side}-{i}", "title": f"Research {i}", "year": year,
               "side": side, "period": str(year), "abstract": "引張強度と析出硬化の検討。" * 400}
              for side, year in (("before", 2023), ("after", 2024)) for i in range(4)]
    papers = landscape_evidence.prepare_papers(papers)
    return {"papers": papers, "movement": {"from_period": "2023", "to_period": "2024",
                "from_count": 81, "to_count": 143, "cosine_distance": .173},
            "input_summary": landscape_evidence.input_summary(papers)}


def budget(payload, **kwargs):
    return llm_context.prepare(payload, "Observe the supplied data.", Answer.model_json_schema(),
        context_window=kwargs.pop("context_window", 8192), context_source="browser",
        output_tokens=kwargs.pop("output_tokens", 2000), **kwargs)


def test_large_japanese_abstracts_fit_without_mutating_metrics_ids_periods_or_source():
    payload = papers_payload()
    original = deepcopy(payload)
    actual, audit = budget(payload)
    assert payload == original
    assert audit["fits"] and audit["reduced"]
    assert audit["input_tokens_estimate"] + audit["output_tokens"] + audit["safety_tokens"] <= 8192
    assert actual["movement"] == original["movement"]
    assert {p["side"] for p in actual["papers"]} == {"before", "after"}
    by_id = {p["id"]: p for p in original["papers"]}
    for paper in actual["papers"]:
        assert paper["period"] == by_id[paper["id"]]["period"]
        assert paper["year"] == by_id[paper["id"]]["year"]
        assert paper["citation_id"] == by_id[paper["id"]]["citation_id"]
        assert paper["abstract_sent_chars"] == len(paper["abstract"])
        assert paper["abstract_original_chars"] == by_id[paper["id"]]["abstract_original_chars"]
        assert paper["abstract_ranges"][0][0] == 0
        assert paper["abstract_ranges"][-1][1] == paper["abstract_original_chars"]
    assert actual["input_summary"]["paper_count"] == len(actual["papers"])
    assert json.loads(llm_context.serialize(actual)) == actual
    assert "引張" not in json.dumps(audit, ensure_ascii=False)


def test_estimator_accounts_for_unicode_and_schema_and_template_reserve():
    assert llm_context.estimate_tokens("強度分析") > llm_context.estimate_tokens("abcd")
    payload = {"papers": [{"id": "p", "abstract": "text " * 900}]}
    small, first = llm_context.prepare(payload, "Instructions", {}, context_window=4096,
                                     context_source="browser", output_tokens=700)
    large, second = llm_context.prepare(payload, "Instructions", {"description": "s" * 6000},
                                      context_window=4096, context_source="browser", output_tokens=700)
    assert second["schema_tokens_estimate"] > first["schema_tokens_estimate"]
    assert len(large["papers"][0]["abstract"]) < len(small["papers"][0]["abstract"])
    assert second["input_tokens_estimate"] + 700 + second["safety_tokens"] <= 4096


def test_numbered_excerpts_and_facts_remain_whole_when_budget_reduces_them():
    payload = {"papers": [{"id": f"p{i}", "excerpts": [
        {"id": f"e{i}-{j}", "text": "研究抄録の一文。" * 45} for j in range(3)]} for i in range(4)]}
    original = deepcopy(payload)
    actual, audit = budget(payload, context_window=4096, output_tokens=1000)
    originals = {item["id"]: item for paper in original["papers"] for item in paper["excerpts"]}
    assert audit["fits"] and audit["omitted_excerpt_ids"]
    assert actual["papers"] and all(paper["excerpts"] for paper in actual["papers"])
    for paper in actual["papers"]:
        for excerpt in paper["excerpts"]:
            assert excerpt == originals[excerpt["id"]]
    assert payload == original
    facts = {"facts": [{"id": f"fact-{i}", "quote": "鋼の強度測定。" * 50, "paper_id": f"p{i}",
                        "source_hash": f"hash-{i}"} for i in range(6)], "metrics": {"count": 61}}
    actual, audit = budget(facts, context_window=4096, output_tokens=1000)
    assert audit["fits"] and audit["omitted_fact_ids"]
    assert all(fact == facts["facts"][int(fact["id"].split("-")[-1])] for fact in actual["facts"])
    assert actual["metrics"] == facts["metrics"]


def test_cannot_fit_both_sides_returns_audit_without_dropping_either_side_or_measurements():
    payload = papers_payload()
    payload["movement"]["unshortenable"] = list(range(4000))
    actual, audit = budget(payload, context_window=2048, output_tokens=600)
    assert not audit["fits"]
    assert {p["side"] for p in actual["papers"]} == {"before", "after"}
    assert actual["movement"] == payload["movement"]


def test_missing_abstract_never_displaces_the_only_substantive_evidence_for_a_period():
    payload = {"papers": [{"id": "empty", "side": "before", "abstract": ""},
                           {"id": "source", "side": "before", "abstract": "実験の結果。" * 500},
                           {"id": "other", "side": "after", "abstract": "比較の結果。" * 500}]}
    actual, audit = budget(payload, context_window=2000, output_tokens=512)
    assert audit["fits"]
    assert {paper["id"] for paper in actual["papers"]} == {"source", "other"}
    assert all(paper["abstract"].strip() for paper in actual["papers"])


def completed(backend="openai_compatible"):
    if backend == "ollama":
        return httpx.Response(200, text=json.dumps({"message": {"content": '{"answer":"ok"}'},
            "done": True, "done_reason": "stop"}) + "\n")
    return httpx.Response(200, text='data: {"choices":[{"index":0,"delta":{"content":"{\\"answer\\":\\"ok\\"}"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n')


def install_transport(monkeypatch, generate, *, backend="openai_compatible", models=None, native=None, legacy=None, show=None):
    original = httpx.Client
    requests = []
    def handler(request):
        requests.append(request)
        if request.url.path in {"/v1/models", "/api/tags", "/custom/models"}:
            return httpx.Response(200, json=models or ({"models": [{"name": "test-model"}]} if backend == "ollama"
                                                     else {"data": [{"id": "test-model"}]}))
        if request.url.path == "/api/show":
            return httpx.Response(200, json=show or {})
        if request.url.path in {"/api/v1/models", "/api/ps"}:
            return httpx.Response(200, json=native) if native is not None else httpx.Response(404)
        if request.url.path == "/api/v0/models":
            return httpx.Response(200, json=legacy) if legacy is not None else httpx.Response(404)
        return generate(request)
    monkeypatch.setattr(field_llm.httpx, "Client", lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs))
    return requests


def invoke_gateway(payload, context, *, backend="openai_compatible", **settings):
    url = settings.pop("url", "http://127.0.0.1:1234/v1" if backend != "ollama" else "http://127.0.0.1:11434")
    with settings_context(ConnectionSettings(local={"backend": backend, "url": url, **settings})):
        return field_llm.structured_output(payload, Answer, "Instructions", "local", input_context=context)


@pytest.mark.parametrize("error", [
    "The number of tokens to keep from the initial prompt is greater than the context length (n_keep: 8485 >= n_ctx: 8192).",
    "Cannot truncate prompt with n_keep >= n_ctx",
    "This model's maximum context length is 8192 tokens, but the input length is too long.",
    "input length exceeds maximum context length",
])
def test_http_400_context_overflow_retries_with_smaller_valid_payload_and_sanitized_audit(monkeypatch, error):
    sent = []
    def generate(request):
        body = json.loads(request.content)
        sent.append(json.loads(body["messages"][1]["content"]))
        if len(sent) == 1:
            return httpx.Response(400, json={"error": {"message": error + " private-secret"}})
        return completed()
    install_transport(monkeypatch, generate)
    payload, context = papers_payload(), {}
    original = deepcopy(payload)
    value, mode, chosen = invoke_gateway(payload, context)
    assert value == {"answer": "ok"} and mode == "local_llm" and chosen == "test-model"
    assert len(sent) == 2 and len(llm_context.serialize(sent[1])) < len(llm_context.serialize(sent[0]))
    assert context["payload"] == sent[-1] and context["metadata"]["retries"] == 1
    assert context["metadata"]["request_attempts"] == 2 and context["metadata"]["status"] == "completed"
    assert "private-secret" not in json.dumps(context)
    assert payload == original


def test_repeated_context_failure_stops_after_three_requests_and_keeps_last_input_audit(monkeypatch):
    sent = []
    def generate(request):
        sent.append(json.loads(request.content))
        return httpx.Response(400, json={"error": "input too long for context window private-secret"})
    install_transport(monkeypatch, generate)
    context = {}
    with pytest.raises(local_llm_stream.LocalStreamError, match="コンテキスト") as caught:
        invoke_gateway(papers_payload(), context)
    assert caught.value.kind == "context_length" and "private-secret" not in str(caught.value)
    assert len(sent) == 3
    assert context["metadata"]["request_attempts"] == 3 and context["metadata"]["status"] == "failed"
    lengths = [len(body["messages"][1]["content"]) for body in sent]
    assert lengths[0] > lengths[1] > lengths[2]


@pytest.mark.parametrize("status,text", [(401, "input too long private-secret"), (403, "context_length_exceeded"),
                                         (500, "CUDA out of memory: context length allocation failed")])
def test_auth_and_memory_errors_never_retry(monkeypatch, status, text):
    sent = []
    def generate(request):
        sent.append(request)
        return httpx.Response(status, text=text)
    install_transport(monkeypatch, generate)
    context = {}
    with pytest.raises(local_llm_stream.LocalStreamError) as caught:
        invoke_gateway({"papers": [{"id": "one", "abstract": "valid source"}]}, context)
    assert len(sent) == 1 and caught.value.kind != "context_length"
    assert "private-secret" not in str(caught.value)


@pytest.mark.parametrize("backend", ["openai_compatible", "ollama"])
def test_stream_context_error_is_recognized_and_output_limit_is_distinct(monkeypatch, backend):
    sent = []
    def generate(request):
        sent.append(request)
        if len(sent) == 1:
            record = json.dumps({"error": "prompt too long: maximum context length is 4096 private-secret"})
            return httpx.Response(200, text=record + "\n" if backend == "ollama" else "data: " + record + "\n\n")
        return completed(backend)
    install_transport(monkeypatch, generate, backend=backend)
    context = {}
    payload = {"papers": [{"id": "p-before", "side": "before", "abstract": "experiment " * 700},
                           {"id": "p-after", "side": "after", "abstract": "improvement " * 700}]}
    invoke_gateway(payload, context, backend=backend)
    assert len(sent) == 2 and context["metadata"]["context_window"] == 4096
    assert context["metadata"]["context_source"] == "server_error"


def test_output_truncation_does_not_trigger_input_retry(monkeypatch):
    sent = []
    def generate(request):
        sent.append(request)
        return httpx.Response(200, text='data: {"choices":[{"index":0,"delta":{},"finish_reason":"length"}]}\n\n')
    install_transport(monkeypatch, generate)
    with pytest.raises(local_llm_stream.LocalStreamError, match="回答上限") as caught:
        invoke_gateway({}, {})
    assert caught.value.kind == "token_limit" and len(sent) == 1


def test_lmstudio_active_instance_limit_wins_over_model_ceiling_and_browser_override(monkeypatch):
    sent = []
    def generate(request):
        sent.append(json.loads(request.content))
        return completed()
    install_transport(monkeypatch, generate, native={"models": [{"key": "test-model", "max_context_length": 131072,
        "loaded_instances": [{"id": "loaded-one", "config": {"context_length": 4096}}]}]})
    context = {}
    invoke_gateway(papers_payload(), context, context_window=32768)
    assert context["metadata"]["context_window"] == 4096
    assert context["metadata"]["context_source"] == "server"
    assert sent[0]["max_tokens"] == 4096 // 3
    assert context["metadata"]["requested_output_tokens"] == 4000


def test_ollama_active_limit_and_output_reserve_never_set_training_context(monkeypatch):
    sent = []
    def generate(request):
        sent.append(json.loads(request.content))
        return completed("ollama")
    install_transport(monkeypatch, generate, backend="ollama",
        native={"models": [{"name": "test-model", "context_length": 4096}]},
        show={"model_info": {"qwen.context_length": 131072}})
    context = {}
    invoke_gateway(papers_payload(), context, backend="ollama")
    assert context["metadata"]["context_window"] == 4096
    assert sent[0]["options"]["num_ctx"] == 4096
    assert sent[0]["options"]["num_predict"] == 4096 // 3


def test_unknown_ollama_context_does_not_silently_reconfigure_model(monkeypatch):
    sent = []
    def generate(request):
        sent.append(json.loads(request.content))
        return completed("ollama")
    install_transport(monkeypatch, generate, backend="ollama", show={"model_info": {"qwen.context_length": 131072}})
    context = {}
    invoke_gateway({}, context, backend="ollama")
    assert "num_ctx" not in sent[0]["options"]
    assert context["metadata"]["context_source"] == "fallback"
    assert context["metadata"]["context_window"] == 4096


def test_legacy_lmstudio_detects_loaded_context_instead_of_training_maximum(monkeypatch):
    install_transport(monkeypatch, lambda _request: completed(), legacy={"data": [
        {"id": "test-model", "loaded_context_length": 4096, "max_context_length": 131072}]})
    context = {}
    invoke_gateway({}, context)
    assert context["metadata"]["context_window"] == 4096
    assert context["metadata"]["context_source"] == "server"


def test_custom_compatible_prefix_does_not_guess_discovery_at_origin_root(monkeypatch):
    requests = install_transport(monkeypatch, lambda _request: completed())
    context = {}
    invoke_gateway({}, context, url="http://127.0.0.1:1234/custom")
    assert [request.url.path for request in requests] == ["/custom/models", "/custom/chat/completions"]


def test_irreducible_input_fails_before_generation_with_auditable_payload(monkeypatch):
    calls = []
    install_transport(monkeypatch, lambda request: calls.append(request))
    context = {}
    with pytest.raises(local_llm_stream.LocalStreamError, match="計測値") as caught:
        invoke_gateway({"measurements": list(range(30000))}, context, context_window=2048)
    assert caught.value.kind == "context_budget" and not calls
    assert context["metadata"]["status"] == "failed" and context["metadata"]["request_attempts"] == 0
    assert len(context["payload"]["measurements"]) == 30000
