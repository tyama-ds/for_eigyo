import json
from contextlib import ExitStack
from types import SimpleNamespace

import httpx
import pytest

from app import field_llm
from app.connection_settings import ConnectionSettings, settings_context


@pytest.fixture
def configure():
    with ExitStack() as stack:
        yield lambda **value: stack.enter_context(settings_context(ConnectionSettings.model_validate(value)))


@pytest.fixture
def report():
    return {"result_id": "a" * 32, "topic_model": "nmf", "focus": {"id": "a", "label": "steel", "count": 10},
            "neighbor": {"id": "b", "label": "welding", "count": 12},
            "evidence_papers": [{"id": f"p{i}", "title": f"Paper {i}", "year": 2024,
                                 "topic_id": "a" if i % 2 else "b", "abstract": "X" * 3000,
                                 "authors": [{"name": "Researcher", "affiliations": ["University"]}]} for i in range(30)],
            "observations": [{"section": "overview", "title": "観測", "text": "10件", "evidence_ids": ["p1"]}],
            "scope": {"sampled": True, "is_demo": False}, "limitations": ["取得集合内の結果"]}


def response_data(pid="p1"):
    return {"headline": "比較レポート", "sections": [{"title": "分野の差分", "text": "観測された比較。", "evidence_ids": [pid]}],
            "caveats": ["因果関係は未確定"]}


def transport(monkeypatch, handler):
    original = httpx.Client
    monkeypatch.setattr(field_llm.httpx, "Client", lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs))


def test_default_report_does_not_contact_any_llm(report, monkeypatch):
    monkeypatch.setattr(field_llm, "local_status", lambda: pytest.fail("No LLM call allowed"))
    value = field_llm.generate(report)
    assert value["mode"] == "deterministic"
    assert value["sections"][0]["text"] == "10件"


@pytest.mark.parametrize("url", ["https://external.example/v1", "http://192.168.0.10:1234", "http://127.0.0.1.evil.com",
                                 "http://user:secret@localhost:11434", "file:///tmp/model", "http://localhost:1234/?key=secret"])
def test_local_url_must_not_transmit_to_remote_server(url):
    with pytest.raises(ValueError, match="ループバック"):
        ConnectionSettings(local={"url": url})


def test_evidence_is_balanced_bounded_and_does_not_mutate(report):
    report["methods"] = {"rows": [{"name": "PCR", "evidence_ids": ["p1", "p29"],
                                  "mentions": [{"paper_id": "p1", "snippet": "PCR"},
                                               {"paper_id": "p29", "snippet": "qPCR"}]}]}
    before = json.dumps(report)
    payload = field_llm.evidence_payload(report)
    assert len(payload["papers"]) == 16
    assert sum(p["topic_id"] == "a" for p in payload["papers"]) == 8
    assert max(len(p["abstract"]) for p in payload["papers"]) == 1400
    assert payload["methods"]["rows"][0]["evidence_ids"] == ["p1"]
    assert payload["methods"]["rows"][0]["mentions"] == [{"paper_id": "p1", "snippet": "PCR"}]
    assert json.dumps(report) == before


def test_local_ollama_generates_json_without_openai(report, monkeypatch, configure):
    configure(local={"backend": "ollama", "url": "http://127.0.0.1:11434"})
    calls = []
    def handler(request):
        calls.append(request)
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "local-test:1"}]})
        if request.url.path == "/api/show":
            return httpx.Response(200, json={"details": {"family": "test"}})
        body = json.loads(request.content)
        assert request.url.path == "/api/chat"
        assert body["stream"] is True and body["format"]["type"] == "object"
        assert body["options"]["temperature"] == 0
        assert "OPENAI_API_KEY" not in request.content.decode()
        records = [{"message": {"content": json.dumps(response_data())}, "done": False},
                   {"message": {"content": ""}, "done": True, "done_reason": "stop"}]
        return httpx.Response(200, text="\n".join(json.dumps(record) for record in records) + "\n")
    transport(monkeypatch, handler)
    value = field_llm.generate(report, "local")
    assert value["mode"] == "local_llm" and value["model"] == "local-test:1"
    assert len(calls) == 3
    assert all(request.url.host == "127.0.0.1" for request in calls)


