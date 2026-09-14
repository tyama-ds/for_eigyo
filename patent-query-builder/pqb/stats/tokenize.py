"""簡易トークナイザと語候補抽出。企画書 §10.2。

形態素解析器が使えない前提で、正規表現により
  「漢字の連続」「カタカナ（長音符含む）の連続」「英数字の連続」
を語単位として切り出し、
  - 隣接する単位の 1〜2 連結（例: 高強度＋ステンレス → 高強度ステンレス）
  - 漢字連続＋送り仮名 1〜2 文字（例: 焼入 → 焼入れ）
  - 4〜6 文字の漢字連続の部分文字列（例: 高強度鋼板 → 高強度、鋼板）
を候補にし、汎用語リストで除去する。ノイズは RSJ 重みと G2 の人の判断で落とす。
"""
from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path

from ..core.document import Document
from ..util import nfkc

_KANJI = "一-龥々〆ヶ"
_KATA = "ァ-ヴー"
_HIRA = "ぁ-ん"
UNIT_RE = re.compile(rf"[{_KANJI}]+|[{_KATA}]+|[A-Za-z][A-Za-z0-9\-\.]*[A-Za-z0-9]|[A-Za-z0-9]+")
KANJI_OKURI_RE = re.compile(rf"([{_KANJI}]+)([{_HIRA}]{{1,2}})")
# 送り仮名として扱わない助詞・接続（例: 鋼板の／鋼板を は語にしない）
_PARTICLE_CHARS = set("のをにがはとでもやへか")
_BAD_SUFFIX_END = _PARTICLE_CHARS | set("したてなさせすおまこそあ")
PARTICLES = {"の", "を", "に", "が", "は", "と", "で", "も", "や", "へ", "か", "な", "て", "た", "し",
             "から", "まで", "より", "など", "では", "には", "とは", "にも", "でも", "への", "との", "での",
             "する", "した", "して", "され", "せる", "れる", "ない", "なる", "なり", "よう", "こと", "もの"}

_STOP_FILE = Path(__file__).with_name("stopwords_ja.txt")
_DEFAULT_STOPWORDS: set[str] | None = None


def load_stopwords(path: str | Path | None = None) -> set[str]:
    global _DEFAULT_STOPWORDS
    if path is None and _DEFAULT_STOPWORDS is not None:
        return set(_DEFAULT_STOPWORDS)
    words: set[str] = set()
    p = Path(path) if path else _STOP_FILE
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                words.add(line)
    except OSError:
        pass
    if path is None:
        _DEFAULT_STOPWORDS = set(words)
    return words


def units(text: str) -> list[tuple[int, int, str]]:
    """(開始, 終了, 文字列) の列。"""
    text = nfkc(text)
    return [(m.start(), m.end(), m.group(0)) for m in UNIT_RE.finditer(text)]


def _is_junk(tok: str) -> bool:
    if len(tok) < 2:
        return True
    if tok.isdigit():
        return True
    if re.fullmatch(r"[0-9\-\.]+", tok):
        return True
    if re.match(r"^(第)?[0-9０-９]+", tok):        # 「第2」「2実施形態」など番号始まり
        return True
    return False


