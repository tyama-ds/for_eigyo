"""社内ドキュメントを参照する RAG（LlamaIndex + OpenAI 互換エンドポイント）。

使い方::

    import llmlab
    llmlab.configure(...)                       # 接続情報を入力
    engine = llmlab.build_rag("./docs")         # docs/ を取り込みインデックス化
    print(engine.query("この資料の要点は？"))

一度作ったインデックスは ./storage に保存され、次回は再利用される。
"""

from __future__ import annotations

from pathlib import Path

from .config import get_settings

DEFAULT_DOCS = "./docs"
DEFAULT_STORAGE = "./storage"


def _apply_settings():
    """LlamaIndex の Settings を現在の接続情報で構成する。"""
    from llama_index.core import Settings as LISettings
    from llama_index.embeddings.openai_like import OpenAILikeEmbedding
    from llama_index.llms.openai_like import OpenAILike

    from .client import build_http_client

    s = get_settings()
    http_client = build_http_client(s)  # プロキシ on/off を反映
    LISettings.llm = OpenAILike(
        model=s.model,
        api_base=s.base_url,
        api_key=s.api_key,
        context_window=s.context_window,
        is_chat_model=True,
        is_function_calling_model=False,
        http_client=http_client,
    )
    LISettings.embed_model = OpenAILikeEmbedding(
        model_name=s.embed_model,
        api_base=s.base_url,
        api_key=s.api_key,
        http_client=http_client,
    )


def build_rag(
    docs_dir: str | Path = DEFAULT_DOCS,
    *,
    storage_dir: str | Path = DEFAULT_STORAGE,
    rebuild: bool = False,
):
    """ドキュメントを取り込み、クエリエンジンを返す。

    既存インデックスがあれば再利用し、`rebuild=True` で作り直す。
    """
    from llama_index.core import (
        StorageContext,
        VectorStoreIndex,
        load_index_from_storage,
    )
    from llama_index.core import SimpleDirectoryReader

    _apply_settings()
    storage_path = Path(storage_dir)

    if storage_path.exists() and not rebuild:
        ctx = StorageContext.from_defaults(persist_dir=str(storage_path))
        index = load_index_from_storage(ctx)
    else:
        docs_path = Path(docs_dir)
        if not docs_path.exists() or not any(docs_path.iterdir()):
            raise FileNotFoundError(
                f"取り込む文書が {docs_path} にありません。ファイルを置いてから再実行してください。"
            )
        documents = SimpleDirectoryReader(str(docs_path)).load_data()
        index = VectorStoreIndex.from_documents(documents)
        index.storage_context.persist(persist_dir=str(storage_path))

    return index.as_query_engine()
