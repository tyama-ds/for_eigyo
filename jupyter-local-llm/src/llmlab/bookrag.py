"""BookRAG — 本・長尺PDF 向けの RAG（ページ出典つき）。

GitHub の BookRAG プロジェクト（FastAPI + pgvector + Ollama のサーバ型）と同等の
「本を取り込み、ページ出典つきで問い合わせる」体験を、llmlab の構成
（JupyterLab 完結 / LlamaIndex / OpenAI 互換エンドポイント / プロキシ対応）で実現する。

使い方::

    import llmlab
    llmlab.configure(base_url=..., api_key=..., model=..., embed_model=...)

    book = llmlab.BookRAG()
    book.add_book("./docs/営業マニュアル.pdf", title="営業マニュアル")
    book.add_book("./docs/製品仕様.pdf")          # title 省略時はファイル名

    print(book.ask("返品の手順は？"))             # 回答＋ページ出典を表示
    print(book.ask("料金体系は？", title="営業マニュアル"))  # 特定の本だけに絞る

特徴:
- 複数の本を1インデックスで管理（本ごとに title / source / page をメタデータ付与）
- 本向けのチャンク分割（既定 1024 トークン / オーバーラップ 128）
- 回答に **書名・ページ番号** の出典を添える
- 取り込み済みインデックスは storage_dir に永続化され再利用される
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_STORAGE = "./storage/books"
DEFAULT_CHUNK_SIZE = 1024
DEFAULT_CHUNK_OVERLAP = 128
DEFAULT_TOP_K = 5


@dataclass
class Source:
    """回答の根拠となった箇所。"""

    title: str
    page: str | None
    score: float | None
    snippet: str

    def __str__(self) -> str:
        page = f" p.{self.page}" if self.page else ""
        score = f" ({self.score:.2f})" if self.score is not None else ""
        return f"- {self.title}{page}{score}: {self.snippet}"


@dataclass
class Answer:
    """回答本文と出典のまとめ。print() で読みやすく整形表示する。"""

    text: str
    sources: list[Source] = field(default_factory=list)

    def __str__(self) -> str:
        if not self.sources:
            return self.text
        lines = "\n".join(str(s) for s in self.sources)
        return f"{self.text}\n\n── 出典 ──\n{lines}"


class BookRAG:
    """本・長尺PDF を取り込み、ページ出典つきで問い合わせる RAG。"""

    def __init__(
        self,
        storage_dir: str | Path = DEFAULT_STORAGE,
        *,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        chunk_overlap: int = DEFAULT_CHUNK_OVERLAP,
        top_k: int = DEFAULT_TOP_K,
    ):
        self.storage_dir = Path(storage_dir)
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
        self.top_k = top_k
        self._index = None  # 遅延ロード
        self._catalog_path = self.storage_dir / "books.json"

    # ---- 取り込み ----------------------------------------------------------

    def add_book(self, path: str | Path, *, title: str | None = None) -> str:
        """1冊の本（PDF/txt/md/docx など）を取り込み、インデックスへ追加する。"""
        from llama_index.core import SimpleDirectoryReader
        from llama_index.core.node_parser import SentenceSplitter

        from .rag import apply_llama_settings

        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"ファイルがありません: {path}")

        apply_llama_settings()
        book_title = title or path.stem

        # ページ単位のメタデータ（PDF なら page_label）を保持して読み込む。
        documents = SimpleDirectoryReader(input_files=[str(path)]).load_data()
        for d in documents:
            d.metadata["title"] = book_title
            d.metadata["source"] = path.name

        splitter = SentenceSplitter(
            chunk_size=self.chunk_size, chunk_overlap=self.chunk_overlap
        )
        nodes = splitter.get_nodes_from_documents(documents)

        index = self._get_or_create_index()
        index.insert_nodes(nodes)
        index.storage_context.persist(persist_dir=str(self.storage_dir))
        self._register_book(book_title, path.name, len(nodes))
        return book_title

    def add_books(self, docs_dir: str | Path) -> list[str]:
        """フォルダ内の対応ファイルをまとめて取り込む。"""
        docs_dir = Path(docs_dir)
        added: list[str] = []
        exts = {".pdf", ".txt", ".md", ".docx", ".pptx", ".csv", ".html"}
        for f in sorted(docs_dir.iterdir()):
            if f.is_file() and f.suffix.lower() in exts:
                added.append(self.add_book(f))
        return added

    # ---- 問い合わせ --------------------------------------------------------

    def query(
        self, question: str, *, title: str | None = None, top_k: int | None = None
    ) -> Answer:
        """質問し、回答とページ出典を返す。title 指定でその本だけに絞る。"""
        from llama_index.core.vector_stores import (
            FilterOperator,
            MetadataFilter,
            MetadataFilters,
        )

        from .rag import apply_llama_settings

        apply_llama_settings()
        index = self._get_or_create_index(create_if_missing=False)
        if index is None:
            raise RuntimeError("まだ本が取り込まれていません。add_book() を実行してください。")

        filters = None
        if title:
            filters = MetadataFilters(
                filters=[MetadataFilter(key="title", value=title, operator=FilterOperator.EQ)]
            )

        engine = index.as_query_engine(
            similarity_top_k=top_k or self.top_k, filters=filters
        )
        response = engine.query(question)

        sources = []
        for node in getattr(response, "source_nodes", []) or []:
            meta = node.node.metadata
            text = node.node.get_content().strip().replace("\n", " ")
            sources.append(
                Source(
                    title=meta.get("title", meta.get("source", "?")),
                    page=meta.get("page_label"),
                    score=getattr(node, "score", None),
                    snippet=(text[:120] + "…") if len(text) > 120 else text,
                )
            )
        return Answer(text=str(response).strip(), sources=sources)

    def ask(self, question: str, *, title: str | None = None, top_k: int | None = None) -> Answer:
        """query() のエイリアス（print() 前提の対話用）。"""
        return self.query(question, title=title, top_k=top_k)

    # ---- 管理 --------------------------------------------------------------

    def books(self) -> list[dict]:
        """取り込み済みの本の一覧（書名・ファイル名・チャンク数）。"""
        if self._catalog_path.exists():
            return json.loads(self._catalog_path.read_text(encoding="utf-8"))
        return []

    def reset(self) -> None:
        """インデックスとカタログを削除する（全消去）。"""
        import shutil

        if self.storage_dir.exists():
            shutil.rmtree(self.storage_dir)
        self._index = None

    # ---- 内部 --------------------------------------------------------------

    def _get_or_create_index(self, create_if_missing: bool = True):
        from llama_index.core import (
            StorageContext,
            VectorStoreIndex,
            load_index_from_storage,
        )

        if self._index is not None:
            return self._index

        # docstore.json があれば既存インデックスとみなしてロード。
        if (self.storage_dir / "docstore.json").exists():
            ctx = StorageContext.from_defaults(persist_dir=str(self.storage_dir))
            self._index = load_index_from_storage(ctx)
        elif create_if_missing:
            self.storage_dir.mkdir(parents=True, exist_ok=True)
            self._index = VectorStoreIndex([])
        return self._index

    def _register_book(self, title: str, source: str, n_chunks: int) -> None:
        catalog = self.books()
        existing = next((b for b in catalog if b["source"] == source), None)
        if existing:
            existing.update(title=title, chunks=n_chunks)
        else:
            catalog.append({"title": title, "source": source, "chunks": n_chunks})
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self._catalog_path.write_text(
            json.dumps(catalog, ensure_ascii=False, indent=2), encoding="utf-8"
        )
