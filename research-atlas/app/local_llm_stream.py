"""Bounded local model streaming, with completion checks and text-free progress.

LM Studio uses OpenAI-compatible SSE and Ollama uses newline-delimited JSON.
A reader thread lets the coordinator enforce deadlines even during a stalled
socket read. The socket still has its own finite timeout; cancellation closes
the response and never waits indefinitely for that reader.
"""
from __future__ import annotations

import codecs
import json
import queue
import re
import threading
import time
from typing import Callable

import httpx


FIRST_RESPONSE_TIMEOUT_SECONDS = 600.0
READ_TIMEOUT_SECONDS = 180.0
TOTAL_TIMEOUT_SECONDS = 1200.0
MAX_RESPONSE_BYTES = 2_000_000
PROGRESS_INTERVAL_SECONDS = 2.0


class LocalStreamError(RuntimeError):
    """An intentionally sanitized failure, suitable for a local job status."""

    def __init__(self, message: str, *, kind: str | None = None):
        super().__init__(message)
        self.kind = kind


_INCOMPLETE = "ローカルLLMの応答が完了前に途切れました。途中のJSONは採用していません。モデルのログ・接続状態を確認して再試行してください。"
_MALFORMED = "ローカルLLMのストリームまたはJSON回答の形式が不正です。構造化出力に対応するモデルを確認してください。途中の回答は採用していません。"
_LENGTH = "ローカルLLMの出力がトークン上限に達し、回答が未完了です。入力範囲を絞るか、短い回答に対応するモデルで再試行してください。途中のJSONは採用していません。"
_FIRST_TIMEOUT = "ローカルLLMの最初の応答を待つ制限時間（10分）に達しました。入力の前処理・モデルの読み込み状況とサーバーのログを確認してください。回答は採用していません。"
_TOTAL_TIMEOUT = "ローカルLLMの生成が全体の制限時間（20分）に達しました。回答は未完了のため採用していません。入力範囲・モデルの処理速度を確認してください。"


def _error(exc: Exception) -> LocalStreamError:
    if isinstance(exc, LocalStreamError):
        return exc
    if isinstance(exc, httpx.ConnectTimeout):
        return LocalStreamError("ローカルLLMへの接続がタイムアウトしました。サーバーの起動と接続先を確認してください。")
    if isinstance(exc, httpx.ReadTimeout):
        return LocalStreamError("ローカルLLMからの受信が長時間停止したため、通信をタイムアウトしました。モデルの処理状況とサーバーのログを確認してください。途中のJSONは採用していません。", kind="read_timeout")
    if isinstance(exc, httpx.TimeoutException):
        return LocalStreamError("ローカルLLMからの受信が長時間停止したため、通信をタイムアウトしました。モデルの読み込み状態・処理負荷を確認してください。途中のJSONは採用していません。")
    if isinstance(exc, httpx.ConnectError):
        return LocalStreamError("ローカルLLMに接続できません。サーバーの起動と接続先を確認してください。")
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        hint = "認証情報を確認してください。" if status in {401, 403} else "接続先とモデル名を確認してください。" if status == 404 else "入力長・構造化出力対応・モデルのログを確認してください。"
        return LocalStreamError(f"ローカルLLMがHTTP {status}を返しました。{hint}応答本文は保存していません。")
    if isinstance(exc, httpx.RemoteProtocolError):
        return LocalStreamError(_INCOMPLETE, kind="incomplete")
    if isinstance(exc, (ValueError, TypeError, KeyError, UnicodeError)):
        return LocalStreamError(_MALFORMED, kind="malformed_json")
    return LocalStreamError("ローカルLLMの通信または応答処理に失敗しました。モデルの起動状態を確認してください。途中のJSONは採用していません。")


def _json_object(text: str) -> dict:
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("duplicate JSON key")
            value[key] = item
        return value
    def invalid_constant(_value):
        raise ValueError("non-finite JSON number")
    value = json.loads(text, object_pairs_hook=unique, parse_constant=invalid_constant)
    if not isinstance(value, dict):
        raise ValueError("JSON object required")
    return value


