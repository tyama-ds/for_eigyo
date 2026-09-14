import json
import threading
import time

import httpx
import pytest

from app import local_llm_stream as streaming


class Chunks(httpx.SyncByteStream):
    def __init__(self, chunks, delay=0):
        self.chunks = chunks
        self.delay = delay
        self.closed = threading.Event()

    def __iter__(self):
        for chunk in self.chunks:
            if self.closed.wait(self.delay):
                return
            yield chunk

    def close(self):
        self.closed.set()


def sse(content=None, reason=None, **extra):
    delta = {} if content is None else {"content": content}
    value = {"choices": [{"index": 0, "delta": delta, "finish_reason": reason, **extra}]}
    return ("data: " + json.dumps(value, ensure_ascii=False) + "\n\n").encode()


def ndjson(content="", done=False, reason=None):
    value = {"message": {"content": content}, "done": done}
    if reason is not None:
        value["done_reason"] = reason
    return (json.dumps(value, ensure_ascii=False) + "\n").encode()


def invoke(chunks, *, backend="openai_compatible", progress=None, status=200):
    stream = chunks if isinstance(chunks, Chunks) else Chunks(chunks)
    def handler(request):
        assert request.method == "POST" and json.loads(request.content)["stream"] is True
        return httpx.Response(status, stream=stream)
    with httpx.Client(transport=httpx.MockTransport(handler), trust_env=False) as client:
        result = streaming.stream_json(client, "http://127.0.0.1:1234/v1/chat/completions",
            {"stream": True}, backend, progress)
    assert stream.closed.is_set()
    return result


@pytest.mark.parametrize("width", [1, 3, 13, 4096])
@pytest.mark.parametrize("newline", [b"\n", b"\r\n", b"\r"])
def test_sse_fragmented_utf8_lines_and_successful_terminators(width, newline):
    data = b"\xef\xbb\xbf: keepalive\n\n" + sse('{"result":"引張') + sse('強度"}', "stop") + b"data: [DONE]\n\n"
    data = data.replace(b"\n", newline)
    chunks = [data[index:index + width] for index in range(0, len(data), width)]
    assert invoke(chunks) == {"result": "引張強度"}


def test_sse_supports_multiline_data_role_only_usage_and_empty_delta():
    data = b'data: {\ndata: "choices": [{"index":0,"delta":{"role":"assistant"},"finish_reason":null}]}\n\n'
    data += sse('{"ok":true}', "stop")
    data += b'data: {"choices":[],"usage":{"total_tokens":12}}\n\ndata: [DONE]\n\n'
    assert invoke([data]) == {"ok": True}


@pytest.mark.parametrize("chunks,match", [
    ([sse('{"ok":true}')], "完了前"),
    ([sse('{"ok":true}', "stop")], "完了前"),
    ([sse('{"ok":true}'), b"data: [DONE]\n\n"], "完了前"),
    ([sse('{"ok":true}', "length"), b"data: [DONE]\n\n"], "トークン上限"),
    ([sse('{"ok":true}', "content_filter"), b"data: [DONE]\n\n"], "完了前"),
    ([sse('{"ok":', "stop"), b"data: [DONE]\n\n"], "形式"),
    ([sse('{"ok":NaN}', "stop"), b"data: [DONE]\n\n"], "形式"),
    ([sse('{"ok":1,"ok":2}', "stop"), b"data: [DONE]\n\n"], "形式"),
    ([sse('[]', "stop"), b"data: [DONE]\n\n"], "形式"),
    ([b'data: {"error":{"message":"private-secret"}}\n\n'], "生成中"),
    ([b'data: {"choices":[{"delta":{"refusal":"private-secret"}}]}\n\n'], "JSON以外"),
    ([b'data: {"choices":[{"delta":{"tool_calls":[{}]}}]}\n\n'], "JSON以外"),
    ([b'data: {"choices":"private-secret"}\n\n'], "形式"),
    ([b'data: \xff\n\n'], "形式"),
    ([b'not-an-event: private-secret\n'], "形式"),
])
def test_sse_rejects_truncation_non_json_finish_reasons_and_protocol_errors(chunks, match):
    with pytest.raises(streaming.LocalStreamError, match=match) as caught:
        invoke(chunks)
    assert "private-secret" not in str(caught.value)


