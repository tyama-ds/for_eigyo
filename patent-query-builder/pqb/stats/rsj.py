"""RSJ 重み（Robertson–Sparck Jones）と offer weight による候補選択。企画書 §10.3・§10.4。

  w(t)  = log( ((r + 0.5) (N - n - R + r + 0.5)) / ((n - r + 0.5) (R - r + 0.5)) )
  OW(t) = r * w(t)

- r = t を含む適合文献数、R = 適合文献数、n = t を含む文献数、N = 採点済み文献数
- N と n は母集団 U の内側（採点済み集合）で数える。DB 全体は不可視なので
  w(t) は「U の中での識別力」。根拠レポートにその旨を注記する。
- 上位 K 件＋無作為標本の混合は適合側に偏るため、無作為標本のみの w も併記し、
  符号が食い違う候補は「要確認」にする。
- 分類コードは全粒度で集計し、OW 最大かつ n ≤ N×ratio の粒度を採用する（同点は細かい方）。
"""
from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import asdict, dataclass, field

from ..core.document import Document
from ..knowledge import codes as codelib
from .tokenize import candidates_from_text, doc_text


def rsj_weight(r: float, n: float, R: float, N: float) -> float:
    num = (r + 0.5) * (N - n - R + r + 0.5)
    den = (n - r + 0.5) * (R - r + 0.5)
    if num <= 0 or den <= 0:
        return 0.0
    return math.log(num / den)


@dataclass
class FeatureStat:
    feature: str                 # 語、またはコード
    kind: str                    # term | code
    scheme: str = ""             # code のとき FI/FT/IPC
    level: str = ""              # code のとき粒度
    r: int = 0
    n: int = 0
    R: int = 0
    N: int = 0
    w: float = 0.0
    ow: float = 0.0
    rw: float = 0.0              # 重み付き r（Σ 適合度/3）
    w_weighted: float = 0.0
    r_s: int = 0                 # 無作為標本のみ
    n_s: int = 0
    R_s: int = 0
    N_s: int = 0
    w_sample: float | None = None
    needs_review: bool = False
    in_query: bool = False
    docs: list = field(default_factory=list)   # 由来文献（適合側、最大 5 件）

    def to_dict(self) -> dict:
        d = asdict(self)
        d["w"] = round(self.w, 4)
        d["ow"] = round(self.ow, 4)
        d["w_weighted"] = round(self.w_weighted, 4)
        d["w_sample"] = None if self.w_sample is None else round(self.w_sample, 4)
        return d


def doc_features(doc: Document, term_vocab: set[str] | None, stopwords: set[str] | None,
                 fields=("TI", "AB")) -> set[tuple]:
    """文献の二値素性。("term", 語) と ("code", 体系, 粒度, コード)。"""
    feats: set[tuple] = set()
    toks = candidates_from_text(doc_text(doc, fields), stopwords)
    if term_vocab is not None:
        toks = {t for t in toks if t in term_vocab}
    for t in toks:
        feats.add(("term", t))
    for scheme, code in doc.all_codes():
        for level, value in codelib.levels(scheme, code):
            if level != "raw":
                feats.add(("code", scheme, level, value))
    return feats


def compute_stats(judged: list[tuple[Document, bool]], term_vocab: set[str] | None = None,
                  stopwords: set[str] | None = None, sample_ids: set[str] | None = None,
                  grades: dict[str, int] | None = None, fields=("TI", "AB")) -> list[FeatureStat]:
    """採点済み文献 [(doc, relevant)] から全素性の RSJ 統計を出す。"""
    N = len(judged)
    R = sum(1 for _, rel in judged if rel)
    sample_ids = sample_ids or set()
    grades = grades or {}
    N_s = sum(1 for d, _ in judged if d.doc_id in sample_ids)
    R_s = sum(1 for d, rel in judged if rel and d.doc_id in sample_ids)
    n_map: dict[tuple, int] = defaultdict(int)
    r_map: dict[tuple, int] = defaultdict(int)
    rw_map: dict[tuple, float] = defaultdict(float)
    ns_map: dict[tuple, int] = defaultdict(int)
    rs_map: dict[tuple, int] = defaultdict(int)
    docs_map: dict[tuple, list] = defaultdict(list)
    for doc, rel in judged:
        feats = doc_features(doc, term_vocab, stopwords, fields)
        in_sample = doc.doc_id in sample_ids
        grade = grades.get(doc.doc_id)
        for f in feats:
            n_map[f] += 1
            if in_sample:
                ns_map[f] += 1
            if rel:
                r_map[f] += 1
                rw_map[f] += (grade / 3.0) if grade is not None else 1.0
                if in_sample:
                    rs_map[f] += 1
                if len(docs_map[f]) < 5:
                    docs_map[f].append(doc.doc_id)
    out: list[FeatureStat] = []
    for f, n in n_map.items():
        r = r_map.get(f, 0)
        w = rsj_weight(r, n, R, N)
        st = FeatureStat(feature=f[1] if f[0] == "term" else f[3], kind=f[0],
                         scheme=f[1] if f[0] == "code" else "", level=f[2] if f[0] == "code" else "",
                         r=r, n=n, R=R, N=N, w=w, ow=r * w, rw=rw_map.get(f, 0.0),
                         w_weighted=rsj_weight(rw_map.get(f, 0.0), n, R, N),
                         r_s=rs_map.get(f, 0), n_s=ns_map.get(f, 0), R_s=R_s, N_s=N_s,
                         docs=docs_map.get(f, []))
        if N_s >= 5 and st.n_s > 0:
            st.w_sample = rsj_weight(st.r_s, st.n_s, R_s, N_s)
            if (st.w_sample > 0) != (w > 0) and abs(w) > 0.3:
                st.needs_review = True
        out.append(st)
    return out


