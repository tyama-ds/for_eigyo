"""テキスト処理ユーティリティ（チャンク分割 / トークン化 / BM25 / ベクトル演算 / JSON 抽出）。

依存なしで日本語・英語の両方をそれなりに扱う:
- トークン化は 英数字の単語 + CJK 文字バイグラム（形態素解析なしの定番手法）
- チャンク分割は段落・文境界を優先して指定文字数に収める
"""
from __future__ import annotations

import json
import math
import re
import unicodedata
from collections import Counter

# ---------------------------------------------------------------- チャンク分割

_SENT_RE = re.compile(r"(?<=[。．！？!?\.])\s*")


def split_chunks(text: str, *, size: int = 1200, overlap: int = 120) -> list[str]:
    """段落→文の順で境界を尊重しつつ、およそ size 文字のチャンクへ分割する。"""
    text = text.replace("\r\n", "\n").strip()
    if not text:
        return []
    if len(text) <= size:
        return [text]

    # 段落単位に割り、長すぎる段落は文単位へさらに割る
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


# ---------------------------------------------------------------- トークン化

_WORD_RE = re.compile(r"[a-z0-9_]+")
_CJK_RE = re.compile(r"[぀-ヿ㐀-鿿豈-﫿]+")


def tokenize(text: str) -> list[str]:
    """英数字の単語 + CJK バイグラム。"""
    text = text.lower()
    tokens = _WORD_RE.findall(text)
    for run in _CJK_RE.findall(text):
        if len(run) == 1:
            tokens.append(run)
        else:
            tokens.extend(run[i:i + 2] for i in range(len(run) - 1))
    return tokens


# ---------------------------------------------------------------- BM25

class BM25:
    """純Python の BM25（Okapi）。数百〜数千チャンク規模を想定。"""

    def __init__(self, docs_tokens: list[list[str]], *, k1: float = 1.5, b: float = 0.75):
        self.k1, self.b = k1, b
        self.doc_tf = [Counter(toks) for toks in docs_tokens]
        self.doc_len = [len(toks) for toks in docs_tokens]
        self.avgdl = (sum(self.doc_len) / len(self.doc_len)) if self.doc_len else 0.0
        self.df: Counter = Counter()
        for tf in self.doc_tf:
            self.df.update(tf.keys())
        self.n_docs = len(docs_tokens)

    def scores(self, query: str) -> list[float]:
        q_tokens = tokenize(query)
        out = [0.0] * self.n_docs
        for tok in set(q_tokens):
            df = self.df.get(tok)
            if not df:
                continue
            idf = math.log(1 + (self.n_docs - df + 0.5) / (df + 0.5))
            for i, tf in enumerate(self.doc_tf):
                f = tf.get(tok)
                if not f:
                    continue
                denom = f + self.k1 * (1 - self.b + self.b * self.doc_len[i] / (self.avgdl or 1))
                out[i] += idf * f * (self.k1 + 1) / denom
        return out

    def top_k(self, query: str, k: int = 6) -> list[tuple[int, float]]:
        scores = self.scores(query)
        order = sorted(range(len(scores)), key=lambda i: (-scores[i], i))
        return [(i, scores[i]) for i in order[:k] if scores[i] > 0]


# ---------------------------------------------------------------- ベクトル

def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def top_k_cosine(query_vec: list[float], vecs: list[list[float]], k: int = 6,
                 ) -> list[tuple[int, float]]:
    scored = [(i, cosine(query_vec, v)) for i, v in enumerate(vecs)]
    scored.sort(key=lambda t: (-t[1], t[0]))
    return scored[:k]


def rrf_fuse(rankings: list[list[int]], *, k: int = 60) -> list[int]:
    """Reciprocal Rank Fusion。rankings は各リトリーバーの文書IDリスト（順位順）。"""
    score: dict[int, float] = {}
    for ranking in rankings:
        for rank, doc_id in enumerate(ranking):
            score[doc_id] = score.get(doc_id, 0.0) + 1.0 / (k + rank + 1)
    return sorted(score, key=lambda d: (-score[d], d))


# ---------------------------------------------------------------- LLM 出力の JSON 抽出

# 日本語ローカルLLMが出しがちな全角記号 → ASCII（「JSONもどき」の修復用）
_FULLWIDTH_JSON = str.maketrans({
    "”": '"', "“": '"', "〝": '"', "〟": '"', "″": '"',
    "’": "'", "‘": "'",
    "：": ":", "，": ",",
    "｛": "{", "｝": "}", "［": "[", "］": "]",
})


def _scan_json(text: str):
    """最初のバランスした JSON オブジェクト/配列を探して parse する。失敗時 None。"""
    start = None
    for i, ch in enumerate(text):
        if ch in "[{":
            start = i
            break
    if start is None:
        return None
    depth = 0
    in_str = False
    esc = False
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
                for attempt in (candidate,
                                re.sub(r",\s*([\]}])", r"\1", candidate)):  # 末尾カンマ除去
                    try:
                        return json.loads(attempt)
                    except ValueError:
                        continue
                return None
    # 閉じ括弧が欠けた場合は諦める（部分 JSON の復元はしない）
    return None


def extract_json(text: str):
    """LLM 応答から最初の JSON オブジェクト/配列を取り出して parse する。

    コードフェンス・前後の説明文・末尾カンマ・全角記号（”／：等）に耐える。失敗時は None。
    """
    if not text:
        return None
    text = re.sub(r"```(?:json)?", "", text)
    translated = text.translate(_FULLWIDTH_JSON)
    # 全角の開き括弧が先に現れる（＝外側の構造が全角で書かれている）なら変換版を先に試す
    first = next((i for i, ch in enumerate(text) if ch in "[{"), len(text))
    first_t = next((i for i, ch in enumerate(translated) if ch in "[{"), len(translated))
    order = (translated, text) if first_t < first else (text, translated)
    for candidate in order:
        result = _scan_json(candidate)
        if result is not None:
            return result
    return None


def parse_score(value, *, default: int = 0, lo: int = 0, hi: int = 10) -> int:
    """LLM が返すスコア/強度を寛容に整数化する。

    7 / "7" / "8/10" / "１０"（全角）/ "高" などを受け付け、範囲にクランプする。
    解釈できなければ default。
    """
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
