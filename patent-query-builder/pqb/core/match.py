"""局所照合: DSL の Query を手元の文献集合（母集団 U）に機械的に当てる。企画書 §10.5。

- 語: 名称・要約・請求の範囲に対する部分一致（NFKC・小文字・空白除去）
- コード: 粒度を考慮した階層一致（pqb.knowledge.codes.matches）
- DB 側の照合との差（全文フィールド、シソーラス展開など）は根拠レポートで注記する
"""
from __future__ import annotations

from ..knowledge import codes as codelib
from ..util import norm_text
from .document import Document
from .dsl import Block, Query, Term


def _doc_texts(doc: Document) -> dict:
    ti = norm_text(doc.title)
    ab = norm_text(doc.abstract)
    cl = norm_text(doc.claims)
    return {"TI": ti, "AB": ab, "CL": cl, "TX": ti + ab + cl}


def term_matches(term: Term, texts: dict) -> bool:
    needle = norm_text(term.text)
    if not needle:
        return False
    return any(needle in texts.get(f, "") for f in term.fields)


def code_matches(scheme: str, code: str, doc: Document) -> bool:
    return any(codelib.matches(scheme, code, dc) for dc in doc.codes.get(scheme, []))


def block_matches(block: Block, doc: Document, texts: dict | None = None) -> bool:
    texts = texts or _doc_texts(doc)
    terms = block.adopted_terms()
    codes = block.adopted_codes()
    t_hit = any(term_matches(t, texts) for t in terms) if terms else None
    c_hit = any(code_matches(c.scheme, c.code, doc) for c in codes) if codes else None
    if t_hit is None and c_hit is None:
        return False
    if t_hit is None:
        return bool(c_hit)
    if c_hit is None:
        return bool(t_hit)
    if block.term_code_join == "AND":
        return t_hit and c_hit
    return t_hit or c_hit


def query_matches(query: Query, doc: Document) -> bool:
    texts = _doc_texts(doc)
    blocks = query.active_blocks()
    if not blocks:
        return False
    if not all(block_matches(b, doc, texts) for b in blocks):
        return False
    for ex in query.active_exclusions():
        if block_matches(ex, doc, texts):
            return False
    return True


def match_ids(query: Query, docs) -> set[str]:
    return {d.doc_id for d in docs if query_matches(query, d)}


def match_docs(query: Query, docs) -> list[Document]:
    return [d for d in docs if query_matches(query, d)]