def aggregate_code_levels(stats: list[FeatureStat], ratio: float = 0.5) -> list[FeatureStat]:
    """同じコードの複数粒度から識別力が最大の粒度を選ぶ。"""
    codes = [s for s in stats if s.kind == "code"]
    if not codes:
        return []
    by_key = {(s.scheme, s.feature): s for s in codes}
    finest: set[tuple[str, str]] = set()
    # 「子を持たないコード」を最も細かいコードとして扱い、その系列（祖先）から選ぶ
    parents = {(s.scheme, codelib.coarser(s.scheme, s.feature)) for s in codes}
    for s in codes:
        if (s.scheme, s.feature) not in parents:
            finest.add((s.scheme, s.feature))
    chosen: dict[tuple[str, str], FeatureStat] = {}
    for scheme, code in finest:
        chain = [(scheme, v) for _, v in codelib.levels(scheme, code)]
        cands = [by_key[k] for k in chain if k in by_key]
        if not cands:
            continue
        N = cands[0].N
        ok = [c for c in cands if c.n <= max(1, ratio * N)] or cands
        best = max(enumerate(ok), key=lambda ic: (ic[1].ow, ic[0]))[1]   # 同点は細かい方
        chosen[(best.scheme, best.feature)] = best
    return sorted(chosen.values(), key=lambda s: -s.ow)


def rank_candidates(judged: list[tuple[Document, bool]], *, stopwords: set[str] | None = None,
                    sample_ids: set[str] | None = None, grades: dict[str, int] | None = None,
                    top_terms: int = 30, top_codes: int = 20, min_tf: int = 2,
                    code_level_ratio: float = 0.5, query_terms: set[str] | None = None,
                    query_codes: set[tuple[str, str]] | None = None) -> dict:
    """G2／G4 に提示する候補表。"""
    from .tokenize import candidate_terms
    docs = [d for d, _ in judged]
    vocab = {t for t, _ in candidate_terms(docs, stopwords, min_tf=min_tf)}
    stats = compute_stats(judged, vocab, stopwords, sample_ids, grades)
    query_terms = {t.lower() if t.isascii() else t for t in (query_terms or set())}
    query_codes = query_codes or set()
    for s in stats:
        if s.kind == "term":
            s.in_query = s.feature in query_terms
        else:
            s.in_query = (s.scheme, s.feature) in query_codes
    terms = [s for s in stats if s.kind == "term"]
    codes = aggregate_code_levels(stats, code_level_ratio)
    pos_terms = sorted([s for s in terms if s.w > 0 and s.r > 0], key=lambda s: (-s.ow, -s.w))[:top_terms]
    neg_terms = sorted([s for s in terms if s.w < 0], key=lambda s: (-s.n, s.w))[:top_terms]
    pos_codes = [s for s in codes if s.w > 0 and s.r > 0][:top_codes]
    neg_codes = sorted([s for s in codes if s.w < 0], key=lambda s: (-s.n, s.w))[:top_codes]
    N = len(judged)
    R = sum(1 for _, rel in judged if rel)
    return {
        "N": N, "R": R, "N_sample": len([d for d in docs if d.doc_id in (sample_ids or set())]),
        "terms": [s.to_dict() for s in pos_terms],
        "codes": [s.to_dict() for s in pos_codes],
        "negative_terms": [s.to_dict() for s in neg_terms],
        "negative_codes": [s.to_dict() for s in neg_codes],
        "note": "統計は母集団 U の内側（採点済み集合 N 件）で計算した識別力であり、DB 全体の値ではない。",
    }
