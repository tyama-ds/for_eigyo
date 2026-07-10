"""PagedRAG / DocRAG — 文書・長尺PDF 向けの標準ベクトル RAG（ページ出典つき）。

LlamaIndex の素直なベクトル検索に、書名・ページ番号の出典付与と本単位フィルタを
足した実装。**論文 BookRAG（階層ツリー＋KG＋エージェント検索）とは別物**で、
「複数文書をまとめてページ出典つきで引きたい」軽量用途向け。BookRAG-inspired 版は
`llmlab.BookRAG`（bookrag.py）を参照。

使い方::

    import llmlab
    llmlab.configure(base_url=..., api_key=..., model=..., embed_model=...)

    rag = llmlab.PagedRAG()           # DocRAG は別名
    rag.add_book("./docs/営業マニュアル.pdf", title="営業マニュアル")
    rag.add_book("./docs/製品仕様.pdf")          # title 省略時はファイル名

    print(rag.ask("返品の手順は？"))             # 回答＋ページ出典を表示
    print(rag.ask("料金体系は？", title="営業マニュアル"))  # 特定の文書だけに絞る

特徴:
- 複数文書を1インデックスで管理（文書ごとに doc_id / title / source / page を付与）
- 文書向けのチャンク分割（既定 1024 トークン / オーバーラップ 128）
- 回答に **書名・ページ番号** の出典を添える
- 取り込み済みインデックスは storage_dir に永続化され再利用される
- 文書ごとの中身（チャンク）を documents/{doc_id}.json で個別確認できる
- 文書単位のスコア集約検索（rank_documents）で「1文書のチャンクばかり上位」を防ぐ
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_STORAGE = "./storage/books"
DEFAULT_CHUNK_SIZE = 1024
DEFAULT_CHUNK_OVERLAP = 128
DEFAULT_TOP_K = 5

# 文書IDを載せる node metadata のキー。LlamaIndex では "doc_id" が予約語扱いで
# MetadataFilter が効かない（ref_doc_id にマップされ 0 件になる）ため、専用キーを使う。
_DOC_ID_KEY = "llmlab_doc_id"


def content_hash(path: str | Path, *, chunk: int = 1 << 20) -> str:
    """ファイル内容の SHA-256（先頭16桁）。同一内容→同一、版違い→別。"""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(chunk), b""):
            h.update(block)
    return h.hexdigest()[:16]


def make_doc_id(path: str | Path, title: str | None = None) -> str:
    """文書の一意な ID を作る（既定は **内容ハッシュ** ベース）。

    - `title` / `path.name` には依存しない。同名文書・版違い・別ファイルを正しく区別
      できる（内容が同じなら別パスでも同一 ID = 冪等な再取り込み）。
    - 読み取れない場合のみ絶対パスのハッシュにフォールバックする。
    - title 引数は呼び出し側の互換のため残すが、ID には影響しない。
    """
    try:
        return "d" + content_hash(path)
    except OSError:
        resolved = str(Path(path).resolve())
        return "d" + hashlib.md5(resolved.encode("utf-8")).hexdigest()[:12]


@dataclass
class Source:
    """回答の根拠となった箇所。"""

    title: str
    page: str | None
    score: float | None
    snippet: str
    path: str | None = None  # 元ファイルの絶対パス（出典リンク用）
    doc_id: str | None = None  # 文書の一意 ID（同名でも衝突しない）

    def __str__(self) -> str:
        page = f" p.{self.page}" if self.page else ""
        score = f" ({self.score:.2f})" if self.score is not None else ""
        ref = f"\n  ↳ {self.path}" if self.path else ""
        return f"- {self.title}{page}{score}: {self.snippet}{ref}"


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


@dataclass
class DocRank:
    """rank_documents の結果1件（文書単位のスコアと代表チャンク）。"""

    doc_id: str
    title: str
    score: float
    n_chunks: int  # 候補集合内でこの文書に属したチャンク数
    chunks: list = field(default_factory=list)  # NodeWithScore（上位 chunks_per_doc 件）


class PagedRAG:
    """文書・長尺PDF を取り込み、ページ出典つきで問い合わせる標準ベクトル RAG。"""

    def __init__(
        self,
        storage_dir: str | Path = DEFAULT_STORAGE,
        *,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        chunk_overlap: int = DEFAULT_CHUNK_OVERLAP,
        top_k: int = DEFAULT_TOP_K,
        documents_dir: str | Path | None = None,
    ):
        self.storage_dir = Path(storage_dir)
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
        self.top_k = top_k
        self._index = None  # 遅延ロード
        self._catalog_path = self.storage_dir / "books.json"
        # 文書ごとの JSON（中身の個別確認用）。既定は storage_dir/documents。
        self.documents_dir = Path(documents_dir) if documents_dir else self.storage_dir / "documents"

    # ---- 取り込み ----------------------------------------------------------

    def add_book(self, path: str | Path, *, title: str | None = None,
                 force: bool = False, doc_id: str | None = None) -> str:
        """1冊の本（PDF/txt/md/docx など）を取り込み、インデックスへ追加する。

        同じ文書（= 同じ内容ハッシュの doc_id）が取り込み済みの場合は重複追加を防ぐため
        スキップする（ベクトル索引は追記型のため、再取り込みすると検索結果が重複する）。
        取り込み直したいときは force=True か reset() を使う。

        doc_id を明示指定すると、その ID で登録する（IndexManager が内容ハッシュ ID を
        共有するため）。省略時は make_doc_id(path) で内容ハッシュから導出する。
        """
        from llama_index.core import SimpleDirectoryReader
        from llama_index.core.node_parser import SentenceSplitter

        from .rag import apply_llama_settings

        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"ファイルがありません: {path}")

        doc_id = doc_id or make_doc_id(path, title)
        # doc_id で重複判定（同名ファイルが別フォルダにある場合でも衝突しない）。
        # 旧カタログ（doc_id 無し）は source 名で後方互換的に判定する。
        existing = next(
            (b for b in self.books()
             if (b.get("doc_id") == doc_id) or
                (b.get("doc_id") is None and b.get("source") == path.name)),
            None,
        )
        if existing and not force:
            print(f"[PagedRAG] {path.name} は取り込み済みのためスキップします"
                  "（重複防止。再取り込みは force=True、全消去は reset()）")
            return existing["title"]

        apply_llama_settings()
        book_title = title or path.stem

        # ページ単位のメタデータ（PDF なら page_label）を保持して読み込む。
        documents = SimpleDirectoryReader(input_files=[str(path)]).load_data()
        for d in documents:
            d.metadata[_DOC_ID_KEY] = doc_id
            d.metadata["title"] = book_title
            d.metadata["source"] = path.name
            d.metadata["path"] = str(path.resolve())  # 出典リンク用の絶対パス
            # 文書ID / 絶対パスは LLM のコンテキストに混ぜない（回答が汚れる/冗長化する）
            excluded = set(d.excluded_llm_metadata_keys or [])
            d.excluded_llm_metadata_keys = list(excluded | {"path", _DOC_ID_KEY})

        splitter = SentenceSplitter(
            chunk_size=self.chunk_size, chunk_overlap=self.chunk_overlap
        )
        nodes = splitter.get_nodes_from_documents(documents)
        for n in nodes:  # チャンク側にも文書ID を確実に持たせる
            n.metadata.setdefault(_DOC_ID_KEY, doc_id)

        index = self._get_or_create_index()
        if existing and force:
            # 追記型ストアなので、削除せず insert すると同じチャンクが二重に入り、
            # その文書が top-k を独占して多様化が壊れる。再取り込み前に旧チャンクを消す。
            self._delete_doc_nodes(index, doc_id)
        index.insert_nodes(nodes)
        index.storage_context.persist(persist_dir=str(self.storage_dir))
        self._register_book(book_title, path.name, len(nodes), doc_id,
                            path=str(path.resolve()))
        self._write_document_json(doc_id, book_title, path.name,
                                  str(path.resolve()), nodes)
        return book_title

    def _delete_doc_nodes(self, index, doc_id: str) -> None:
        """再取り込み(force=True)前に、その文書の既存チャンクを索引から削除する。

        チャンク ID は文書JSON（前回取り込み時に記録）から取得する。JSON が無い
        旧索引では特定できないため、その場合は案内のみ（reset() を推奨）。
        """
        doc = self.document(doc_id)
        ids = [c.get("chunk_id") for c in (doc or {}).get("chunks", []) if c.get("chunk_id")]
        if not ids:
            if doc is None:
                print("[PagedRAG] force=True: 旧チャンクの記録（文書JSON）が無いため置換できません"
                      "（文書JSON対応より前に取り込まれた索引の可能性）。"
                      "重複を避けるには reset() で作り直してください。")
            return
        try:
            index.delete_nodes(ids, delete_from_docstore=True)
        except Exception as e:  # noqa: BLE001  一部バックエンドは delete 非対応
            print(f"[PagedRAG] force=True: 旧チャンクの削除に失敗（重複の恐れ）: {e}")

    def add_books(self, docs_dir: str | Path) -> list[str]:
        """フォルダ内の対応ファイルをまとめて取り込む。"""
        docs_dir = Path(docs_dir)
        added: list[str] = []
        exts = {".pdf", ".txt", ".md", ".docx", ".doc", ".pptx",
                ".csv", ".xlsx", ".xls", ".html", ".epub"}
        for f in sorted(docs_dir.iterdir()):
            if f.is_file() and f.suffix.lower() in exts:
                added.append(self.add_book(f))
        return added

    # ---- 問い合わせ --------------------------------------------------------

    def query(
        self, question: str, *, title: str | None = None,
        doc_id: str | None = None, top_k: int | None = None,
    ) -> Answer:
        """質問し、回答とページ出典を返す。

        title / doc_id を指定するとその文書だけに絞る（doc_id が最も確実で、
        同名タイトルの取り違えが起きない）。
        """
        from .rag import apply_llama_settings

        apply_llama_settings()
        index = self._get_or_create_index(create_if_missing=False)
        if index is None:
            raise RuntimeError("まだ本が取り込まれていません。add_book() を実行してください。")

        filters = self._build_filters(title=title, doc_id=doc_id)
        engine = index.as_query_engine(
            similarity_top_k=top_k or self.top_k, filters=filters
        )
        response = engine.query(question)
        source_nodes = list(getattr(response, "source_nodes", []) or [])

        # フィルタの効き目を検証する。ベクトルストアの実装/バージョンによっては
        # フィルタが無視され「別文書の内容」で答えてしまうため、その場合は取得し直して
        # クライアント側でフィルタし、回答も作り直す（MultiPaperRAG の比較が同一文書
        # ばかりを見る症状の対策）。
        from .client import strip_think  # LlamaIndex 経由の応答にも思考過程が混ざるため除去

        want_key, want_val = (_DOC_ID_KEY, doc_id) if doc_id else ("title", title)
        if want_val is not None:
            got = {n.node.metadata.get(want_key) for n in source_nodes}
            # フィルタが無視される実装では「0件」または「別文書の混入」が起きる。
            # どちらの症状でも手動フィルタ（広めに取ってクライアント側で厳密に絞る）へ。
            if (not got) or (got - {want_val}):
                print(f"[PagedRAG] 警告: {want_key} フィルタが効いていません"
                      f"（要求: {want_val} / 取得: {got or '0件'}）。手動フィルタで再試行します。")
                response_text, source_nodes = self._manual_scoped_query(
                    index, question, want_key, want_val, top_k or self.top_k)
            else:
                response_text = strip_think(str(response))
        else:
            response_text = strip_think(str(response))

        return Answer(text=response_text,
                      sources=[self._node_to_source(n) for n in source_nodes])

    def _manual_scoped_query(self, index, question: str, key: str, value: str,
                             top_k: int):
        """フィルタ非対応ストア向けフォールバック: 広めに取得→クライアント側で
        key==value に絞り→その抜粋だけを文脈に LLM で回答を生成する。"""
        from .client import complete

        retriever = index.as_retriever(similarity_top_k=max(top_k * 10, 50))
        nodes = [n for n in retriever.retrieve(question)
                 if n.node.metadata.get(key) == value][:top_k]
        if not nodes:
            return f"（文書「{value}」から該当箇所を見つけられませんでした）", []
        ctx = "\n\n".join(
            f"[p.{n.node.metadata.get('page_label','?')}] {n.node.get_content()[:800]}"
            for n in nodes
        )
        text = complete(
            f"以下は文書「{value}」からの抜粋です。この抜粋のみに基づいて質問に答えてください。\n\n"
            f"質問: {question}\n\n抜粋:\n{ctx}"
        ).strip()
        return text, nodes

    # 後方互換の別名（title 限定版）。
    def _manual_title_query(self, index, question: str, title: str, top_k: int):
        return self._manual_scoped_query(index, question, "title", title, top_k)

    def ask(self, question: str, *, title: str | None = None,
            doc_id: str | None = None, top_k: int | None = None) -> Answer:
        """query() のエイリアス（print() 前提の対話用）。"""
        return self.query(question, title=title, doc_id=doc_id, top_k=top_k)

    # ---- 文書単位の検索（文書間検索のための多様化） ------------------------

    def rank_documents(
        self, question: str, *, candidate_chunk_k: int = 50,
        top_n: int = 4, chunks_per_doc: int = 4, agg: str = "max",
    ) -> list[DocRank]:
        """質問に対して **文書単位** で候補を集約して返す。

        「全チャンクから top-k を取って終わり」だと1文書のチャンクばかりが上位を
        占め、文書間検索にならない。そこで広めに candidate_chunk_k 件のチャンクを取り、
        doc_id ごとに group by し、文書スコア（max または top-n 平均）で上位 top_n
        文書を返す。各文書の代表チャンク（上位 chunks_per_doc 件）も同梱する。

        agg: "max"（既定・最大チャンクスコア）/ "avg"（上位 chunks_per_doc の平均）
        """
        nodes = self._retrieve(question, top_k=candidate_chunk_k)
        return self._aggregate_by_doc(nodes, top_n=top_n,
                                      chunks_per_doc=chunks_per_doc, agg=agg)

    @staticmethod
    def _aggregate_by_doc(nodes, *, top_n: int, chunks_per_doc: int,
                          agg: str = "max") -> list[DocRank]:
        """取得済みチャンク（NodeWithScore の並び）を doc_id 単位で集約する（純関数）。

        doc_id が無い旧索引のチャンクは title→source→node_id の順でキーにフォールバック。
        """
        groups: dict[str, list] = {}
        titles: dict[str, str] = {}
        for nw in nodes:
            meta = nw.node.metadata
            key = (meta.get(_DOC_ID_KEY) or meta.get("title")
                   or meta.get("source") or nw.node.node_id)
            groups.setdefault(key, []).append(nw)
            titles.setdefault(key, meta.get("title") or meta.get("source") or str(key))

        ranked: list[DocRank] = []
        for key, group in groups.items():
            group.sort(key=lambda n: (n.score if n.score is not None else 0.0),
                       reverse=True)
            scores = [n.score if n.score is not None else 0.0 for n in group]
            if agg == "avg":
                head = scores[:chunks_per_doc]
                doc_score = sum(head) / len(head) if head else 0.0
            else:  # "max"
                doc_score = max(scores) if scores else 0.0
            ranked.append(DocRank(
                doc_id=str(key), title=titles[key], score=doc_score,
                n_chunks=len(group), chunks=group[:chunks_per_doc],
            ))
        ranked.sort(key=lambda d: d.score, reverse=True)
        return ranked[:top_n]

    def retrieve_in_doc(self, question: str, *, doc_id: str | None = None,
                        title: str | None = None, top_m: int = 4) -> list:
        """1文書内だけで chunk top-m を取る（NodeWithScore の並び）。"""
        filters = self._build_filters(title=title, doc_id=doc_id)
        nodes = self._retrieve(question, top_k=top_m, filters=filters)
        key, val = (_DOC_ID_KEY, doc_id) if doc_id else ("title", title)
        if val is None:
            return nodes  # 絞り込み指定なし
        # フィルタが効いていれば全件が val に一致する。0件や別文書の混入（=フィルタ
        # 無視）のときは、広めに取ってクライアント側で厳密に絞るフォールバックへ。
        if nodes and all(n.node.metadata.get(key) == val for n in nodes):
            return nodes
        wide = self._retrieve(question, top_k=max(top_m * 10, 50))
        return [n for n in wide if n.node.metadata.get(key) == val][:top_m]

    def _retrieve(self, question: str, *, top_k: int, filters=None) -> list:
        """検索のみ（回答生成なし）で NodeWithScore の並びを返す。"""
        from .rag import apply_llama_settings

        apply_llama_settings()
        index = self._get_or_create_index(create_if_missing=False)
        if index is None:
            return []
        retriever = index.as_retriever(similarity_top_k=top_k, filters=filters)
        return list(retriever.retrieve(question))

    def _build_filters(self, *, title: str | None = None, doc_id: str | None = None):
        from llama_index.core.vector_stores import (
            FilterOperator,
            MetadataFilter,
            MetadataFilters,
        )

        flist = []
        if doc_id:
            flist.append(MetadataFilter(key=_DOC_ID_KEY, value=doc_id,
                                        operator=FilterOperator.EQ))
        if title:
            flist.append(MetadataFilter(key="title", value=title,
                                        operator=FilterOperator.EQ))
        return MetadataFilters(filters=flist) if flist else None

    @staticmethod
    def _node_to_source(node) -> Source:
        meta = node.node.metadata
        text = node.node.get_content().strip().replace("\n", " ")
        return Source(
            title=meta.get("title", meta.get("source", "?")),
            page=meta.get("page_label"),
            score=getattr(node, "score", None),
            snippet=(text[:120] + "…") if len(text) > 120 else text,
            # 旧索引には "path" が無い → SimpleDirectoryReader 由来の file_path に
            # フォールバック
            path=meta.get("path") or meta.get("file_path"),
            doc_id=meta.get(_DOC_ID_KEY),
        )

    # ---- 管理 --------------------------------------------------------------

    def books(self) -> list[dict]:
        """取り込み済みの本の一覧（doc_id・書名・ファイル名・チャンク数）。"""
        if self._catalog_path.exists():
            return json.loads(self._catalog_path.read_text(encoding="utf-8"))
        return []

    def document_ids(self) -> list[str]:
        """保存済みの文書 JSON の doc_id 一覧。"""
        if not self.documents_dir.exists():
            return []
        return sorted(p.stem for p in self.documents_dir.glob("*.json"))

    def document(self, doc_id: str) -> dict | None:
        """文書1つ分の JSON（doc_id / title / source / path / summary / chunks）。"""
        p = self.documents_dir / f"{doc_id}.json"
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None

    def set_summary(self, doc_id: str, summary: str) -> None:
        """文書 JSON の summary を更新する（無ければ何もしない）。"""
        doc = self.document(doc_id)
        if doc is None:
            return
        doc["summary"] = summary
        (self.documents_dir / f"{doc_id}.json").write_text(
            json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")

    def delete_document(self, doc_id: str) -> bool:
        """1文書分のチャンクを索引・カタログ・文書JSONから削除する。"""
        index = self._get_or_create_index(create_if_missing=False)
        removed = False
        if index is not None:
            self._delete_doc_nodes(index, doc_id)
            index.storage_context.persist(persist_dir=str(self.storage_dir))
            removed = True
        catalog = [b for b in self.books() if b.get("doc_id") != doc_id]
        if self._catalog_path.exists():
            self._catalog_path.write_text(
                json.dumps(catalog, ensure_ascii=False, indent=2), encoding="utf-8")
        jp = self.documents_dir / f"{doc_id}.json"
        if jp.exists():
            jp.unlink()
        return removed

    def reset(self) -> None:
        """インデックスとカタログ・文書JSONを削除する（全消去）。"""
        import shutil

        if self.storage_dir.exists():
            shutil.rmtree(self.storage_dir)
        # documents_dir が storage_dir と別系統（兄弟/独立）のときだけ個別に消す。
        # storage_dir の子孫（既に消済み）や祖先（消すと無関係ファイルまで巻き添え）は除外。
        docs = self.documents_dir.resolve()
        store = self.storage_dir.resolve()
        disjoint = (docs != store and store not in docs.parents
                    and docs not in store.parents)
        if self.documents_dir.exists() and disjoint:
            shutil.rmtree(self.documents_dir, ignore_errors=True)
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

    def _register_book(self, title: str, source: str, n_chunks: int,
                       doc_id: str, *, path: str | None = None) -> None:
        catalog = self.books()
        existing = next((b for b in catalog if b.get("doc_id") == doc_id), None)
        if existing is None:  # 旧カタログ（doc_id 無し）の同名エントリを引き継ぐ
            existing = next((b for b in catalog
                             if b.get("doc_id") is None and b.get("source") == source), None)
        entry = {"doc_id": doc_id, "title": title, "source": source,
                 "chunks": n_chunks, "path": path}
        if existing:
            existing.update(entry)
        else:
            catalog.append(entry)
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self._catalog_path.write_text(
            json.dumps(catalog, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def _write_document_json(self, doc_id: str, title: str, source: str,
                             path: str, nodes, summary: str = "") -> None:
        """文書ごとの JSON（中身の個別確認用）を保存する。

        構造: {doc_id, title, source, path, summary, chunks:[{chunk_id, text, page,
        metadata}]}。埋め込みベクトルは含めない（検索は vector index を使う）。
        既存 JSON があれば summary は保持する。
        """
        existing = self.document(doc_id)
        if existing and not summary:
            summary = existing.get("summary", "")
        chunks = []
        for n in nodes:
            meta = dict(n.metadata)
            chunk_meta = {k: meta.get(k) for k in ("title", "source", "page_label", "path")
                          if meta.get(k) is not None}
            chunk_meta["doc_id"] = doc_id  # 予約語回避のため node では別キーに載せている
            chunks.append({
                "chunk_id": n.node_id,
                "text": n.get_content(),
                "page": meta.get("page_label"),
                "metadata": chunk_meta,
            })
        payload = {
            "doc_id": doc_id, "title": title, "source": source, "path": path,
            "summary": summary, "chunks": chunks,
        }
        self.documents_dir.mkdir(parents=True, exist_ok=True)
        (self.documents_dir / f"{doc_id}.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


# DocRAG は PagedRAG の別名（呼びやすい方を使う）。
DocRAG = PagedRAG
