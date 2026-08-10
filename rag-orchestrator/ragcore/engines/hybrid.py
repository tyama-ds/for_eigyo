"""ハイブリッド検索 RAG（ベクトル + BM25 を RRF で融合）。

意味検索と字句検索の弱点を互いに補う、実務で定番の構成。
"""
from __future__ import annotations

import time

from ..textutil import BM25, rrf_fuse, tokenize, top_k_cosine
from .base import (Engine, EngineContext, build_chunks, chunk_citation,
                   extractive_answer, generate_answer)

TOP_K = 6
CANDIDATES = 12          # 各リトリーバーから RRF へ渡す候補数


class HybridEngine(Engine):
    id = "hybrid"
    name = "Hybrid RAG（組み込み）"
    kind = "builtin"
    description = ("ベクトル検索と BM25 を Reciprocal Rank Fusion で融合 → top-k → 生成。"
                   "意味一致と字句一致の両取り")
    requires_chat = False
    requires_embed = True

    def ingest(self, corpus: list[dict], ctx: EngineContext) -> dict:
        chunks = build_chunks(corpus)
        if not chunks:
            raise ValueError("コーパスが空です")
        ctx.progress(0.2, f"埋め込み計算（{len(chunks)}チャンク）")
        vecs = ctx.llm.embed([c["text"][:2000] for c in chunks])
        ctx.progress(1.0, "完了")
        return {"engine": self.id, "built_at": time.time(),
                "chunks": chunks, "chunk_vecs": vecs,
                "stats": {"chunks": len(chunks)}}

    def query(self, index: dict, question: str, mode: str, ctx: EngineContext) -> dict:
        chunks = index["chunks"]
        ctx.progress(0.15, "ベクトル検索")
        qvec = ctx.llm.embed([question])[0]
        vec_rank = [i for i, s in top_k_cosine(qvec, index["chunk_vecs"], k=CANDIDATES)
                    if s > 0]
        ctx.progress(0.35, "BM25 検索")
        bm25 = BM25([tokenize(c["text"]) for c in chunks])
        lex_rank = [i for i, _ in bm25.top_k(question, k=CANDIDATES)]
        fused = rrf_fuse([vec_rank, lex_rank])[:TOP_K]
        passages = [chunks[i] for i in fused]
        ctx.progress(0.6, "回答生成")
        think = ""
        if ctx.chat_ok:
            answer, think = generate_answer(ctx, question, passages, task_tag="hybrid_answer")
            mode_used = "hybrid(RRF)"
        else:
            answer = extractive_answer(question, passages)
            mode_used = "hybrid(抽出)"
        citations = [chunk_citation(chunks[i]) for i in fused]
        return {"answer": answer, "think": think, "mode": mode_used, "citations": citations,
                "stats": {"vector_candidates": len(vec_rank),
                          "bm25_candidates": len(lex_rank), "retrieved": len(passages)}}