@pytest.mark.parametrize("width", [1, 11, 4096])
def test_ollama_ndjson_fragmentation_thinking_only_and_final_content(width):
    data = b'{"message":{"thinking":"private-thought"},"done":false}\n'
    data += ndjson('{"result":"引張') + ndjson('強度"}', True, "stop")
    assert invoke([data[index:index + width] for index in range(0, len(data), width)], backend="ollama") == {"result": "引張強度"}


@pytest.mark.parametrize("data,match", [
    (ndjson('{"ok":true}'), "完了前"),
    (ndjson('{"ok":true}', True), "完了前"),
    (ndjson('{"ok":true}', True, "length"), "トークン上限"),
    (ndjson('{"ok":', True, "stop"), "形式"),
    (b'{"message":{"content":"{}"},"done":"true","done_reason":"stop"}\n', "形式"),
    (b'{"error":"private-secret"}\n', "生成中"),
])
def test_ollama_requires_successful_done_and_valid_json(data, match):
    with pytest.raises(streaming.LocalStreamError, match=match) as caught:
        invoke([data], backend="ollama")
    assert "private-secret" not in str(caught.value)


def test_size_limit_counts_all_received_bytes_before_parsing(monkeypatch):
    monkeypatch.setattr(streaming, "MAX_RESPONSE_BYTES", 60)
    with pytest.raises(streaming.LocalStreamError, match="2MB"):
        invoke([b": " + b"x" * 80 + b"\n\n"])


@pytest.mark.parametrize("status,match", [(400, "HTTP 400"), (401, "認証"), (404, "モデル名"), (500, "HTTP 500")])
def test_http_error_is_distinct_and_does_not_read_or_echo_body(status, match):
    with pytest.raises(streaming.LocalStreamError, match=match) as caught:
        invoke([b"private-secret private-abstract"], status=status)
    assert "private-secret" not in str(caught.value)


@pytest.mark.parametrize("exception,match", [(httpx.ConnectTimeout, "接続がタイムアウト"),
    (httpx.ConnectError, "接続できません"), (httpx.ReadTimeout, "受信が長時間停止"),
    (httpx.RemoteProtocolError, "完了前")])
def test_socket_errors_are_distinguished_and_sanitized(exception, match):
    class Broken(Chunks):
        def __iter__(self):
            yield sse('{"ok":true}')
            raise exception("private-secret and private-abstract")
    with pytest.raises(streaming.LocalStreamError, match=match) as caught:
        invoke(Broken([]))
    assert "private-secret" not in str(caught.value)


def test_hard_deadline_cancels_a_silent_socket_without_waiting_for_socket_timeout(monkeypatch):
    monkeypatch.setattr(streaming, "TOTAL_TIMEOUT_SECONDS", 0.06)
    monkeypatch.setattr(streaming, "READ_TIMEOUT_SECONDS", 10)
    chunks = Chunks([sse('{"ok":true}', "stop"), b"data: [DONE]\n\n"], delay=10)
    started = time.monotonic()
    with pytest.raises(streaming.LocalStreamError, match="全体の制限時間"):
        invoke(chunks)
    assert time.monotonic() - started < 0.5
    assert chunks.closed.is_set()