def test_local_cloud_model_rejected_before_sending_papers(report, monkeypatch, configure):
    configure(local={"backend": "ollama", "url": "http://localhost:11434"})
    paths = []
    def handler(request):
        paths.append(request.url.path)
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "renamed-model"}]})
        return httpx.Response(200, json={"remote_host": "https://ollama.com", "remote_model": "remote"})
    transport(monkeypatch, handler)
    with pytest.raises(ValueError, match="クラウド"):
        field_llm.generate(report, "local")
    assert paths == ["/api/tags", "/api/show"]


def test_local_openai_compatible_protocol(report, monkeypatch, configure):
    configure(local={"backend": "openai_compatible", "url": "http://127.0.0.1:1234/v1"})
    def handler(request):
        if request.method == "GET":
            assert request.url.path == "/v1/models"
            return httpx.Response(200, json={"data": [{"id": "local-model"}]})
        assert request.url.path == "/v1/chat/completions"
        body = json.loads(request.content)
        assert body["response_format"]["json_schema"]["strict"] and body["stream"] is True
        records = [{"choices": [{"index": 0, "delta": {"content": json.dumps(response_data())}, "finish_reason": None}]},
                   {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}]
        return httpx.Response(200, text="".join("data: " + json.dumps(record) + "\n\n" for record in records) + "data: [DONE]\n\n")
    transport(monkeypatch, handler)
    assert field_llm.generate(report, "local")["mode"] == "local_llm"


def test_local_stream_preserves_browser_auth_direct_routing_and_progress(report, monkeypatch, configure):
    configure(local={"backend": "openai_compatible", "url": "http://127.0.0.1:1234/v1", "api_key": "local-secret"},
              openai={"api_key": "cloud-secret", "model": "cloud-model"},
              proxy={"enabled": True, "url": "http://proxy.invalid:8080", "no_proxy": ""})
    requests, clients, progress = [], [], []
    original = httpx.Client
    def handler(request):
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(200, json={"data": [{"id": "local-model"}]})
        data = json.dumps(response_data())
        event = {"choices": [{"index": 0, "delta": {"content": data}, "finish_reason": "stop"}]}
        return httpx.Response(200, text="data: " + json.dumps(event) + "\n\ndata: [DONE]\n\n")
    def client(**kwargs):
        clients.append(kwargs)
        return original(transport=httpx.MockTransport(handler), **kwargs)
    monkeypatch.setattr(field_llm.httpx, "Client", client)
    value = field_llm.generate(report, "local", progress=progress.append)
    assert value["mode"] == "local_llm"
    assert all(request.headers["Authorization"] == "Bearer local-secret" for request in requests)
    assert all("cloud-secret" not in str(request.headers) for request in requests)
    assert all(client["proxy"] is None and client["trust_env"] is False for client in clients)
    assert clients[-1]["timeout"].read == 600
    assert progress[0]["received_chars"] == 0 and progress[-1]["received_chars"] > 0
    assert all(set(item) == {"elapsed_seconds", "received_chars"} for item in progress)


def test_remote_error_does_not_leak_response_or_secret(monkeypatch, configure):
    configure(local={"url": "http://localhost:11434"})
    def handler(request):
        return httpx.Response(500, text="secret: private document and key")
    transport(monkeypatch, handler)
    status = field_llm.local_status()
    assert not status["available"]
    assert "secret" not in json.dumps(status)


def test_openai_is_explicit_structured_and_never_stores(report, monkeypatch, configure):
    configure(openai={"api_key": "test-secret", "model": "configured-model"})
    sent = {}
    class Client:
        def __init__(self, **kwargs):
            self.responses = self
            assert kwargs["base_url"] == "https://api.openai.com/v1"
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def parse(self, **kwargs):
            sent.update(kwargs)
            return SimpleNamespace(output_parsed=field_llm.NarrativeOutput(**response_data()))
    import openai
    monkeypatch.setattr(openai, "OpenAI", Client)
    value = field_llm.generate(report, "openai")
    assert value["mode"] == "openai"
    assert sent["store"] is False and sent["model"] == "configured-model"
    assert sent["text_format"] is field_llm.NarrativeOutput
    assert "test-secret" not in json.dumps(value)


def test_fabricated_or_unshared_paper_ids_are_rejected(report):
    payload = field_llm.evidence_payload(report)
    for identifier in ("nonexistent", "p29"):
        with pytest.raises(RuntimeError, match="論文ID"):
            field_llm._validate_narrative(response_data(identifier), payload, "local_llm", "test")
    with pytest.raises(RuntimeError, match="形式"):
        field_llm._validate_narrative({"headline": "invalid"}, payload, "local_llm", "test")
