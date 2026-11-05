"""検索インデックス（BM25 + 埋め込み）と検索戦略。

- チャンクの検索用テキストは「文書タイトル > 見出し」を本文の前に付ける
  （Anthropic の Contextual Retrieval の軽量版。章タイトルの語でも当たるようにする）
- Hybrid: BM25 と埋め込み（空間ごと）を RRF で融合
- 埋め込みは「空間」（プロファイル × モデル）ごとに持ち、質問も同じ空間で埋め込む。
  社外秘文書はローカル空間、公開文書はクラウド空間…と混在してもよい
- 起動時に DB から全件ロードし、以後は文書の追加・削除で増分更新する
"""
from __future__ import annotations

import threading

from . import db
from .textutil import BM25Index, VectorIndex, rrf_fuse, unpack_vec


def index_text(doc_title: str, chunk: dict) -> str:
    head = " > ".join(x for x in (doc_title, chunk.get("heading") or "") if x)
    return f"[{head}]\n{chunk['text']}" if head else chunk["text"]


class SearchIndex:
    def __init__(self):
        self._lock = threading.RLock()
        self.loaded_for: str | None = None
        self.bm25 = BM25Index()
        self.vectors: dict[str, VectorIndex] = {}
        self.meta: dict[str, dict] = {}       # chunk_id -> {doc_id, ord, locator, page, heading}

    # ------------------------------------------------------------ 構築
    def ensure_loaded(self) -> None:
        with self._lock:
            if self.loaded_for == str(db.DATA_DIR):
                return
            self.bm25 = BM25Index()
            self.vectors = {}
            self.meta = {}
            titles = {r["id"]: r["title"] for r in db.query("SELECT id,title FROM documents")}
            for r in db.query("SELECT id,doc_id,ord,locator,heading,page,text FROM chunks"):
                ch = dict(r)
                self._add_chunk(titles.get(ch["doc_id"], ""), ch)
            for r in db.query("SELECT chunk_id,space,vec FROM vectors"):
                if r["chunk_id"] in self.meta:
                    self.vectors.setdefault(r["space"], VectorIndex()).add(
                        r["chunk_id"], unpack_vec(r["vec"]))
            self.loaded_for = str(db.DATA_DIR)

    def _add_chunk(self, title: str, ch: dict) -> None:
        self.meta[ch["id"]] = {"doc_id": ch["doc_id"], "ord": ch["ord"],
                               "locator": ch.get("locator") or "", "page": ch.get("page"),
                               "heading": ch.get("heading") or ""}
        self.bm25.add(ch["id"], index_text(title, ch))

    def add_doc(self, doc_id: str) -> None:
        self.ensure_loaded()
        with self._lock:
            self._remove(doc_id)
            doc = db.get_doc(doc_id)
            title = doc["title"] if doc else ""
            for ch in db.doc_chunks(doc_id):
                self._add_chunk(title, ch)

    def add_vectors(self, space: str, items: list[tuple[str, list[float]]]) -> None:
        self.ensure_loaded()
        with self._lock:
            vi = self.vectors.setdefault(space, VectorIndex())
            existing = set(vi.keys)
            for cid, vec in items:
                if cid not in existing:
                    vi.add(cid, vec)

    def remove_doc(self, doc_id: str) -> None:
        self.ensure_loaded()
        with self._lock:
            self._remove(doc_id)

    def _remove(self, doc_id: str) -> None:
        ids = [cid for cid, m in self.meta.items() if m["doc_id"] == doc_id]
        if not ids:
            return
        idset = set(ids)
        for cid in ids:
            self.bm25.remove(cid)
            self.meta.pop(cid, None)
        for space, vi in list(self.vectors.items()):
            if any(k in idset for k in vi.keys):
                nv = VectorIndex()
                for k, v in zip(vi.keys, vi.vecs):
                    if k not in idset:
                        nv.add(k, v)
                self.vectors[space] = nv

    def drop_vectors(self, doc_id: str) -> None:
        with self._lock:
            ids = {cid for cid, m in self.meta.items() if m["doc_id"] == doc_id}
            for space, vi in list(self.vectors.items()):
                nv = VectorIndex()
                for k, v in zip(vi.keys, vi.vecs):
                    if k not in ids:
                        nv.add(k, v)
                self.vectors[space] = nv

    # ------------------------------------------------------------ 検索
    def spaces_for(self, allowed_docs: set[str] | None) -> list[str]:
        self.ensure_loaded()
        out = []
        for space, vi in self.vectors.items():
            if not len(vi):
                continue
            if allowed_docs is None or any(self.meta.get(k, {}).get("doc_id") in allowed_docs
                                           for k in vi.keys[:2000]):
                out.append(space)
        return out

    def hybrid(self, query: str, *, allowed_docs: set[str] | None, k: int = 20,
               qvecs: dict[str, list[float]] | None = None) -> list[tuple[str, float]]:
        """BM25 + 各空間のベクトル検索を RRF で融合。qvecs は {space: 質問ベクトル}。"""
        self.ensure_loaded()

        def allow(cid):
            return allowed_docs is None or self.meta.get(cid, {}).get("doc_id") in allowed_docs

        rankings = [[cid for cid, _ in self.bm25.search(query, k=k * 2, allow=allow)]]
        for space, qv in (qvecs or {}).items():
            vi = self.vectors.get(space)
            if vi is not None and qv:
                rankings.append([cid for cid, _ in vi.search(qv, k=k * 2, allow=allow)])
        rankings = [r for r in rankings if r]
        if not rankings:
            return []
        return rrf_fuse(rankings)[:k]

    def bm25_only(self, query: str, *, allowed_docs: set[str] | None, k: int = 20):
        self.ensure_loaded()
        return self.bm25.search(query, k=k, allow=lambda cid: allowed_docs is None
                                or self.meta.get(cid, {}).get("doc_id") in allowed_docs)


INDEX = SearchIndex()


def load_chunks(ids: list[str]) -> dict[str, dict]:
    """チャンク ID → チャンク（+ 文書タイトル・区分）。"""
    if not ids:
        return {}
    out: dict[str, dict] = {}
    for i in range(0, len(ids), 500):
        part = ids[i:i + 500]
        marks = ",".join("?" * len(part))
        for r in db.query(
                f"SELECT c.id,c.doc_id,c.ord,c.locator,c.heading,c.page,c.text,"
                f"d.title AS doc_title,d.sensitivity,d.ext,d.filename "
                f"FROM chunks c JOIN documents d ON d.id=c.doc_id WHERE c.id IN ({marks})", part):
            out[r["id"]] = dict(r)
    return out
