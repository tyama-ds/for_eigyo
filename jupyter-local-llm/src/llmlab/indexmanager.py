"""IndexManager — 文書(doc_id)中心の文書間RAG。ローカルLLMでの実用性を最優先。

設計方針:
- 通常RAG（`fast`）を高速に使えるようにし、BookRAG Full（`graph`）は必要時だけ
  明示的に使う高コスト拡張にする。
- 文書は **内容ハッシュの doc_id** で識別（title/book名は表示用のみ）。同名文書・
  版違い・別ファイルを正しく区別できる。
- 文書ごとに JSON を個別保存（確認・再構築・削除できる）。

index_mode:
- ``fast``      : doc_id + チャンク + 埋め込みのみ。**既定**。ローカルLLMで軽い。
- ``hierarchy`` : 見出し/セクション階層まで作成。Entity/Relation 抽出はしない。
- ``graph``     : BookRAG Full 相当。Entity/Relation 抽出まで行う（低速・LLM多用）。

`graph` 未作成でも `fast`/`hierarchy` の検索は動く（検索は共有ベクトル索引を使う）。

保存レイアウト（storage_dir 既定 ``./storage/index``）::

    vectors/            共有ベクトル索引（fast/hierarchy の chunk+embedding）
    docs/{doc_id}.json      メタ（title/source_path/content_hash/index_mode/status…）
    chunks/{doc_id}.json    チャンク（PagedRAG が書き出す）
    bookindex/{doc_id}/     文書ごとの BookRAG 索引（hierarchy=木 / graph=木+KG）
    status/{doc_id}.json    ステータス（pending/running/ready/failed/skipped + error）

使い方::

    import llmlab
    llmlab.configure(...)
    im = llmlab.IndexManager()
    im.add_document("./docs/2024.pdf")                 # fast（既定・高速）
    im.add_document("./docs/規程.pdf", index_mode="graph")  # 明示時のみ重い抽出
    res = im.search("退職金の計算方法は？")             # 文書単位で多様化して検索
    for d in res: print(d["title"], d["doc_id"], d["score"])
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .pagedrag import PagedRAG, content_hash

INDEX_MODES = ("fast", "hierarchy", "graph")
STATUSES = ("pending", "running", "ready", "failed", "skipped")
DEFAULT_STORAGE = "./storage/index"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


@dataclass
class SearchHit:
    """検索結果1件（1文書分。チャンクは文書内に束ねる）。"""

    doc_id: str
    title: str
    score: float
    source_path: str | None = None
    used_graph: bool = False
    fallback_reason: str | None = None   # graph 要求だが未構築→通常RAG等
    chunks: list[dict] = field(default_factory=list)  # {text, page, score, source}

    def to_dict(self) -> dict:
        return {
            "doc_id": self.doc_id, "title": self.title, "score": self.score,
            "source_path": self.source_path, "used_graph": self.used_graph,
            "fallback_reason": self.fallback_reason, "chunks": self.chunks,
        }


class IndexManager:
    """doc_id 中心・index_mode 切替の文書間 RAG マネージャ。"""

    def __init__(self, storage_dir: str | Path = DEFAULT_STORAGE):
        self.root = Path(storage_dir)
        self.docs_dir = self.root / "docs"
        self.chunks_dir = self.root / "chunks"
        self.status_dir = self.root / "status"
        self.bookindex_dir = self.root / "bookindex"
        # fast/hierarchy 用の共有ベクトル索引。チャンクJSONは chunks/ に書き出す。
        self._paged = PagedRAG(storage_dir=str(self.root / "vectors"),
                               documents_dir=str(self.chunks_dir))

    # ---- 取り込み ----------------------------------------------------------

    def add_document(self, path: str | Path, *, title: str | None = None,
                     index_mode: str = "fast", force: bool = False,
                     layout=False, ocr=False, progress=None) -> dict:
        """文書を index_mode で取り込む。既定は fast（高速・通常RAG）。

        - graph 未指定なら Entity/Relation 抽出は走らない（重い処理は明示時だけ）。
        - 同じ doc_id かつ同じ content_hash が ready なら、force でない限り再抽出せず
          skipped で返す（キャッシュ/差分更新）。
        - 失敗は status=failed + error に記録し、例外は握りつぶさず再送出する。
        """
        if index_mode not in INDEX_MODES:
            raise ValueError(f"index_mode は {INDEX_MODES} のいずれか: {index_mode!r}")
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"ファイルがありません: {path}")

        chash = content_hash(path)
        doc_id = "d" + chash
        title = title or path.stem

        prev = self._read(self.docs_dir, doc_id)
        if prev and prev.get("content_hash") == chash and prev.get("status") == "ready" \
                and prev.get("index_mode") == index_mode and not force:
            self._set_status(doc_id, "skipped", index_mode, note="変更なし（キャッシュ利用）")
            prev["status"] = "skipped"
            return prev

        created = prev.get("created_at") if prev else _now()

        def _log(msg):
            if progress:
                try:
                    progress({"stage": msg, "current": 0, "total": 1, "detail": ""})
                except Exception:  # noqa: BLE001
                    pass

        self._set_status(doc_id, "running", index_mode)
        meta = {
            "doc_id": doc_id, "title": title, "source_path": str(path.resolve()),
            "content_hash": chash, "index_mode": index_mode, "status": "running",
            "chunk_count": 0, "created_at": created, "updated_at": _now(),
            "graph_index": False, "error": None,
        }
        self._write(self.docs_dir, doc_id, meta)
        try:
            # 1) fast: 共有ベクトル索引に chunk+embedding（全モード共通の土台）
            _log("チャンク化＋埋め込み（fast）")
            self._route_bx_progress(progress,
                lambda: self._paged.add_book(path, title=title, doc_id=doc_id, force=True))
            chunks = self._paged.document(doc_id) or {}
            meta["chunk_count"] = len(chunks.get("chunks", []))

            # 2) hierarchy / graph: 文書ごとの BookRAG 索引
            if index_mode in ("hierarchy", "graph"):
                from .bookrag import BookRAG

                book_dir = self.bookindex_dir / doc_id
                if book_dir.exists():
                    shutil.rmtree(book_dir)
                book = BookRAG(storage_dir=str(book_dir))
                build_graph = index_mode == "graph"
                _log("セクション木を構築" + ("＋Entity/Relation抽出（graph・低速）"
                                          if build_graph else "（hierarchy）"))
                self._route_bx_progress(progress, lambda: book.add_book(
                    path, title=title, doc_id=doc_id, force=True,
                    build_graph=build_graph, layout=layout, ocr=ocr))
                meta["graph_index"] = build_graph and book.has_graph()

            meta.update(status="ready", updated_at=_now())
            self._write(self.docs_dir, doc_id, meta)
            self._set_status(doc_id, "ready", index_mode)
            return meta
        except Exception as e:  # noqa: BLE001  握りつぶさず記録して再送出
            meta.update(status="failed", error=f"{type(e).__name__}: {e}", updated_at=_now())
            self._write(self.docs_dir, doc_id, meta)
            self._set_status(doc_id, "failed", index_mode, error=meta["error"])
            raise

    def rebuild(self, doc_id: str, *, index_mode: str | None = None) -> dict:
        """文書を（必要なら別モードで）作り直す（force=True 相当）。"""
        meta = self._read(self.docs_dir, doc_id)
        if not meta:
            raise KeyError(f"未登録の doc_id: {doc_id}")
        src = meta.get("source_path")
        if not src or not Path(src).exists():
            raise FileNotFoundError(f"元ファイルが見つかりません: {src}")
        return self.add_document(src, title=meta.get("title"),
                                 index_mode=index_mode or meta.get("index_mode", "fast"),
                                 force=True)

    def delete(self, doc_id: str) -> bool:
        """文書を全ストア（索引・チャンク・木/KG・メタ・status）から削除する。"""
        existed = bool(self._read(self.docs_dir, doc_id))
        try:
            self._paged.delete_document(doc_id)
        except Exception as e:  # noqa: BLE001
            print(f"[IndexManager] ベクトル索引からの削除に失敗（続行）: {e}")
        shutil.rmtree(self.bookindex_dir / doc_id, ignore_errors=True)
        for d in (self.docs_dir, self.chunks_dir, self.status_dir):
            p = d / f"{doc_id}.json"
            if p.exists():
                p.unlink()
        return existed

    # ---- 一覧・詳細 --------------------------------------------------------

    def documents(self) -> list[dict]:
        """登録済み文書のメタ一覧（更新日時の新しい順）。"""
        out = []
        if self.docs_dir.exists():
            for p in self.docs_dir.glob("*.json"):
                m = self._read_path(p)
                if m:
                    st = self._read(self.status_dir, m["doc_id"])
                    if st:  # status JSON を正とする（running 中に落ちた場合の整合）
                        m["status"] = st.get("status", m.get("status"))
                        m["error"] = st.get("error") or m.get("error")
                    out.append(m)
        out.sort(key=lambda m: m.get("updated_at", ""), reverse=True)
        return out

    def document(self, doc_id: str) -> dict | None:
        """文書1件の詳細（メタ + status + チャンク + 木の要約）。"""
        meta = self._read(self.docs_dir, doc_id)
        if not meta:
            return None
        chunks = self._paged.document(doc_id) or {}
        detail = {
            "meta": meta,
            "status": self._read(self.status_dir, doc_id) or {},
            "chunks": chunks.get("chunks", []),
            "bookindex": None,
        }
        book_dir = self.bookindex_dir / doc_id
        if (book_dir / "bookindex.json").exists():
            bi = self._read_path(book_dir / "bookindex.json") or {}
            nodes = bi.get("nodes", [])
            detail["bookindex"] = {
                "node_count": len(nodes),
                "section_count": sum(1 for n in nodes if n.get("type") == "Section"),
                "entity_count": len(bi.get("entities", [])),
                "relation_count": len(bi.get("relations", [])),
                "sections": [n.get("title") for n in nodes
                             if n.get("type") == "Section" and n.get("level")][:200],
            }
        return detail

    def status(self, doc_id: str) -> dict:
        return self._read(self.status_dir, doc_id) or {"doc_id": doc_id, "status": "unknown"}

    # ---- 検索（2段階: 文書 top-N → 文書内 chunk top-k） --------------------

    def search(self, question: str, *, document_top_n: int = 4,
               chunk_top_k_per_doc: int = 4, max_chunks_per_doc: int | None = None,
               use_graph: bool = False) -> list[SearchHit]:
        """文書間検索。まず doc_id 単位で候補文書を選び、各文書内で chunk top-k を取る。

        - いきなり全チャンク global top_k を取らず、doc_id で集約して多様化する。
        - use_graph=True で graph 索引がある文書は BookRAG 検索を使い、無ければ通常RAG
          へフォールバック（落とさない。fallback_reason に理由を記録）。
        """
        cap = max_chunks_per_doc or chunk_top_k_per_doc
        cand_k = max(document_top_n * chunk_top_k_per_doc * 5, 50)
        ranked = self._paged.rank_documents(
            question, candidate_chunk_k=cand_k, top_n=document_top_n,
            chunks_per_doc=chunk_top_k_per_doc)
        metas = {m["doc_id"]: m for m in self.documents()}

        hits: list[SearchHit] = []
        for r in ranked:
            meta = metas.get(r.doc_id, {})
            title = meta.get("title") or r.title
            hit = SearchHit(doc_id=r.doc_id, title=title, score=round(r.score, 4),
                            source_path=meta.get("source_path"))
            used_graph = False
            if use_graph:
                if meta.get("graph_index") and (self.bookindex_dir / r.doc_id).exists():
                    used_graph = self._graph_chunks(r.doc_id, question, hit, cap)
                else:
                    hit.fallback_reason = ("graph 索引が未作成のため通常RAGで検索"
                                           if meta else "メタ情報なし→通常RAG")
            if not used_graph:
                self._normal_chunks(r.doc_id, question, hit, chunk_top_k_per_doc, cap)
            hit.used_graph = used_graph
            hits.append(hit)
        return hits

    def _normal_chunks(self, doc_id, question, hit, top_k, cap) -> None:
        nodes = self._paged.retrieve_in_doc(question, doc_id=doc_id, top_m=max(top_k, cap))
        for n in nodes[:cap]:
            meta = n.node.metadata
            hit.chunks.append({
                "text": n.node.get_content().strip(),
                "page": meta.get("page_label"),
                "score": round(getattr(n, "score", 0.0) or 0.0, 4),
                "source": meta.get("path") or meta.get("source"),
            })

    def _graph_chunks(self, doc_id, question, hit, cap) -> bool:
        try:
            from .bookrag import BookRAG

            book = BookRAG(storage_dir=str(self.bookindex_dir / doc_id))
            ans = book.query(question)
            for e in ans.evidence[:cap]:
                hit.chunks.append({
                    "text": e.snippet, "page": e.page,
                    "score": round(e.s_text, 4), "source": e.source,
                })
            hit.chunks.insert(0, {"text": f"[BookRAG回答] {ans.text}", "page": None,
                                  "score": 1.0, "source": None})
            return True
        except Exception as e:  # noqa: BLE001  graph 検索失敗→通常RAGへ
            hit.fallback_reason = f"graph 検索に失敗→通常RAG（{type(e).__name__}）"
            return False

    # ---- 内部: JSON I/O ----------------------------------------------------

    def _route_bx_progress(self, progress, fn):
        """BookRAG/PagedRAG の内部ログ（bx.log）を progress へ転送しつつ fn を実行。"""
        if progress is None:
            return fn()
        from . import bookindex as bx

        old = bx.log

        def _fwd(msg):
            old(msg)
            try:
                progress({"stage": str(msg), "current": 0, "total": 1, "detail": ""})
            except Exception:  # noqa: BLE001
                pass

        bx.log = _fwd
        try:
            return fn()
        finally:
            bx.log = old

    def _set_status(self, doc_id, status, index_mode, *, error=None, note=None) -> None:
        self._write(self.status_dir, doc_id, {
            "doc_id": doc_id, "status": status, "index_mode": index_mode,
            "updated_at": _now(), "error": error, "note": note,
        })

    @staticmethod
    def _read_path(p: Path) -> dict | None:
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def _read(self, d: Path, doc_id: str) -> dict | None:
        return self._read_path(d / f"{doc_id}.json")

    @staticmethod
    def _write(d: Path, doc_id: str, obj: dict) -> None:
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{doc_id}.json").write_text(
            json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
