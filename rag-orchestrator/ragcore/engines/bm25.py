"""BM25（字句一致）RAG。

埋め込み API 不要・LLM 無しでも抜粋モードで動く、最も軽いエンジン。
DRO の Mock エンジンと同様「LLM 未設定でも動作確認できる」役割も担う。
"""
from __future__ import annotations

import time

from ..textutil import BM25, tokenize
from .base import (Engine, EngineContext, build_chunks, chunk_citation,
                   extractive_answer, generate_answer)

TOP_K = 6

# インデックス（JSON）から BM25 を作り直すのは安いが、クエリごとには避ける。
# index 自体も持っておく（id() は解放後に別のオブジェクトへ使い回されるため、is で同じものか確かめる）
_bm25_cache: dict[int, tuple[dict, BM25]] = {}


def bm25_for(index: dict) -> BM25:
    """index["chunks"] の BM25（直前に使った index ならキャッシュを使う）。"""
    cached = _bm25_cache.get(id(index))
    if cached is not None and cached[0] is index:
        return cached[1]
    bm25 = BM25([tokenize(c["text"]) for c in index["chunks"]])
    _bm25_cache.clear()
    _bm25_cache[id(index)] = (index, bm25)
    return bm25


class BM25Engine(Engine):
    id = "bm25"
    name = "BM25 RAG（組み込み）"
    kind = "builtin"
    description = ("字句一致（BM25）→ top-k → 生成。埋め込み不要で最軽量。"
                   "LLM 未設定でも抜粋モードで動作")
    requires_chat = False
    requires_embed = False

    def ingest(self, corpus: list[dict], ctx: EngineContext) -> dict:
        chunks = build_chunks(corpus)
        if not chunks:
            raise ValueError("コーパスが空です")
        ctx.progress(1.0, "完了")
        return {"engine": self.id, "built_at": time.time(), "chunks": chunks,
                "stats": {"chunks": len(chunks)}}

    def retrieve(self, index: dict, question: str, ctx: EngineContext, k: int = TOP_K,
                 *, detail: dict | None = None) -> list[tuple[str, float]]:
        """字句一致（BM25）の上位 k チャンク。LLM も埋め込みも使わない。"""
        chunks = index["chunks"]
        return [(chunks[i]["id"], s) for i, s in bm25_for(index).top_k(question, k=k)]

    def query(self, index: dict, question: str, mode: str, ctx: EngineContext) -> dict:
        ctx.progress(0.2, "BM25 検索")
        hits = self.retrieve(index, question, ctx, TOP_K)
        by_id = {c["id"]: c for c in index["chunks"]}
        passages = [by_id[cid] for cid, _ in hits]
        ctx.progress(0.6, "回答生成")
        think = ""
        if ctx.chat_ok:
            answer, think = generate_answer(ctx, question, passages, task_tag="bm25_answer")
            mode_used = "bm25"
        else:
            answer = extractive_answer(question, passages)
            mode_used = "bm25(抽出)"
        citations = [chunk_citation(by_id[cid], s) for cid, s in hits]
        return {"answer": answer, "think": think, "mode": mode_used,
                "citations": citations, "stats": {"retrieved": len(passages)}}
