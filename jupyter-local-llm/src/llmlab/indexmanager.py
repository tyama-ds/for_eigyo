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

import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .pagedrag import PagedRAG, content_hash

INDEX_MODES = ("fast", "hierarchy", "graph")
STATUSES = ("pending", "running", "ready", "failed", "skipped")
DEFAULT_STORAGE = "./storage/index"

# フォルダ一括取り込みの対応拡張子（fast はベクトル索引に入る全形式）
FOLDER_EXTS = {".pdf", ".txt", ".md", ".docx", ".doc", ".pptx",
               ".csv", ".xlsx", ".xls", ".html", ".epub"}
# hierarchy/graph（BookRAG の木/KG）を作れる形式。それ以外は fast に自動降格
BOOK_EXTS = {".pdf", ".docx", ".md", ".txt", ".pptx", ".xlsx"}


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


@dataclass
class DocAnswer:
    """ask() / summarize() の結果。text が回答/要約本文（Markdown 可）。"""

    text: str
    hits: list[SearchHit] = field(default_factory=list)   # 根拠（ask）
    per_doc: list[dict] = field(default_factory=list)     # 文書別の部分要約（summarize）

    def to_dict(self) -> dict:
        return {"text": self.text, "hits": [h.to_dict() for h in self.hits],
                "per_doc": self.per_doc}

    def __str__(self) -> str:
        out = [self.text]
        if self.hits:
            out += ["", "── 根拠（文書別） ──"]
            for h in self.hits:
                out.append(f"■ {h.title}（{h.doc_id}, score {h.score}）")
        if self.per_doc:
            out += ["", "── 文書別の要約 ──"]
            for p in self.per_doc:
                out.append(f"■ {p['title']}（{p['doc_id']}）\n{p['text']}")
        return "\n".join(out)


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
        resolved = str(path.resolve())

        prev = self._read(self.docs_dir, doc_id)
        if prev and prev.get("content_hash") == chash and prev.get("status") == "ready" \
                and prev.get("index_mode") == index_mode and not force:
            self._set_status(doc_id, "skipped", index_mode, note="変更なし（キャッシュ利用）")
            prev["status"] = "skipped"  # 呼び出し元への通知のみ（永続の status は ready のまま）
            return prev

        created = prev.get("created_at") if prev else _now()

        def _log(msg):
            if progress:
                try:
                    progress({"stage": str(msg), "current": 0, "total": 1, "detail": ""})
                except Exception:  # noqa: BLE001
                    pass

        # 同じ元ファイルの旧版（別 doc_id）が登録済みなら置き換える。
        # 内容ハッシュIDのため、ファイル編集後の再登録は新IDになる — 旧版を残すと
        # 一覧に新旧が並び、検索が古い本文を混ぜて返す。
        for old in self.documents():
            if old.get("source_path") == resolved and old["doc_id"] != doc_id:
                _log(f"旧版 {old['doc_id']} を置き換えます（内容が変更されたため）")
                print(f"[IndexManager] {path.name}: 旧版（doc_id={old['doc_id']}）を"
                      "削除して新しい内容で登録します")
                self.delete(old["doc_id"])

        self._set_status(doc_id, "running", index_mode)
        meta = {
            "doc_id": doc_id, "title": title, "source_path": resolved,
            "content_hash": chash, "index_mode": index_mode, "status": "running",
            "chunk_count": 0, "created_at": created, "updated_at": _now(),
            "graph_index": False, "layout": bool(layout), "error": None,
        }
        self._write(self.docs_dir, doc_id, meta)
        try:
            # 1) 共有ベクトル索引に chunk+embedding（全モード共通の土台）。
            #    内容が変わらないモード変更（fast→graph 等）では再埋め込みしない
            #    （ローカルの埋め込みサーバでは embedding が高コストなため）。
            same_content = bool(prev and prev.get("content_hash") == chash)
            have_chunks = (self.chunks_dir / f"{doc_id}.json").exists()
            same_mode_force = force and prev is not None \
                and prev.get("index_mode") == index_mode
            if (not same_content) or (not have_chunks) or same_mode_force:
                _log("チャンク化＋埋め込み")
                with self._forward_logs(progress):
                    self._paged.add_book(path, title=title, doc_id=doc_id, force=True)
            else:
                _log("チャンク/埋め込みは変更なしのため再利用")
            chunks = self._paged.document(doc_id) or {}
            meta["chunk_count"] = len(chunks.get("chunks", []))

            # 2) hierarchy / graph: 文書ごとの BookRAG 索引。
            #    fast では作らず、既存の木/KG が残っていれば消す（詳細表示との矛盾防止）。
            book_dir = self.bookindex_dir / doc_id
            if index_mode in ("hierarchy", "graph"):
                from .bookrag import BookRAG

                if book_dir.exists():
                    shutil.rmtree(book_dir)
                book = BookRAG(storage_dir=str(book_dir))
                build_graph = index_mode == "graph"
                _log("セクション木を構築" + ("＋Entity/Relation抽出（graph・低速）"
                                          if build_graph else "（hierarchy）"))
                with self._forward_logs(progress):
                    book.add_book(path, title=title, doc_id=doc_id, force=True,
                                  build_graph=build_graph, layout=layout, ocr=ocr)
                meta["graph_index"] = build_graph and book.has_graph()
            else:
                shutil.rmtree(book_dir, ignore_errors=True)

            meta.update(status="ready", updated_at=_now())
            self._write(self.docs_dir, doc_id, meta)
            self._set_status(doc_id, "ready", index_mode)
            return meta
        except Exception as e:  # noqa: BLE001  握りつぶさず記録して再送出
            meta.update(status="failed", error=f"{type(e).__name__}: {e}", updated_at=_now())
            self._write(self.docs_dir, doc_id, meta)
            self._set_status(doc_id, "failed", index_mode, error=meta["error"])
            raise

    def add_folder(self, docs_dir: str | Path, *, index_mode: str = "fast",
                   force: bool = False, layout=False, ocr=False,
                   progress=None) -> dict:
        """フォルダ内の対応文書を **1ファイル=1文書** として順に取り込む。

        - 各ファイルは個別の doc_id を持つ独立文書になる（検索は従来どおり
          文書ごとに chunk top-k を取り doc_id 単位で集約・多様化する）。
        - hierarchy/graph 指定時、木/KG を作れない形式（csv/html 等）は
          そのファイルだけ fast に自動降格して取り込む（スキップしない）。
        - 1ファイルの失敗で全体を止めない（status=failed に記録して続行）。
        - 返り値: {"results": [meta...], "added", "skipped", "failed", "errors"}
        """
        docs_dir = Path(docs_dir)
        if not docs_dir.is_dir():
            raise NotADirectoryError(f"フォルダではありません: {docs_dir}")
        files = sorted(f for f in docs_dir.iterdir()
                       if f.is_file() and f.suffix.lower() in FOLDER_EXTS)
        if not files:
            raise FileNotFoundError(
                f"{docs_dir} に対応文書がありません"
                f"（対応: {', '.join(sorted(FOLDER_EXTS))}）")

        def emit(stage, cur, detail=""):
            if progress:
                try:
                    progress({"stage": stage, "current": cur, "total": len(files),
                              "detail": detail})
                except Exception:  # noqa: BLE001
                    pass

        results, errors = [], []
        added = skipped = failed = 0
        for i, f in enumerate(files):
            eff_mode = index_mode
            if index_mode != "fast" and f.suffix.lower() not in BOOK_EXTS:
                eff_mode = "fast"
                emit(f"[{i + 1}/{len(files)}] {f.name}", i,
                     f"{f.suffix} は木/KG 非対応のため fast で取り込み")

            # ファイル内の進捗（チャンク化・抽出フェーズ等）は detail に流し、
            # バーはフォルダ全体（i/total）で進める
            def _inner(evt, _i=i, _name=f.name):
                emit(f"[{_i + 1}/{len(files)}] {_name}", _i, str(evt.get("stage", "")))

            emit(f"[{i + 1}/{len(files)}] {f.name}", i, f"取り込み中（{eff_mode}）")
            try:
                meta = self.add_document(f, index_mode=eff_mode, force=force,
                                         layout=layout, ocr=ocr, progress=_inner)
                results.append(meta)
                if meta.get("status") == "skipped":
                    skipped += 1
                else:
                    added += 1
            except Exception as e:  # noqa: BLE001  1件の失敗で全体を止めない
                failed += 1
                errors.append({"file": f.name, "error": f"{type(e).__name__}: {e}"})
                print(f"[IndexManager] {f.name} の取り込みに失敗（続行）: {e}")
        emit("完了", len(files),
             f"追加 {added} / 変更なし {skipped} / 失敗 {failed}")
        return {"results": results, "added": added, "skipped": skipped,
                "failed": failed, "errors": errors}

    def rebuild(self, doc_id: str, *, index_mode: str | None = None,
                progress=None) -> dict:
        """文書を（必要なら別モードで）作り直す（force=True 相当）。

        index_mode 省略時は **現在のモードを維持** する（fast に降格しない）。
        """
        meta = self._read(self.docs_dir, doc_id)
        if not meta:
            raise KeyError(f"未登録の doc_id: {doc_id}")
        src = meta.get("source_path")
        if not src or not Path(src).exists():
            raise FileNotFoundError(f"元ファイルが見つかりません: {src}")
        return self.add_document(src, title=meta.get("title"),
                                 index_mode=index_mode or meta.get("index_mode", "fast"),
                                 layout=meta.get("layout", False),  # 見出し判定設定を維持
                                 force=True, progress=progress)

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
        """登録済み文書のメタ一覧（更新日時の新しい順）。

        status JSON は「進行中/失敗」の過渡状態のみ上書きに使う。skipped（変更なし
        キャッシュ）は直近操作の記録であって索引の状態ではないため、一覧では
        meta の ready をそのまま見せる（恒久的に「変更なし」と表示しない）。
        """
        out = []
        if self.docs_dir.exists():
            for p in self.docs_dir.glob("*.json"):
                m = self._read_path(p)
                if m:
                    st = self._read(self.status_dir, m["doc_id"]) or {}
                    if st.get("status") in ("running", "failed", "pending"):
                        m["status"] = st["status"]
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
               use_graph: bool = False, doc_ids: list[str] | None = None) -> list[SearchHit]:
        """文書間検索。まず doc_id 単位で候補文書を選び、各文書内で chunk top-k を取る。

        - いきなり全チャンク global top_k を取らず、doc_id で集約して多様化する。
        - use_graph=True で graph 索引がある文書は BookRAG 検索を使い、無ければ通常RAG
          へフォールバック（落とさない。fallback_reason に理由を記録）。
        - doc_ids 指定時はその文書だけを対象にする（文書ごとに検索して集約）。
        """
        cap = max_chunks_per_doc or chunk_top_k_per_doc
        if doc_ids:
            # 対象文書が指定されているときは文書ごとに検索してスコア順に並べる
            from types import SimpleNamespace

            ranked = []
            for did in doc_ids:
                nodes = self._paged.retrieve_in_doc(question, doc_id=did,
                                                    top_m=chunk_top_k_per_doc)
                score = max((getattr(n, "score", 0.0) or 0.0 for n in nodes), default=0.0)
                meta = self._read(self.docs_dir, did) or {}
                ranked.append(SimpleNamespace(doc_id=did, score=score,
                                              title=meta.get("title", did)))
            ranked.sort(key=lambda r: r.score, reverse=True)
            ranked = ranked[:document_top_n]
        else:
            cand_k = max(document_top_n * chunk_top_k_per_doc * 5, 50)
            ranked = self._paged.rank_documents(
                question, candidate_chunk_k=cand_k, top_n=document_top_n,
                chunks_per_doc=chunk_top_k_per_doc)

        hits: list[SearchHit] = []
        for r in ranked:
            # メタは候補文書（top-N 件）だけ遅延読み込み（全件走査しない）
            meta = self._read(self.docs_dir, r.doc_id) or {}
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

    # ---- 回答生成・要約（検索 + LLM 合成） ----------------------------------

    def ask(self, question: str, *, document_top_n: int = 4,
            chunk_top_k_per_doc: int = 4, max_chunks_per_doc: int | None = None,
            use_graph: bool = False, doc_ids: list[str] | None = None,
            progress=None) -> DocAnswer:
        """質問に **回答を生成** する（検索 → 文書別の根拠を文脈に LLM で合成）。

        「要約してください」「比較してください」のような依頼文にもそのまま応える。
        根拠は doc_id 単位で多様化され、回答には文書名を明示させる。
        """
        from . import bookindex as bx

        def emit(stage, cur, total):
            if progress:
                try:
                    progress({"stage": stage, "current": cur, "total": total, "detail": ""})
                except Exception:  # noqa: BLE001
                    pass

        emit("関連文書を検索", 0, 2)
        hits = self.search(question, document_top_n=document_top_n,
                           chunk_top_k_per_doc=chunk_top_k_per_doc,
                           max_chunks_per_doc=max_chunks_per_doc,
                           use_graph=use_graph, doc_ids=doc_ids)
        if not hits or not any(h.chunks for h in hits):
            return DocAnswer(text="該当する文書が見つかりませんでした。"
                                  "文書が登録済みか、質問の言い換えを確認してください。",
                             hits=hits)
        ctx = "\n\n".join(
            f"### 文書「{h.title}」\n" + "\n".join(
                f"- {('p.' + str(c['page']) + ' ') if c.get('page') else ''}{c['text'][:800]}"
                for c in h.chunks)
            for h in hits)
        emit("回答を生成", 1, 2)
        text = bx.llm_text(
            f"依頼: {question}\n\n文書からの抜粋:\n{ctx}",
            system=("あなたは文書アシスタントです。以下の抜粋のみに基づいて依頼に日本語で"
                    "応えてください（回答・要約・比較など依頼の種類に従う）。"
                    "どの文書の情報かを文書名で明示し、抜粋に無い内容は推測しないでください。"),
        ).strip()
        emit("完了", 2, 2)
        return DocAnswer(text=text, hits=hits)

    def summarize(self, instruction: str | None = None, *,
                  doc_ids: list[str] | None = None, chunks_per_doc: int = 6,
                  progress=None) -> DocAnswer:
        """登録文書を **要約** する（文書ごとに部分要約 → 統合要約の Map-Reduce）。

        - doc_ids 省略時は登録済みの全文書が対象（文書ごとに1回 LLM を呼ぶため、
          ローカルLLMでは文書数に比例して時間がかかる）。
        - instruction で観点を指定できる（例: 「リスク面を中心に」）。
        - 検索ベースではなく各文書のチャンクを頭から均等に読むため、
          「全体を要約」のような特定トピックの無い依頼に強い。
        """
        from . import bookindex as bx

        metas = [m for m in self.documents()
                 if (not doc_ids) or m["doc_id"] in set(doc_ids)]
        if not metas:
            return DocAnswer(text="対象の文書がありません。先に文書を追加してください。")

        def emit(stage, cur, total):
            if progress:
                try:
                    progress({"stage": stage, "current": cur, "total": total, "detail": ""})
                except Exception:  # noqa: BLE001
                    pass

        per_doc: list[dict] = []
        total = len(metas) + 1
        for i, m in enumerate(metas):
            emit(f"文書を要約: {m['title']}", i, total)
            doc = self._paged.document(m["doc_id"]) or {}
            chunks = doc.get("chunks", [])
            # 文書全体をカバーするよう均等に間引く（先頭だけ読まない）
            step = max(1, len(chunks) // chunks_per_doc)
            picked = chunks[::step][:chunks_per_doc]
            body = "\n".join(f"- {c.get('text', '')[:800]}" for c in picked)
            if not body.strip():
                per_doc.append({"doc_id": m["doc_id"], "title": m["title"],
                                "text": "（本文を取得できませんでした）"})
                continue
            focus = f"特に次の観点を重視: {instruction}\n" if instruction else ""
            try:
                summ = bx.llm_text(
                    f"次の文書抜粋を、重要な数値・固有名詞を落とさず簡潔に要約してください。\n"
                    f"{focus}\n文書「{m['title']}」の抜粋:\n{body}").strip()
            except Exception as e:  # noqa: BLE001  1文書の失敗で全体を止めない
                summ = f"（要約に失敗: {type(e).__name__}: {e}）"
            per_doc.append({"doc_id": m["doc_id"], "title": m["title"], "text": summ})

        emit("統合要約を生成", len(metas), total)
        if len(per_doc) == 1:
            final = per_doc[0]["text"]
        else:
            blocks = "\n\n".join(f"■ {p['title']}\n{p['text']}" for p in per_doc)
            focus = f"特に次の観点を重視: {instruction}\n" if instruction else ""
            final = bx.llm_text(
                "以下は文書ごとの要約です。全体を貫く共通点・相違点が分かるように、"
                f"文書名を明示しながら日本語で統合要約を書いてください。\n{focus}\n{blocks}"
            ).strip()
        emit("完了", total, total)
        return DocAnswer(text=final, per_doc=per_doc)

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

    @staticmethod
    def _forward_logs(progress):
        """BookRAG/PagedRAG の内部ログ（bx.log）を progress へ転送するコンテキスト。

        bx.log_to はスレッドローカルのため、Studio で複数タスクが並行しても
        転送先が混線しない（旧実装のグローバル差し替えは並行実行で競合していた）。
        """
        from contextlib import nullcontext

        from . import bookindex as bx

        if progress is None:
            return nullcontext()

        def _fwd(msg):
            try:
                progress({"stage": str(msg), "current": 0, "total": 1, "detail": ""})
            except Exception:  # noqa: BLE001
                pass

        return bx.log_to(_fwd)

    def _set_status(self, doc_id, status, index_mode, *, error=None, note=None) -> None:
        self._write(self.status_dir, doc_id, {
            "doc_id": doc_id, "status": status, "index_mode": index_mode,
            "updated_at": _now(), "error": error, "note": note,
        })

    @staticmethod
    def _read_path(p: Path) -> dict | None:
        from .workspace import _read_json_file

        return _read_json_file(p, None)

    def _read(self, d: Path, doc_id: str) -> dict | None:
        return self._read_path(d / f"{doc_id}.json")

    @staticmethod
    def _write(d: Path, doc_id: str, obj: dict) -> None:
        from .workspace import _write_json_file

        _write_json_file(d / f"{doc_id}.json", obj)
