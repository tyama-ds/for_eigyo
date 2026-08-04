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

# インデックス（JSON）から BM25 を作り直すのは安いが、クエリごとには避ける
_bm25_cache: dict[int, BM25] = {}


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

    def query(self, index: dict, question: str, mode: str, ctx: EngineContext) -> dict:
        ctx.progress(0.2, "BM25 検索")
        cache_key = id(index)
        bm25 = _bm25_cache.get(cache_key)
        if bm25 is None:
            bm25 = BM25([tokenize(c["text"]) for c in index["chunks"]])
            _bm25_cache.clear()
            _bm25_cache[cache_key] = bm25
        hits = bm25.top_k(question, k=TOP_K)
        passages = [index["chunks"][i] for i, _ in hits]
        ctx.progress(0.6, "回答生成")
        think = ""
        if ctx.chat_ok:
            answer, think = generate_answer(ctx, question, passages, task_tag="bm25_answer")
            mode_used = "bm25"
        else:
            answer = extractive_answer(question, passages)
            mode_used = "bm25(抽出)"
        citations = [chunk_citation(index["chunks"][i], s) for i, s in hits]
        return {"answer": answer, "think": think, "mode": mode_used,
                "citations": citations, "stats": {"retrieved": len(passages)}}
