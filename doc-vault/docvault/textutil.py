"""テキスト処理（チャンク分割 / トークン化 / 転置インデックス BM25 / ベクトル / JSON 抽出）。

依存なしで日本語・英語を扱う:
- トークン化は 英数字の単語 + CJK 文字バイグラム（形態素解析なしの定番手法）
- チャンク分割は段落・文境界を優先し、文書の構造（ページ・スライド・見出し）単位で行う
- BM25 は転置インデックス方式（質問の語を含むチャンクだけを走査）で数万チャンクでも実用速度
"""
from __future__ import annotations

import json
import math
import re
import unicodedata
from array import array
from collections import Counter, defaultdict

try:                                    # あれば高速化に使う（無くても動く）
    import numpy as _np
except ImportError:                     # pragma: no cover - 環境依存
    _np = None

# ---------------------------------------------------------------- 正規化 / チャンク分割

_SENT_RE = re.compile(r"(?<=[。．！？!?\.])\s*")


def normalize_text(text: str) -> str:
    """抽出テキストの正規化: NFKC（全角英数→半角等）・制御文字除去・空白の整理。"""
    text = unicodedata.normalize("NFKC", text or "")
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
    text = re.sub(r"[\x01-\x08\x0b\x0c\x0e-\x1f]", " ", text)
    text = re.sub(r"[ \t　]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def split_chunks(text: str, *, size: int = 900, overlap: int = 100) -> list[str]:
    """段落→文の順で境界を尊重しつつ、およそ size 文字のチャンクへ分割する。"""
    text = text.replace("\r\n", "\n").strip()
    if not text:
        return []
    if len(text) <= size:
        return [text]
    pieces: list[str] = []
    for para in re.split(r"\n\s*\n", text):
        para = para.strip()
        if not para:
            continue
        if len(para) <= size:
            pieces.append(para)
            continue
        for sent in _SENT_RE.split(para):
            sent = sent.strip()
            if not sent:
                continue
            while len(sent) > size:                      # 句点のない超長文は強制分割
                pieces.append(sent[:size])
                sent = sent[size - overlap:]
            pieces.append(sent)
    chunks: list[str] = []
    buf = ""
    for piece in pieces:
        if buf and len(buf) + len(piece) + 1 > size:
            chunks.append(buf)
            buf = buf[-overlap:] if overlap else ""      # 末尾を次チャンクへ持ち越し
            buf = buf + ("\n" if buf else "") + piece
        else:
            buf = (buf + "\n" + piece) if buf else piece
    if buf.strip():
        chunks.append(buf)
    return [c.strip() for c in chunks if c.strip()]


def build_chunks(sections: list[dict], *, size: int = 900, overlap: int = 100,
                 min_merge: int = 200) -> list[dict]:
    """構造化セクション（ページ・スライド・シート・見出し単位）をチャンク列へ。

    短いセクション（スライド1枚に数十文字など）は同じ見出しが続く限り結合し、
    チャンクの位置情報（locator）は「p.3-4」のように範囲で持つ。
    """
    chunks: list[dict] = []
    buf: dict | None = None

    def flush():
        nonlocal buf
        if buf and buf["text"].strip():
            chunks.append(buf)
        buf = None

    for sec in sections:
        text = (sec.get("text") or "").strip()
        if not text:
            continue
        loc = sec.get("locator") or ""
        heading = sec.get("heading") or ""
        page = sec.get("page")
        if len(text) < min_merge and buf and len(buf["text"]) + len(text) < size \
                and buf["heading"] == heading:
            buf["text"] += "\n\n" + text
            buf["locator_end"] = loc
            continue
        flush()
        parts = split_chunks(text, size=size, overlap=overlap)
        for i, part in enumerate(parts):
            item = {"text": part, "locator": loc, "locator_end": loc,
                    "heading": heading, "page": page}
            if i < len(parts) - 1 or len(part) >= min_merge:
                chunks.append(item)
            else:
                buf = item                               # 末尾の短片は次と結合できるよう保留
    flush()
    for i, ch in enumerate(chunks):
        if ch["locator_end"] and ch["locator_end"] != ch["locator"]:
            ch["locator"] = _merge_locators(ch["locator"], ch["locator_end"])
        ch.pop("locator_end", None)
        ch["ord"] = i
    return chunks


def _merge_locators(a: str, b: str) -> str:
    """'p.3' + 'p.5' → 'p.3-5'。形式が違えばそのまま連結。"""
    ma, mb = re.match(r"^(.*?)(\d+)$", a), re.match(r"^(.*?)(\d+)$", b)
    if ma and mb and ma.group(1) == mb.group(1):
        return f"{a}-{mb.group(2)}"
    return f"{a}〜{b}"


# ---------------------------------------------------------------- トークン化

_WORD_RE = re.compile(r"[a-z0-9_][a-z0-9_\-\.]*[a-z0-9_]|[a-z0-9_]")
_CJK_RE = re.compile(r"[぀-ヿ㐀-鿿豈-﫿]+")


def tokenize(text: str) -> list[str]:
    """英数字の単語 + CJK バイグラム（1文字の CJK 語はそのまま）。"""
    text = unicodedata.normalize("NFKC", text or "").lower()
    tokens = _WORD_RE.findall(text)
    for run in _CJK_RE.findall(text):
        if len(run) == 1:
            tokens.append(run)
        else:
            tokens.extend(run[i:i + 2] for i in range(len(run) - 1))
    return tokens


# ---------------------------------------------------------------- BM25（転置インデックス）

class BM25Index:
    """追加・削除できる転置インデックス BM25（Okapi）。キーは任意の文字列 ID。"""

    def __init__(self, *, k1: float = 1.4, b: float = 0.72):
        self.k1, self.b = k1, b
        self.postings: dict[str, dict[str, int]] = defaultdict(dict)
        self.doc_len: dict[str, int] = {}
        self.total_len = 0

    def __len__(self) -> int:
        return len(self.doc_len)

    def add(self, key: str, text: str) -> None:
        if key in self.doc_len:
            self.remove(key)
        tf = Counter(tokenize(text))
        for tok, n in tf.items():
            self.postings[tok][key] = n
        length = sum(tf.values())
        self.doc_len[key] = length
        self.total_len += length

    def remove(self, key: str) -> None:
        length = self.doc_len.pop(key, None)
        if length is None:
            return
        self.total_len -= length
        # 削除は頻繁ではないので全語走査で十分
        empty = []
        for tok, post in self.postings.items():
            if post.pop(key, None) is not None and not post:
                empty.append(tok)
        for tok in empty:
            del self.postings[tok]

    def search(self, query: str, k: int = 20, *, allow=None) -> list[tuple[str, float]]:
        """(key, score) を score 降順で返す。allow(key)->bool で絞り込み可能。"""
        n = len(self.doc_len)
        if not n:
            return []
        avgdl = self.total_len / n or 1.0
        scores: dict[str, float] = defaultdict(float)
        for tok in set(tokenize(query)):
            post = self.postings.get(tok)
            if not post:
                continue
            df = len(post)
            idf = math.log(1 + (n - df + 0.5) / (df + 0.5))
            for key, f in post.items():
                denom = f + self.k1 * (1 - self.b + self.b * self.doc_len[key] / avgdl)
                scores[key] += idf * f * (self.k1 + 1) / denom
        items = [(key, s) for key, s in scores.items() if s > 0 and (allow is None or allow(key))]
        items.sort(key=lambda t: (-t[1], t[0]))
        return items[:k]


# ---------------------------------------------------------------- ベクトル

def pack_vec(vec: list[float]) -> bytes:
    return array("f", vec).tobytes()


def unpack_vec(blob: bytes) -> list[float]:
    arr = array("f")
    arr.frombytes(blob)
    return arr.tolist()


def _norm(vec):
    s = math.sqrt(sum(x * x for x in vec))
    return [x / s for x in vec] if s else vec


class VectorIndex:
    """総当たりのコサイン類似検索（numpy があれば行列演算、無ければ純 Python）。"""

    def __init__(self):
        self.keys: list[str] = []
        self.vecs: list[list[float]] = []
        self._mat = None

    def __len__(self) -> int:
        return len(self.keys)

    def add(self, key: str, vec: list[float]) -> None:
        self.keys.append(key)
        self.vecs.append(_norm(vec))
        self._mat = None

    def search(self, qvec: list[float], k: int = 20, *, allow=None) -> list[tuple[str, float]]:
        if not self.keys:
            return []
        q = _norm(qvec)
        if len(q) != len(self.vecs[0]):
            return []
        if _np is not None:
            if self._mat is None:
                self._mat = _np.asarray(self.vecs, dtype="float32")
            sims = (self._mat @ _np.asarray(q, dtype="float32")).tolist()
        else:
            sims = [sum(a * b for a, b in zip(q, v)) for v in self.vecs]
        order = sorted(range(len(sims)), key=lambda i: -sims[i])
        out = []
        for i in order:
            if allow is not None and not allow(self.keys[i]):
                continue
            out.append((self.keys[i], float(sims[i])))
            if len(out) >= k:
                break
        return out


def rrf_fuse(rankings: list[list[str]], *, k: int = 60,
             weights: list[float] | None = None) -> list[tuple[str, float]]:
    """Reciprocal Rank Fusion。rankings は各リトリーバーのキー列（順位順）。"""
    score: dict[str, float] = defaultdict(float)
    for idx, ranking in enumerate(rankings):
        w = weights[idx] if weights else 1.0
        for rank, key in enumerate(ranking):
            score[key] += w / (k + rank + 1)
    return sorted(score.items(), key=lambda t: (-t[1], t[0]))


# ---------------------------------------------------------------- LLM 出力の JSON 抽出

_FULLWIDTH_JSON = str.maketrans({
    "”": '"', "“": '"', "〝": '"', "〟": '"', "″": '"',
    "’": "'", "‘": "'",
    "：": ":", "，": ",",
    "｛": "{", "｝": "}", "［": "[", "］": "]",
})


def _scan_json(text: str):
    start = next((i for i, ch in enumerate(text) if ch in "[{"), None)
    if start is None:
        return None
    depth, in_str, esc = 0, False, False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
            if depth == 0:
                candidate = text[start:i + 1]
                for attempt in (candidate, re.sub(r",\s*([\]}])", r"\1", candidate)):
                    try:
                        return json.loads(attempt)
                    except ValueError:
                        continue
                return None
    return None


def extract_json(text: str):
    """LLM 応答から最初の JSON オブジェクト/配列を取り出す。失敗時 None。

    コードフェンス・前後の説明文・末尾カンマ・全角記号に耐える。
    """
    if not text:
        return None
    text = re.sub(r"```(?:json)?", "", text)
    translated = text.translate(_FULLWIDTH_JSON)
    first = next((i for i, ch in enumerate(text) if ch in "[{"), len(text))
    first_t = next((i for i, ch in enumerate(translated) if ch in "[{"), len(translated))
    for candidate in ((translated, text) if first_t < first else (text, translated)):
        result = _scan_json(candidate)
        if result is not None:
            return result
    return None


def parse_score(value, *, default: int = 0, lo: int = 0, hi: int = 10) -> int:
    """LLM のスコア/強度を寛容に整数化（7 / "7" / "8/10" / "１０" / "高"）。"""
    if isinstance(value, bool) or value is None:
        return default
    if isinstance(value, (int, float)):
        return max(lo, min(hi, int(value)))
    if isinstance(value, str):
        s = unicodedata.normalize("NFKC", value).casefold()
        m = re.search(r"-?\d+", s)
        if m:
            return max(lo, min(hi, int(m.group())))
        for words, mapped in ((("高", "強", "high", "strong"), 7),
                              (("中", "medium", "moderate"), 5),
                              (("低", "弱", "low", "weak"), 3)):
            if any(w in s for w in words):
                return max(lo, min(hi, mapped))
    return default


def as_str_list(value, *, limit: int = 20, maxlen: int = 80) -> list[str]:
    """LLM が返す「文字列の配列」を寛容に受ける（文字列1個・区切り文字列も可）。"""
    if isinstance(value, str):
        value = re.split(r"[、,，;；\n]", value)
    if not isinstance(value, list):
        return []
    out = []
    for v in value:
        s = str(v).strip().strip("\"'「」")
        if s and s not in out:
            out.append(s[:maxlen])
        if len(out) >= limit:
            break
    return out