def candidates_from_text(text: str, stopwords: set[str] | None = None,
                         max_len: int = 20) -> set[str]:
    """1 テキストから語候補（重複なし）を取り出す。"""
    stop = stopwords if stopwords is not None else load_stopwords()
    text = nfkc(text)
    us = units(text)
    out: set[str] = set()
    for i, (s, e, tok) in enumerate(us):
        low = tok.lower() if tok.isascii() else tok
        if not _is_junk(low) and low not in stop:
            out.add(low)
        # 隣接単位の連結（間に文字が無いもの）
        if i + 1 < len(us) and us[i + 1][0] == e:
            joined = tok + us[i + 1][2]
            joined = joined.lower() if joined.isascii() else joined
            if 2 <= len(joined) <= max_len and joined not in stop:
                out.add(joined)
        # 漢字連続の部分文字列（4〜6 文字）。汎用語（製造方法 など）の部分文字列は出さない
        if re.fullmatch(rf"[{_KANJI}]+", tok) and 4 <= len(tok) <= 6 and tok not in stop:
            for a in range(len(tok)):
                for b in range(a + 2, len(tok) + 1):
                    sub = tok[a:b]
                    if len(sub) < len(tok) and sub not in stop:
                        out.add(sub)
    # 送り仮名つき
    for m in KANJI_OKURI_RE.finditer(text):
        if m.group(1) in stop:
            continue
        hira = m.group(2)
        for L in (1, 2):
            suffix = hira[:L]
            if len(suffix) < L or suffix in PARTICLES or suffix[-1] in _BAD_SUFFIX_END:
                continue
            word = m.group(1) + suffix
            if word not in stop and len(word) <= max_len:
                out.add(word)
    return {t for t in out if not _is_junk(t)}


def doc_text(doc: Document, fields=("TI", "AB")) -> str:
    return "\n".join(doc.text(f) for f in fields if doc.text(f))


def term_postings(docs, stopwords: set[str] | None = None,
                  fields=("TI", "AB")) -> dict[str, set[str]]:
    """語 → 出現文献 ID 集合。"""
    postings: dict[str, set[str]] = defaultdict(set)
    for doc in docs:
        for tok in candidates_from_text(doc_text(doc, fields), stopwords):
            postings[tok].add(doc.doc_id)
    return dict(postings)


def dedupe_by_postings(postings: dict[str, set[str]], max_short: int = 5) -> dict[str, set[str]]:
    """候補語の重複を減らす。

    1. 語幹（漢字）と送り仮名つき変種の出現集合が同一なら片方だけ残す
       （れ・き・け・り・え で終わる変種は語として自然なので変種を残す: 焼入れ／備え。それ以外は語幹を残す: 圧延しめ → 圧延）
    2. 部分文字列の語（≤ max_short 文字）が、それを含む長い語の内側でしか現れない
       （出現集合が長い語の集合に包含される）場合、短い語を落とす。例: 「自動」「動車」は「自動車」の中でしか出ない
       ただし「語幹＋送り仮名」の形の長い語は包含元と見なさない
    """
    hira_re = re.compile(rf"^(.+?[{_KANJI}])([{_HIRA}]{{1,2}})$")
    good_suffix = {"れ", "き", "け", "り", "え"}
    drop: set[str] = set()
    for term, ids in postings.items():
        m = hira_re.match(term)
        if not m or m.group(1) not in postings or postings[m.group(1)] != ids:
            continue
        if m.group(2) in good_suffix:
            drop.add(m.group(1))
        else:
            drop.add(term)
    postings = {t: ids for t, ids in postings.items() if t not in drop}
    by_gram: dict[str, list[str]] = defaultdict(list)
    for term in postings:
        if len(term) >= 2:
            for i in range(len(term) - 1):
                by_gram[term[i:i + 2]].append(term)
    keep: dict[str, set[str]] = {}
    for term, ids in postings.items():
        if len(term) <= max_short:
            covered = False
            for longer in by_gram.get(term[:2], ()):
                if len(longer) <= len(term) or term not in longer or not ids <= postings[longer]:
                    continue
                rest = longer.replace(term, "", 1)
                if re.fullmatch(rf"[{_HIRA}]+", rest):      # 語幹＋送り仮名は包含元にしない
                    continue
                covered = True
                break
            if covered:
                continue
        keep[term] = set(ids)
    return keep


def candidate_terms(docs, stopwords: set[str] | None = None, min_tf: int = 2,
                    fields=("TI", "AB")) -> list[tuple[str, int]]:
    """文献集合から (語, 文献頻度) を頻度順に返す。TF ≥ min_tf のみ。"""
    postings = dedupe_by_postings(term_postings(docs, stopwords, fields))
    items = [(t, len(ids)) for t, ids in postings.items() if len(ids) >= min_tf]
    items.sort(key=lambda x: (-x[1], x[0]))
    return items
