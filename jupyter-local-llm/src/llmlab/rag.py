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


# 設定(Settings は frozen dataclass=ハッシュ可能)ごとに llm/embed を再利用する。
# 呼び出しのたびに httpx.Client を作り捨てるとコネクションが漏れるため。
_models_cache: dict = {}


def apply_llama_settings():
    """LlamaIndex の Settings を現在の接続情報で構成する（bookrag からも共有）。"""
    from llama_index.core import Settings as LISettings

    from .client import build_http_client

    s = get_settings()
    cached = _models_cache.get(s)
    if cached is None:
        http_client = build_http_client(s)  # プロキシ on/off を反映
        cached = (_make_llm(s, http_client), _make_embed(s, http_client))
        _models_cache.clear()  # 旧設定分は破棄
        _models_cache[s] = cached
    LISettings.llm, LISettings.embed_model = cached


def _construct(cls, kwargs: dict, http_client):
    """バージョン差異に耐えるコンストラクタ。

    http_client → timeout/max_retries の順に、未対応の引数を落として再試行する
    （古い llama-index では TypeError/ValidationError になるため）。
    """
    attempts = [
        {**kwargs, "http_client": http_client},
        kwargs,
        {k: v for k, v in kwargs.items() if k not in ("timeout", "max_retries")},
    ]
    last = None
    for kw in attempts:
        try:
            return cls(**kw)
        except (TypeError, ValueError) as e:  # pydantic ValidationError は ValueError 派生
            last = e
    raise last


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
             is_function_calling_model=False,
             timeout=s.request_timeout, max_retries=2),  # RAG 経路にも timeout を効かせる
        http_client,
    )


def _make_embed(s, http_client):
    # サーバに /v1/embeddings が無い等の場合のローカル埋め込み
    if getattr(s, "embed_provider", "openai") == "local":
        try:
            from llama_index.embeddings.huggingface import HuggingFaceEmbedding
        except ImportError as e:
            raise ModuleNotFoundError(
                "ローカル埋め込みには llama-index-embeddings-huggingface が必要です。\n"
                "  pip install llama-index-embeddings-huggingface"
            ) from e
        return HuggingFaceEmbedding(model_name=s.embed_local_model)

    # 埋め込み用の接続先（チャットと別エンドポイントなら embed_base_url を使う）
    base = s.embed_base_url or s.base_url
    key = s.embed_api_key or s.api_key

    # 推奨: OpenAILikeEmbedding（任意のモデル名 + カスタム api_base を許容）
    try:
        from llama_index.embeddings.openai_like import OpenAILikeEmbedding

        return _construct(
            OpenAILikeEmbedding,
            dict(model_name=s.embed_model, api_base=base, api_key=key,
                 timeout=s.request_timeout, max_retries=2),
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
        dict(model=s.embed_model, api_base=base, api_key=key,
             timeout=s.request_timeout, max_retries=2),
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

    # ディレクトリ存在ではなく docstore.json の有無で「既存インデックス」を判定する。
    # ./storage は PagedRAG/BookRAG 等の既定保存先の親でもあり、存在だけでは判定できない。
    has_index = (storage_path / "docstore.json").exists()
    if has_index and not rebuild:
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

    return _CleanQueryEngine(index.as_query_engine())


class _CleanResponse:
    """LlamaIndex の Response をラップし、思考過程を除去したテキストを返す。"""

    def __init__(self, resp):
        from .client import strip_think

        self._resp = resp
        self.source_nodes = getattr(resp, "source_nodes", [])
        self.response = strip_think(str(resp))

    def __str__(self) -> str:
        return self.response


class _CleanQueryEngine:
    """query() の応答から思考過程（<think>…</think>）を除去する薄いラッパー。"""

    def __init__(self, engine):
        self._engine = engine

    def query(self, question):
        return _CleanResponse(self._engine.query(question))

    def __getattr__(self, name):  # その他の属性は元エンジンへ委譲
        return getattr(self._engine, name)