def _final_answer_object(text: str) -> dict:
    """Unwrap only recognized final-answer envelopes, never JSON inside reasoning."""
    answer = text.strip()
    if answer.startswith("<think>"):
        end = answer.find("</think>", len("<think>"))
        if end < 0:
            raise LocalStreamError("思考タグ <think> が閉じておらず、最終回答を確認できませんでした。途中の回答は採用していません。", kind="reasoning_incomplete")
        if "<think>" in answer[len("<think>"):end]:
            raise LocalStreamError(_MALFORMED, kind="malformed_json")
        answer = answer[end + len("</think>"):].strip()
    if not answer:
        raise LocalStreamError("ローカルLLMから最終回答のJSONが返りませんでした。思考部分だけで終了していないか、モデルのログを確認してください。", kind="missing_final_answer")
    # Accept a single complete JSON fence, not arbitrary prose or brace extraction.
    fenced = re.fullmatch(r"```(?:json)?[ \t]*\r?\n([\s\S]*?)\r?\n```", answer, re.IGNORECASE)
    if fenced:
        answer = fenced.group(1)
    return _json_object(answer)


class _Parser:
    def __init__(self, backend: str):
        self.backend = backend
        self.decoder = codecs.getincrementaldecoder("utf-8-sig")("strict")
        self.pending = ""
        self.data: list[str] = []
        self.event = ""
        self.parts: list[str] = []
        self.received_chars = 0
        self.stopped = False
        self.completed = False

    def _content(self, content):
        if content is None:
            return
        if not isinstance(content, str) or self.stopped and content:
            raise LocalStreamError(_MALFORMED, kind="malformed_json")
        self.parts.append(content)
        self.received_chars += len(content)

    def _sse(self, text: str):
        if self.completed:
            raise LocalStreamError(_MALFORMED, kind="malformed_json")
        if text == "[DONE]":
            if not self.stopped:
                raise LocalStreamError(_INCOMPLETE, kind="incomplete")
            self.completed = True
            return
        record = _json_object(text)
        if "error" in record or self.event == "error":
            raise LocalStreamError("ローカルLLMが生成中のエラーを通知しました。モデルのログを確認してください。途中のJSONは採用していません。")
        choices = record.get("choices")
        if not isinstance(choices, list) or len(choices) > 1:
            raise LocalStreamError(_MALFORMED, kind="malformed_json")
        if not choices:
            if "usage" not in record:
                raise LocalStreamError(_MALFORMED, kind="malformed_json")
            return
        choice = choices[0]
        if not isinstance(choice, dict) or choice.get("index", 0) != 0:
            raise LocalStreamError(_MALFORMED, kind="malformed_json")
        delta = choice.get("delta", {})
        if not isinstance(delta, dict) or delta.get("role") not in {None, "assistant"}:
            raise LocalStreamError(_MALFORMED, kind="malformed_json")
        if delta.get("tool_calls") or delta.get("function_call") or delta.get("refusal"):
            raise LocalStreamError("ローカルLLMが要求したJSON以外の応答を返しました。構造化出力対応を確認してください。")
        self._content(delta.get("content"))
        reason = choice.get("finish_reason")
        if reason == "length":
            raise LocalStreamError(_LENGTH, kind="token_limit")
        if reason is not None:
            if reason != "stop" or self.stopped:
                raise LocalStreamError(_INCOMPLETE, kind="incomplete")
            self.stopped = True

    def _ndjson(self, text: str):
        record = _json_object(text)
        if "error" in record:
            raise LocalStreamError("ローカルLLMが生成中のエラーを通知しました。モデルのログを確認してください。途中のJSONは採用していません。")
        if type(record.get("done")) is not bool:
            raise LocalStreamError(_MALFORMED, kind="malformed_json")
        message = record.get("message", {})
        if not isinstance(message, dict) or message.get("tool_calls"):
            raise LocalStreamError(_MALFORMED, kind="malformed_json")
        self._content(message.get("content"))
        if record["done"]:
            if record.get("done_reason") == "length":
                raise LocalStreamError(_LENGTH, kind="token_limit")
            if record.get("done_reason") != "stop":
                raise LocalStreamError(_INCOMPLETE, kind="incomplete")
            self.stopped = self.completed = True

    def _line(self, line: str):
        if self.backend == "ollama":
            if line.strip():
                self._ndjson(line)
            return
        if not line:
            if self.data:
                self._sse("\n".join(self.data))
            self.data, self.event = [], ""
        elif line.startswith(":"):
            return
        else:
            field, _, value = line.partition(":")
            value = value[1:] if value.startswith(" ") else value
            if field == "data":
                self.data.append(value)
            elif field == "event":
                self.event = value
            elif field not in {"id", "retry"}:
                raise LocalStreamError(_MALFORMED, kind="malformed_json")

    def feed(self, chunk: bytes, *, eof=False):
        self.pending += self.decoder.decode(chunk, final=eof)
        while not self.completed:
            offsets = [position for mark in ("\r", "\n") if (position := self.pending.find(mark)) >= 0]
            if not offsets:
                break
            index = min(offsets)
            if self.pending[index] == "\r" and index == len(self.pending) - 1 and not eof:
                break
            width = 2 if self.pending[index:index + 2] == "\r\n" else 1
            line, self.pending = self.pending[:index], self.pending[index + width:]
            self._line(line)
        if eof and not self.completed:
            if self.pending:
                self._line(self.pending)
                self.pending = ""
            if self.backend != "ollama" and self.data:
                self._line("")
        if eof and not self.completed:
            raise LocalStreamError(_INCOMPLETE, kind="incomplete")

    def result(self) -> dict:
        if not self.completed or not self.stopped:
            raise LocalStreamError(_INCOMPLETE, kind="incomplete")
        return _final_answer_object("".join(self.parts))