def test_inactivity_deadline_is_distinct_from_total_deadline(monkeypatch):
    monkeypatch.setattr(streaming, "TOTAL_TIMEOUT_SECONDS", 10)
    monkeypatch.setattr(streaming, "FIRST_RESPONSE_TIMEOUT_SECONDS", 10)
    monkeypatch.setattr(streaming, "READ_TIMEOUT_SECONDS", 0.05)
    class Paused(Chunks):
        def __iter__(self):
            yield b": first-byte\n\n"
            self.closed.wait(10)
    chunks = Paused([])
    with pytest.raises(streaming.LocalStreamError, match="180秒間受信がない"):
        invoke(chunks)
    assert chunks.closed.is_set()


def test_first_response_can_exceed_ongoing_idle_allowance(monkeypatch):
    monkeypatch.setattr(streaming, "TOTAL_TIMEOUT_SECONDS", 2)
    monkeypatch.setattr(streaming, "FIRST_RESPONSE_TIMEOUT_SECONDS", 1)
    monkeypatch.setattr(streaming, "READ_TIMEOUT_SECONDS", 0.05)
    class SlowPrefill(Chunks):
        def __iter__(self):
            if self.closed.wait(0.12):
                return
            yield sse('{"ok":true}', "stop") + b"data: [DONE]\n\n"
    started = time.monotonic()
    assert invoke(SlowPrefill([])) == {"ok": True}
    assert time.monotonic() - started > streaming.READ_TIMEOUT_SECONDS


def test_first_response_deadline_is_bounded_and_reports_prefill_stage(monkeypatch):
    monkeypatch.setattr(streaming, "TOTAL_TIMEOUT_SECONDS", 10)
    monkeypatch.setattr(streaming, "FIRST_RESPONSE_TIMEOUT_SECONDS", 0.06)
    monkeypatch.setattr(streaming, "READ_TIMEOUT_SECONDS", 10)
    chunks = Chunks([sse('{"ok":true}', "stop")], delay=10)
    started = time.monotonic()
    with pytest.raises(streaming.LocalStreamError, match="最初の応答.*10分") as caught:
        invoke(chunks)
    assert "前処理" in str(caught.value) and "メモリ" not in str(caught.value)
    assert time.monotonic() - started < 0.5 and chunks.closed.is_set()


def test_socket_read_timeout_before_any_bytes_has_first_response_message():
    class NoResponse(Chunks):
        def __iter__(self):
            raise httpx.ReadTimeout("private connection details")
            yield b""  # This is a stream whose first read raises.
    with pytest.raises(streaming.LocalStreamError, match="最初の応答") as caught:
        invoke(NoResponse([]))
    assert "private" not in str(caught.value)


def test_active_generation_can_exceed_inactivity_limit_and_progress_is_text_free(monkeypatch):
    monkeypatch.setattr(streaming, "READ_TIMEOUT_SECONDS", 0.15)
    monkeypatch.setattr(streaming, "TOTAL_TIMEOUT_SECONDS", 2)
    monkeypatch.setattr(streaming, "PROGRESS_INTERVAL_SECONDS", 0.03)
    parts = ['{"result":"', *list("機密の文章を生成します"), '"}']
    chunks = Chunks([*(sse(part) for part in parts), sse(None, "stop"), b"data: [DONE]\n\n"], delay=0.018)
    events = []
    started = time.monotonic()
    assert invoke(chunks, progress=events.append) == {"result": "機密の文章を生成します"}
    assert time.monotonic() - started > streaming.READ_TIMEOUT_SECONDS
    assert len(events) >= 4
    assert events[0]["received_chars"] == 0
    assert events[-1]["received_chars"] == len("".join(parts))
    assert all(set(event) == {"elapsed_seconds", "received_chars"} for event in events)
    assert "機密" not in json.dumps(events, ensure_ascii=False)
    assert [event["received_chars"] for event in events] == sorted(event["received_chars"] for event in events)


def test_progress_callback_failure_does_not_discard_complete_result():
    def broken(_value):
        raise RuntimeError("private display error")
    assert invoke([sse('{"ok":true}', "stop"), b"data: [DONE]\n\n"], progress=broken) == {"ok": True}
