"""ログ・イベント・エラーメッセージからのsecret redaction。

known secretの値そのもの、およびAuthorization/api key風のパターンを [REDACTED] に置換する。
"""

from __future__ import annotations

import re
import threading

_PATTERNS = [
    # Authorization: Bearer xxx / Basic xxx
    re.compile(r"(?i)(authorization\s*[:=]\s*)(bearer|basic)?\s*[A-Za-z0-9+/_\-.=]{8,}"),
    # api key風のquery/env値
    re.compile(r"(?i)((?:api[-_]?key|x-api-key|apikey|token|secret)\s*[:=]\s*)[^\s&\"']{8,}"),
    # URL内の userinfo (http://user:pass@host)
    re.compile(r"(?i)(https?://)([^/@\s:]+):([^/@\s]+)@"),
    # OpenAI/Anthropic key形式
    re.compile(r"sk-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{16,}"),
]


class Redactor:
    """プロセス内のknown secret値を登録し、テキストから除去する。"""

    def __init__(self) -> None:
        self._values: set[str] = set()
        self._lock = threading.Lock()

    def register(self, value: str | None) -> None:
        if value and len(value) >= 6:
            with self._lock:
                self._values.add(value)

    def redact(self, text: str) -> str:
        if not text:
            return text
        with self._lock:
            values = sorted(self._values, key=len, reverse=True)
        for v in values:
            text = text.replace(v, "[REDACTED]")
        for pat in _PATTERNS:
            if pat.pattern.startswith("(?i)(https?://)"):
                text = pat.sub(r"\1\2:[REDACTED]@", text)
            elif pat.pattern.startswith("(?i)(authorization"):
                text = pat.sub(r"\1[REDACTED]", text)
            elif pat.pattern.startswith("(?i)((?:api"):
                text = pat.sub(r"\1[REDACTED]", text)
            else:
                text = pat.sub("[REDACTED]", text)
        return text


redactor = Redactor()


def redact(text: str) -> str:
    return redactor.redact(text)
