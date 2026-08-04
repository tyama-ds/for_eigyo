"""ベクトル検索 RAG（Naive RAG のベースライン）。

チャンク分割 → 埋め込み → コサイン類似度 top-k → 回答生成。
GraphRAG との比較の基準になる、最も一般的な構成。
"""
from __future__ import annotations

import time

from ..textutil import top_k_cosine
from .base import (Engine, EngineContext, build_chunks, chunk_citation,
                   extractive_answer, generate_answer)

TOP_K = 6


class VectorEngine(Engine):
    id = "vector"
    name = "Vector RAG（組み込み）"
    kind = "builtin"
    description = ("チャンク→埋め込み→コサイン類似度 top-k→生成。"
                   "いわゆる Naive RAG。比較のベースライン")
    requires_chat = False         # LLM 無しでも抜粋モードで動く
    requires_embed = True

    def ingest(self, corpus: list[dict], ctx: EngineContext) -> dict:
        chunks = build_chunks(corpus)
        if not chunks:
            raise ValueError("コーパスが空です")
        ctx.progress(0.2, f"埋め込み計算（{len(chunks)}チャンク）")
        vecs = ctx.llm.embed([c["text"][:2000] for c in chunks])
        ctx.progress(1.0, "完了")
        return {
            "engine": self.id, "built_at": time.time(),
            "chunks": chunks, "chunk_vecs": vecs,
            "stats": {"chunks": len(chunks), "dim": len(vecs[0]) if vecs else 0},
        }

    def query(self, index: dict, question: str, mode: str, ctx: EngineContext) -> dict:
        ctx.progress(0.2, "類似チャンク検索")
        qvec = ctx.llm.embed([question])[0]
        hits = [(i, s) for i, s in top_k_cosine(qvec, index["chunk_vecs"], k=TOP_K) if s > 0]
        passages = [index["chunks"][i] for i, _ in hits]
        ctx.progress(0.6, "回答生成")
        if ctx.chat_ok:
            answer = generate_answer(ctx, question, passages, task_tag="vector_answer")
            mode_used = "vector"
        else:
            answer = extractive_answer(question, passages)
            mode_used = "vector(抽出)"
        citations = [chunk_citation(index["chunks"][i], s) for i, s in hits]
        return {"answer": answer, "mode": mode_used, "citations": citations,
                "stats": {"retrieved": len(passages)}}
