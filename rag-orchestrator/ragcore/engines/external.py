"""外部 RAG 実装のアダプタ（実験的）。

DRO が GPT Researcher / Open Deep Research を包むのと同じ発想で、
公開されている RAG 実装をアダプタ経由で呼び出す。ライブラリが pip で
導入されている場合のみ有効化され、未導入なら UI に導入コマンドを表示する。

- nano-graphrag (https://github.com/gusye1234/nano-graphrag): 約1,000行の GraphRAG 実装
- LightRAG (https://github.com/HKUDS/LightRAG): グラフ+ベクトル二層インデックスの軽量 GraphRAG

どちらも LLM / 埋め込みはこのアプリの接続設定（OpenAI 互換エンドポイント）を注入する。
ライブラリ側の API はバージョンで変わり得るため experimental 扱いとし、
失敗はエラーとして UI にそのまま表示する（silent fallback はしない）。
"""
from __future__ import annotations

import asyncio
import importlib.util
import time
from pathlib import Path

from .base import Engine, EngineContext

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "external"


def _module_available(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def _run_async(coro):
    """バックグラウンドスレッドから安全にコルーチンを実行する。"""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _make_openai_funcs(cfg: dict):
    """接続設定から (async LLM関数, async 埋め込み関数, 埋め込み次元) を作る。

    openai / numpy は nano-graphrag・LightRAG 両方の依存なので、
    外部エンジンが導入済みなら利用できる。
    """
    import numpy as np
    from openai import AsyncOpenAI

    def _client(base: str, key: str) -> AsyncOpenAI:
        return AsyncOpenAI(base_url=base, api_key=key or "sk-no-key",
                           timeout=float(cfg.get("request_timeout") or 180.0))

    async def llm_func(prompt, system_prompt=None, history_messages=None, **kwargs):
        kwargs.pop("hashing_kv", None)            # ライブラリ内部のキャッシュ用引数
        kwargs.pop("keyword_extraction", None)
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.extend(history_messages or [])
        messages.append({"role": "user", "content": prompt})
        allowed = {k: v for k, v in kwargs.items()
                   if k in ("max_tokens", "temperature", "response_format")}
        client = _client(cfg["base_url"].rstrip("/"), cfg.get("api_key", ""))
        resp = await client.chat.completions.create(
            model=cfg["model"], messages=messages, **allowed)
        return resp.choices[0].message.content or ""

    embed_base = (cfg.get("embed_base_url") or cfg["base_url"]).rstrip("/")
    embed_key = cfg.get("embed_api_key") or cfg.get("api_key", "")
    embed_model = cfg.get("embed_model") or cfg["model"]

    async def embed_func(texts: list[str]):
        client = _client(embed_base, embed_key)
        resp = await client.embeddings.create(model=embed_model, input=texts)
        rows = sorted(resp.data, key=lambda r: r.index)
        return np.array([r.embedding for r in rows])

    dim = len(_run_async(embed_func(["dimension probe"]))[0])
    return llm_func, embed_func, dim


class _ExternalBase(Engine):
    kind = "external"
    experimental = True
    requires_chat = True
    requires_embed = True
    module = ""                 # import 判定に使うモジュール名
    install_hint = ""

    def availability(self, cfg: dict) -> tuple[bool, str]:
        if not _module_available(self.module):
            return False, f"未導入です（{self.install_hint} で導入）"
        return super().availability(cfg)

    def _working_dir(self) -> Path:
        path = DATA_DIR / self.id
        path.mkdir(parents=True, exist_ok=True)
        return path

    @staticmethod
    def _combine_corpus(corpus: list[dict]) -> list[str]:
        return [f"# {doc.get('title') or doc['id']}\n\n{doc['text']}" for doc in corpus]


class NanoGraphRAGEngine(_ExternalBase):
    id = "nano-graphrag"
    name = "nano-graphrag（外部）"
    description = ("gusye1234/nano-graphrag: 約1,000行のシンプルな GraphRAG 実装。"
                   "global / local / naive 検索")
    module = "nano_graphrag"
    install_hint = "pip install nano-graphrag"

    def _build(self, ctx: EngineContext):
        from nano_graphrag import GraphRAG
        from nano_graphrag._utils import wrap_embedding_func_with_attrs

        llm_func, embed_func, dim = _make_openai_funcs(ctx.cfg)
        embedding = wrap_embedding_func_with_attrs(
            embedding_dim=dim, max_token_size=8192)(embed_func)
        return GraphRAG(
            working_dir=str(self._working_dir()),
            best_model_func=llm_func,
            cheap_model_func=llm_func,
            embedding_func=embedding,
        )

    def ingest(self, corpus: list[dict], ctx: EngineContext) -> dict:
        ctx.progress(0.05, "nano-graphrag 初期化")
        graph = self._build(ctx)
        ctx.progress(0.15, "insert（グラフ構築）実行中…")
        graph.insert(self._combine_corpus(corpus))
        ctx.progress(1.0, "完了")
        return {"engine": self.id, "built_at": time.time(),
                "working_dir": str(self._working_dir()),
                "stats": {"docs": len(corpus)}}

    def query(self, index: dict, question: str, mode: str, ctx: EngineContext) -> dict:
        from nano_graphrag import QueryParam

        graph = self._build(ctx)
        nano_mode = {"global": "global", "local": "local"}.get(mode, "global")
        ctx.progress(0.3, f"query（{nano_mode}）実行中…")
        answer = graph.query(question, param=QueryParam(mode=nano_mode))
        return {"answer": str(answer), "mode": nano_mode, "citations": [],
                "stats": {"note": "出典はライブラリの回答文中に含まれる形式"}}


class LightRAGEngine(_ExternalBase):
    id = "lightrag"
    name = "LightRAG（外部）"
    description = ("HKUDS/LightRAG: グラフ+ベクトルの二層インデックスと増分更新が特徴の"
                   "軽量 GraphRAG。naive / local / global / hybrid 検索")
    module = "lightrag"
    install_hint = "pip install lightrag-hku"

    def _build(self, ctx: EngineContext):
        from lightrag import LightRAG
        from lightrag.utils import EmbeddingFunc

        llm_func, embed_func, dim = _make_openai_funcs(ctx.cfg)
        rag = LightRAG(
            working_dir=str(self._working_dir()),
            llm_model_func=llm_func,
            embedding_func=EmbeddingFunc(embedding_dim=dim, max_token_size=8192,
                                         func=embed_func),
        )

        async def _init():
            if hasattr(rag, "initialize_storages"):
                await rag.initialize_storages()
            try:
                from lightrag.kg.shared_storage import initialize_pipeline_status
                await initialize_pipeline_status()
            except ImportError:
                pass

        _run_async(_init())
        return rag

    def ingest(self, corpus: list[dict], ctx: EngineContext) -> dict:
        ctx.progress(0.05, "LightRAG 初期化")
        rag = self._build(ctx)
        ctx.progress(0.15, "insert（グラフ構築）実行中…")
        texts = self._combine_corpus(corpus)
        if hasattr(rag, "ainsert"):
            _run_async(rag.ainsert(texts))
        else:
            rag.insert(texts)
        ctx.progress(1.0, "完了")
        return {"engine": self.id, "built_at": time.time(),
                "working_dir": str(self._working_dir()),
                "stats": {"docs": len(corpus)}}

    def query(self, index: dict, question: str, mode: str, ctx: EngineContext) -> dict:
        from lightrag import QueryParam

        rag = self._build(ctx)
        lr_mode = {"global": "global", "local": "local"}.get(mode, "hybrid")
        ctx.progress(0.3, f"query（{lr_mode}）実行中…")
        param = QueryParam(mode=lr_mode)
        if hasattr(rag, "aquery"):
            answer = _run_async(rag.aquery(question, param=param))
        else:
            answer = rag.query(question, param=param)
        return {"answer": str(answer), "mode": lr_mode, "citations": [],
                "stats": {"note": "出典はライブラリの回答文中に含まれる形式"}}
