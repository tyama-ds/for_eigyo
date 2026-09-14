"""小さな共通ユーティリティ（ID、時刻、正規化、文字コード自動判定）。"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
import uuid
from datetime import datetime, timezone
from pathlib import Path


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def new_id(prefix: str = "") -> str:
    token = uuid.uuid4().hex[:12]
    return f"{prefix}{token}" if prefix else token


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def json_dumps(obj, **kw) -> str:
    kw.setdefault("ensure_ascii", False)
    return json.dumps(obj, **kw)


def canonical_json(obj) -> str:
    """多数決のキーに使う正規化 JSON（キー順・空白を固定）。"""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


_WS_RE = re.compile(r"\s+")


def nfkc(text: str) -> str:
    return unicodedata.normalize("NFKC", text or "")


def norm_text(text: str) -> str:
    """照合用の正規化: NFKC → 小文字 → 空白除去。"""
    return _WS_RE.sub("", nfkc(text).lower())


def decode_auto(data: bytes) -> tuple[str, str]:
    """UTF-8（BOM 付き含む）→ cp932 の順に試し、(テキスト, 使用したコーデック) を返す。"""
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8", errors="replace"), "utf-8-sig"
    for enc in ("utf-8", "cp932"):
        try:
            return data.decode(enc), enc
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace"), "utf-8(replace)"


def read_text_auto(path: str | Path) -> tuple[str, str]:
    return decode_auto(Path(path).read_bytes())


def clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def safe_div(a: float, b: float, default: float | None = None) -> float | None:
    return a / b if b else default
