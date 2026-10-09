"""Socket-level integration tests; all servers bind to loopback only."""

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import socket
import threading
import time
from types import SimpleNamespace

import httpx
import pytest
import uvicorn

from chat_app.main import create_app


HEADERS = {"X-Local-Chat": "1"}


def _frame(content=None, finish=None):
    delta = {} if content is None else {"content": content}
    data = {"choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
    return ("data: " + json.dumps(data, ensure_ascii=False) + "\n\n").encode("utf-8")


@contextmanager
def _http_server(handler):
    class JoinedServer(ThreadingHTTPServer):
        daemon_threads = False
        block_on_close = True

    server = JoinedServer(("127.0.0.1", 0), handler)
    worker = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.05), name="test-http-server")
    worker.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)
        assert not worker.is_alive(), "HTTP fixture did not stop"


@pytest.fixture
def fake_llm():
    state = SimpleNamespace(payloads=[], requests=[], stop=threading.Event(),
                            slow_closed=threading.Event(), slow_exited=threading.Event())

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def do_GET(self):
            state.requests.append((self.command, self.path))
            assert self.path == "/v1/models"
            body = json.dumps({"object": "list", "data": [{"id": "socket-model"}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
            self.close_connection = True

        def do_POST(self):
            state.requests.append((self.command, self.path))
            assert self.path == "/v1/chat/completions"
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state.payloads.append(payload)
            slow = payload["messages"][-1]["content"] == "slow-cancel-test"
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            try:
                if slow:
                    self.wfile.write(_frame("途中の回答"))
                    self.wfile.flush()
                    for _ in range(500):
                        if state.stop.wait(0.02):
                            return
                        self.wfile.write(_frame("続き" * 1024))
                        self.wfile.flush()
                else:
                    # Split a UTF-8 sequence across socket writes to exercise decoding.
                    first = _frame("確認しました。")
                    split = first.index("確認".encode("utf-8")) + 1
                    self.wfile.write(first[:split])
                    self.wfile.flush()
                    self.wfile.write(first[split:] + _frame("追加の回答です。"))
                    self.wfile.flush()
                self.wfile.write(_frame(finish="stop") + b"data: [DONE]\n\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                if slow:
                    state.slow_closed.set()
            finally:
                if slow:
                    state.slow_exited.set()

    with _http_server(Handler) as server:
        state.url = f"http://127.0.0.1:{server.server_port}/v1"
        try:
            yield state
        finally:
            state.stop.set()


@pytest.fixture
def live_app(tmp_path):
    app = create_app(tmp_path)
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    configuration = uvicorn.Config(app, host="127.0.0.1", port=port, loop="asyncio",
                                   log_level="critical", access_log=False, timeout_graceful_shutdown=3)
    server = uvicorn.Server(configuration)
    worker = threading.Thread(target=lambda: server.run(sockets=[listener]), name="test-uvicorn")
    worker.start()
    try:
        deadline = time.monotonic() + 5
        while not server.started and worker.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert server.started, "Loopback application did not start"
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", headers=HEADERS,
                          trust_env=False, timeout=5) as client:
            yield SimpleNamespace(client=client, app=app)
    finally:
        server.should_exit = True
        worker.join(timeout=5)
        if worker.is_alive():
            server.force_exit = True
            worker.join(timeout=3)
        listener.close()
        assert not worker.is_alive(), "Application fixture did not stop"


def _configure(client, fake_llm, **settings):
    result = client.put("/api/settings", json={"base_url": fake_llm.url, "model": "socket-model", **settings})
    assert result.status_code == 200, result.text


def _events(response):
    event, data = "message", []
    for line in response.iter_lines():
        if not line:
            if data:
                yield event, json.loads("\n".join(data))
            event, data = "message", []
        elif line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            data.append(line[5:].lstrip())


def test_real_socket_models_attachment_stream_and_history(live_app, fake_llm):
    client = live_app.client
    _configure(client, fake_llm)
    models = client.get("/api/models")
    assert models.status_code == 200
    assert models.json() == {"models": ["socket-model"]}
    ident = client.post("/api/conversations", json={}).json()["id"]
    upload = client.post("/api/attachments", files={"file": ("memo.txt", "添付資料の重要な内容".encode(), "text/plain")})
    assert upload.status_code == 200
    with client.stream("POST", "/api/chat", json={"conversation_id": ident, "message": "要約してください",
                       "attachment_ids": [upload.json()["id"]]}) as response:
        assert response.status_code == 200
        events = list(_events(response))
    assert events[0][0] == "meta"
    assert "".join(data["content"] for kind, data in events if kind == "delta") == "確認しました。追加の回答です。"
    assert events[-1][0] == "done" and events[-1][1]["status"] == "complete"
    first_input = fake_llm.payloads[0]["messages"][-1]["content"]
    assert "要約してください" in first_input
    assert "添付資料の重要な内容" in first_input
    assert fake_llm.payloads[0]["stream"] is True
    with client.stream("POST", "/api/chat", json={"conversation_id": ident, "message": "続けて説明してください"}) as response:
        assert response.status_code == 200
        assert list(_events(response))[-1][0] == "done"
    sent_history = fake_llm.payloads[1]["messages"]
    assert [message["role"] for message in sent_history] == ["system", "user", "assistant", "user"]
    assert "添付資料の重要な内容" in sent_history[1]["content"]
    assert sent_history[2]["content"] == "確認しました。追加の回答です。"
    saved = client.get("/api/conversations/" + ident).json()["messages"]
    assert len(saved) == 4
    assert all(message["status"] == "complete" for message in saved)


def test_real_socket_cancel_closes_llm_and_persists_partial(live_app, fake_llm):
    client = live_app.client
    _configure(client, fake_llm)
    ident = client.post("/api/conversations", json={}).json()["id"]
    with client.stream("POST", "/api/chat", json={"conversation_id": ident, "message": "slow-cancel-test"}) as response:
        assert response.status_code == 200
        for kind, data in _events(response):
            if kind == "delta":
                assert data["content"] == "途中の回答"
                break
        else:
            pytest.fail("No partial answer was streamed before cancellation")
        # Leaving the context closes the browser-side HTTP socket mid-stream.
    deadline = time.monotonic() + 5
    saved = []
    while time.monotonic() < deadline:
        saved = client.get("/api/conversations/" + ident).json()["messages"]
        if len(saved) == 2:
            break
        time.sleep(0.02)
    assert len(saved) == 2
    assert saved[-1]["content"].startswith("途中の回答")
    assert saved[-1]["status"] == "interrupted"
    assert ident not in live_app.app.state.active
    assert fake_llm.slow_closed.wait(timeout=3), "Cancelled request left the downstream LLM socket open"
    assert fake_llm.slow_exited.wait(timeout=1)
    with client.stream("POST", "/api/chat", json={"conversation_id": ident, "message": "停止後に再開"}) as response:
        assert response.status_code == 200
        assert list(_events(response))[-1][1]["status"] == "complete"


def test_real_socket_web_proxy_is_separate_from_llm(live_app, fake_llm, monkeypatch):
    requests = []

    class Proxy(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            requests.append((self.path, self.headers.get("Proxy-Authorization")))
            body = "<html><title>Proxy document</title><p>プロキシ経由の本文</p></html>".encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    with _http_server(Proxy) as server:
        proxy_url = f"http://reader@127.0.0.1:{server.server_port}"
        # A literal public address passes SSRF validation without any DNS or
        # external traffic; the local proxy responds directly, never forwarding.
        target = "http://93.184.216.34/proxy-fixture"
        monkeypatch.setenv("HTTP_PROXY", proxy_url)
        monkeypatch.setenv("HTTPS_PROXY", proxy_url)
        monkeypatch.setenv("ALL_PROXY", proxy_url)
        monkeypatch.setenv("NO_PROXY", "")
        client = live_app.client
        _configure(client, fake_llm, proxy_mode="manual", proxy_url=proxy_url, proxy_password="local-test-password")
        models = client.get("/api/models")
        assert models.status_code == 200
        assert models.json()["models"] == ["socket-model"]
        assert requests == [], "LLM connection unexpectedly used the external-web proxy"
        attachment = client.post("/api/web", json={"url": target})
        assert attachment.status_code == 200, attachment.text
        assert attachment.json()["name"] == "Proxy document"
        assert len(requests) == 1
        assert requests[0][0] == target
        assert requests[0][1].startswith("Basic ")
        ident = client.post("/api/conversations", json={}).json()["id"]
        with client.stream("POST", "/api/chat", json={"conversation_id": ident, "message": "本文を要約",
                           "attachment_ids": [attachment.json()["id"]]}) as response:
            assert response.status_code == 200
            assert list(_events(response))[-1][1]["status"] == "complete"
        assert "プロキシ経由の本文" in fake_llm.payloads[-1]["messages"][-1]["content"]
        assert len(requests) == 1, "LLM chat unexpectedly used the external-web proxy"