def stream_json(client: httpx.Client, url: str, body: dict, backend: str,
                progress: Callable[[dict], None] | None = None) -> dict:
    """Return only a complete JSON object with a successful protocol terminator."""
    started = last_received = time.monotonic()
    has_received = False
    last_notified = started - PROGRESS_INTERVAL_SECONDS
    events: queue.Queue = queue.Queue(maxsize=4)
    cancelled = threading.Event()
    responses: list[httpx.Response] = []
    parser = _Parser(backend)

    def notify(*, force=False):
        nonlocal last_notified
        now = time.monotonic()
        if progress and (force or now - last_notified >= PROGRESS_INTERVAL_SECONDS):
            last_notified = now
            try:
                progress({"elapsed_seconds": round(max(0, now - started), 1), "received_chars": parser.received_chars})
            except Exception:
                # A display callback must not discard a successfully generated answer.
                pass

    def send(kind, value):
        while not cancelled.is_set():
            try:
                events.put((kind, value), timeout=0.2)
                return
            except queue.Full:
                pass

    def read():
        try:
            with client.stream("POST", url, json=body) as response:
                responses.append(response)
                response.raise_for_status()
                received = 0
                for chunk in response.iter_bytes():
                    if cancelled.is_set():
                        return
                    received += len(chunk)
                    if received > MAX_RESPONSE_BYTES:
                        raise LocalStreamError("ローカルLLMの受信量が2MBの上限を超えました。回答は採用していません。")
                    if chunk:
                        send("chunk", chunk)
            send("eof", None)
        except Exception as exc:
            send("error", _error(exc))

    notify(force=True)
    reader = threading.Thread(target=read, name="atlas-local-llm-stream", daemon=True)
    reader.start()
    try:
        while not parser.completed:
            now = time.monotonic()
            total_remaining = TOTAL_TIMEOUT_SECONDS - (now - started)
            idle_remaining = (READ_TIMEOUT_SECONDS if has_received else FIRST_RESPONSE_TIMEOUT_SECONDS) - (now - last_received)
            if total_remaining <= 0:
                raise LocalStreamError(_TOTAL_TIMEOUT)
            if idle_remaining <= 0:
                if not has_received:
                    raise LocalStreamError(_FIRST_TIMEOUT)
                raise LocalStreamError("ローカルLLMから180秒間受信がないため、通信をタイムアウトしました。モデルの読み込み状態・処理負荷を確認してください。途中のJSONは採用していません。")
            try:
                kind, value = events.get(timeout=min(0.5, total_remaining, idle_remaining))
            except queue.Empty:
                notify()
                continue
            if kind == "error":
                if not has_received and getattr(value, "kind", None) == "read_timeout":
                    raise LocalStreamError(_FIRST_TIMEOUT)
                raise value
            if kind == "chunk":
                has_received = True
                last_received = time.monotonic()
                parser.feed(value)
            else:
                parser.feed(b"", eof=True)
            notify()
        if time.monotonic() - started >= TOTAL_TIMEOUT_SECONDS:
            raise LocalStreamError(_TOTAL_TIMEOUT)
        parsed = parser.result()
        notify(force=True)
        return parsed
    except Exception as exc:
        raise _error(exc) from None
    finally:
        cancelled.set()
        for response in responses:
            try:
                response.close()
            except Exception:
                pass
        reader.join(timeout=0.2)
