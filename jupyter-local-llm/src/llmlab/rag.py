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


def apply_llama_settings():
    """LlamaIndex の Settings を現在の接続情報で構成する（bookrag からも共有）。"""
    from llama_index.core import Settings as LISettings

    from .client import build_http_client

    s = get_settings()
    http_client = build_http_client(s)  # プロキシ on/off を反映
    LISettings.llm = _make_llm(s, http_client)
    LISettings.embed_model = _make_embed(s, http_client)


def _construct(cls, kwargs: dict, http_client):
    """http_client 非対応の古い版でも動くよう、TypeError 時は外して再生成する。"""
    try:
        return cls(**kwargs, http_client=http_client)
    except TypeError:
        return cls(**kwargs)


def _make_llm(s, http_client):
    try:
        from llama_index.llms.openai_like import OpenAILike
    except ImportError as e:
        raise ModuleNotFoundError(
            "RAG には llama-index-llms-openai-like が必要です。\n"
            "  pip install llama-index-llms-openai-like"
        ) from e
    return _construct(
        OpenAILike,
        dict(model=s.model, api_base=s.base_url, api_key=s.api_key,
             context_window=s.context_window, is_chat_model=True,
             is_function_calling_model=False),
        http_client,
    )


def _make_embed(s, http_client):
    # 推奨: OpenAILikeEmbedding（任意のモデル名 + カスタム api_base を許容）
    try:
        from llama_index.embeddings.openai_like import OpenAILikeEmbedding

        return _construct(
            OpenAILikeEmbedding,
            dict(model_name=s.embed_model, api_base=s.base_url, api_key=s.api_key),
            http_client,
        )
    except ImportError:
        pass
    # フォールバック: 標準 OpenAIEmbedding（api_base 指定可。モデル名は version により制限あり）
    try:
        from llama_index.embeddings.openai import OpenAIEmbedding
    except ImportError as e:
        raise ModuleNotFoundError(
            "RAG の埋め込みに llama-index-embeddings-openai-like（推奨）か "
            "llama-index-embeddings-openai のいずれかが必要です。\n"
            "  pip install llama-index-embeddings-openai-like"
        ) from e
    print(
        "[警告] llama-index-embeddings-openai-like が無いため OpenAIEmbedding で代替します。"
        "カスタムの埋め込みモデル名が拒否される場合は次を実行してください:\n"
        "  pip install llama-index-embeddings-openai-like"
    )
    return _construct(
        OpenAIEmbedding,
        dict(model=s.embed_model, api_base=s.base_url, api_key=s.api_key),
        http_client,
    )


# 後方互換のための別名。
_apply_settings = apply_llama_settings


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

    apply_llama_settings()
    storage_path = Path(storage_dir)

    if storage_path.exists() and not rebuild:
        ctx = StorageContext.from_defaults(persist_dir=str(storage_path))
        index = load_index_from_storage(ctx)
    else:
        docs_path = Path(docs_dir)
        if not docs_path.exists():
            raise FileNotFoundError(f"パスが見つかりません: {docs_path}")
        # ファイル単体 / フォルダ のどちらでも受け付ける
        if docs_path.is_dir():
            if not any(docs_path.iterdir()):
                raise FileNotFoundError(
                    f"フォルダ {docs_path} が空です。文書を置いてから再実行してください。"
                )
            reader = SimpleDirectoryReader(input_dir=str(docs_path))
        else:
            reader = SimpleDirectoryReader(input_files=[str(docs_path)])
        documents = reader.load_data()
        index = VectorStoreIndex.from_documents(documents)
        index.storage_context.persist(persist_dir=str(storage_path))

    return index.as_query_engine()
