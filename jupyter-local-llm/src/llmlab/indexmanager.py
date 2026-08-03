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

import hashlib
import re
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

# ---- 検索パイプラインの既定値 ------------------------------------------------
# document_top_n は「doc_ids 未指定時の自動文書選定」にだけ効く（明示選択は全件）。
DEFAULT_DOC_TOP_N = 8
# 文書内: 広めに候補を取り（>= この値）、リランキング後に chunk_top_k 件を採用する
DEFAULT_CHUNK_CANDIDATES = 8
# ask() の最終コンテキストに入れる抜粋合計の文字予算（無制限投入を防ぐ）
CONTEXT_CHAR_BUDGET = 12000
# ask() で対象文書がこれ以上なら Map-Reduce（文書ごとに部分回答→統合）へ切替
MAP_REDUCE_DOC_THRESHOLD = 6

# ---- graph（Entity/Relation 抽出）のローカルLLM向け安全既定 -------------------
SAFE_GRAPH_DEFAULTS = {
    "graph_max_workers": 1,    # ローカルLLMは並列に弱い（GPU/KVキャッシュ/JSON崩れ）
    "graph_max_nodes": 100,    # セクション均等サンプリングの上限
    "graph_chunk_chars": 2000, # チャンクを大きめに＝LLM呼び出し回数を減らす
    "er_use_llm": False,
}


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


_TERM_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9\-_.]{1,}|[0-9０-９]+(?:\.[0-9０-９]+)*")


def _keyword_terms(question: str) -> list[str]:
    """キーワード検索用の語（英数字・型番・数値・規程番号など）を抽出する。"""
    return [t.lower() for t in _TERM_RE.findall(question or "") if len(t) >= 2]


def _keyword_bonus(terms: list[str], text: str) -> float:
    """ベクトルスコアに足すキーワード一致ボーナス（0〜0.2）。

    埋め込みが苦手な数値・製品型番・規程番号の完全一致を拾う軽量ハイブリッド。
    """
    if not terms:
        return 0.0
    lower = (text or "").lower()
    hit = sum(1 for t in terms if t in lower)
    return 0.2 * hit / len(terms)


_HEADING_RE = re.compile(
    r"^(?:#{1,4}\s+.+|第\s*[0-9０-９一二三四五六七八九十]+\s*[章節条部].*|"
    r"[0-9０-９]+(?:\.[0-9０-９]+)*\s+\S.*)$")


def make_section_id(heading_path: str) -> str:
    """見出しパスから **再現可能な安定 section_id** を作る（再構築で変わらない）。"""
    return "s" + hashlib.md5((heading_path or "(本文)").encode("utf-8")).hexdigest()[:8]


def make_collection_id(folder: str | Path) -> str:
    """フォルダ取り込み単位の安定 ID（フォルダ絶対パスから決定的に導出）。"""
    return "c" + hashlib.md5(str(Path(folder).resolve()).encode("utf-8")).hexdigest()[:10]


def normalize_tags(tags) -> list[str]:
    """タグの正規化: 前後空白を除去し、空文字を捨て、順序維持で重複除去する。"""
    if not tags:
        return []
    return list(dict.fromkeys(t for t in (str(x).strip() for x in tags) if t))


@dataclass
class SearchHit:
    """検索結果1件（1文書分。チャンクは文書内に束ねる）。"""

    doc_id: str
    title: str
    score: float
    source_path: str | None = None
    used_graph: bool = False
    fallback_reason: str | None = None   # graph 要求だが未構築→通常RAG等
    # chunks: {text, page, score, source, section_id, section(見出しパス), chunk_id}
    chunks: list[dict] = field(default_factory=list)

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

    def __init__(self, storage_dir: str | Path = DEFAULT_STORAGE, *,
                 graph_max_workers: int | None = None,
                 graph_max_nodes: int | None = None,
                 graph_chunk_chars: int | None = None,
                 er_use_llm: bool | None = None):
        self.root = Path(storage_dir)
        self.docs_dir = self.root / "docs"
        self.chunks_dir = self.root / "chunks"
        self.status_dir = self.root / "status"
        self.bookindex_dir = self.root / "bookindex"
        self.collections_dir = self.root / "collections"
        self._memberships_path = self.root / "memberships.json"
        # graph（Entity/Relation 抽出）の設定。ローカルLLM向け安全値が既定
        self.graph_settings = dict(SAFE_GRAPH_DEFAULTS)
        for k, v in [("graph_max_workers", graph_max_workers),
                     ("graph_max_nodes", graph_max_nodes),
                     ("graph_chunk_chars", graph_chunk_chars),
                     ("er_use_llm", er_use_llm)]:
            if v is not None:
                self.graph_settings[k] = v
        # fast/hierarchy 用の共有ベクトル索引。チャンクJSONは chunks/ に書き出す。
        self._paged = PagedRAG(storage_dir=str(self.root / "vectors"),
                               documents_dir=str(self.chunks_dir))

    # ---- 取り込み ----------------------------------------------------------

    def add_document(self, path: str | Path, *, title: str | None = None,
                     index_mode: str = "fast", force: bool = False,
                     layout=False, ocr=False, progress=None,
                     collection_id: str | None = None,
                     relative_path: str | None = None,
                     tags: list[str] | None = None,
                     graph_settings: dict | None = None) -> dict:
        """文書を index_mode で取り込む。既定は fast（高速・通常RAG）。

        - graph 未指定なら Entity/Relation 抽出は走らない（重い処理は明示時だけ）。
        - 同じ doc_id かつ同じ content_hash が ready なら、force でない限り再抽出せず
          skipped で返す（キャッシュ/差分更新）。
        - **状態は工程別**（vector_status / hierarchy_status / graph_status）。
          graph だけ失敗しても vector/hierarchy 検索は使える（status は ready のまま、
          graph_status=failed + graph_error に記録）。
        - collection_id / relative_path / tags でフォルダ取り込みとの所属を記録する
          （既存の索引に無くても後方互換で動く）。
        - graph_settings: {"graph_max_workers","graph_max_nodes","graph_chunk_chars",
          "er_use_llm"} の上書き（省略時はコンストラクタ設定＝安全既定）。
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
            # 変更なしキャッシュ。ただし所在（collection/ファイル）は新規なら追記する
            #（同一内容が複数フォルダにあるケースで所属を失わない）
            self._add_membership(doc_id, collection_id, relative_path, tags,
                                 source_path=resolved)
            prev = self._read(self.docs_dir, doc_id) or prev
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

        # 同じ元ファイルの旧版（別 doc_id）が登録済みなら「このファイルの分だけ」
        # 置き換える。内容は複数フォルダで共有され得る（同一内容=同一 doc_id）ため、
        # 旧 doc_id をグローバル削除すると別フォルダに残っている旧文書まで消える。
        # → このファイル（source_path）を指す membership 行だけを旧 doc_id から外し、
        #   他の所在が残っていれば旧文書の索引・チャンク・メタは保持する。
        #   所在が0件になった場合のみ旧文書を削除する。
        inherited = {"collection_ids": [], "tags": []}
        for old in self.documents():
            if old["doc_id"] == doc_id:
                continue
            moved = self._detach_location(old, resolved)
            if moved is None:
                continue
            inherited["collection_ids"] += moved["collection_ids"]
            inherited["tags"] += moved["tags"]
            if moved["orphaned"]:
                _log(f"旧版 {old['doc_id']} を置き換えます（内容が変更されたため）")
                print(f"[IndexManager] {path.name}: 旧版（doc_id={old['doc_id']}）を"
                      "削除して新しい内容で登録します")
                self.delete(old["doc_id"])
            else:
                _log(f"旧版 {old['doc_id']} は他のフォルダに所在が残るため保持し、"
                     f"このファイルの所属だけを新しい内容へ移します")
                print(f"[IndexManager] {path.name}: 旧版（doc_id={old['doc_id']}）は"
                      "他の所在から参照されているため索引を保持します")

        self._set_status(doc_id, "running", index_mode)
        meta = {
            "doc_id": doc_id, "title": title, "source_path": resolved,
            "content_hash": chash, "index_mode": index_mode, "status": "running",
            "chunk_count": 0, "created_at": created, "updated_at": _now(),
            "graph_index": False, "layout": bool(layout), "error": None,
            # 工程別ステータス（graph だけの失敗で文書全体を failed にしない）。
            # "none" = そのモードでは対象外（GUI は非表示にする）
            "vector_status": "pending", "hierarchy_status": "none",
            "graph_status": "none", "graph_error": None,
            # collection / タグ（後方互換: 旧メタには無くてもよい）
            "collection_ids": list(dict.fromkeys(
                (prev or {}).get("collection_ids", []) + inherited["collection_ids"])),
            "relative_path": relative_path or (prev or {}).get("relative_path"),
            "tags": normalize_tags((prev or {}).get("tags", []) + list(tags or [])
                                   + inherited["tags"]),
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
            meta["vector_status"] = "running"
            if (not same_content) or (not have_chunks) or same_mode_force:
                _log("チャンク化＋埋め込み")
                with self._forward_logs(progress):
                    # 所在（membership）はこちらで管理するため、同一パスの旧 doc_id を
                    # PagedRAG 側で消させない（共有内容の旧版が別フォルダに残り得る）
                    self._paged.add_book(path, title=title, doc_id=doc_id, force=True,
                                         replace_same_path=False)
            else:
                _log("チャンク/埋め込みは変更なしのため再利用")
            self._assign_sections(doc_id)   # 安定 section_id / 見出しパスを付与
            chunks = self._paged.document(doc_id) or {}
            meta["chunk_count"] = len(chunks.get("chunks", []))
            meta["vector_status"] = "ready"

            # 2) hierarchy / graph: 文書ごとの BookRAG 索引。
            #    fast では作らず、既存の木/KG が残っていれば消す（詳細表示との矛盾防止）。
            book_dir = self.bookindex_dir / doc_id
            if index_mode in ("hierarchy", "graph"):
                gs = {**self.graph_settings, **(graph_settings or {})}
                build_graph = index_mode == "graph"
                meta["hierarchy_status"] = "running"
                if build_graph:
                    meta["graph_status"] = "running"
                self._write(self.docs_dir, doc_id, meta)
                _log("セクション木を構築" + ("＋Entity/Relation抽出（graph・低速）"
                                          if build_graph else "（hierarchy）"))
                try:
                    self._build_book_layer(path, doc_id, title, build_graph,
                                           layout, ocr, gs, progress,
                                           fresh=not (force and build_graph
                                                      and (book_dir / "graph_progress.json").exists()))
                    meta["hierarchy_status"] = "ready"
                    if build_graph:
                        from .bookrag import BookRAG

                        meta["graph_index"] = BookRAG(storage_dir=str(book_dir)).has_graph()
                        meta["graph_status"] = "ready" if meta["graph_index"] else "failed"
                        if not meta["graph_index"]:
                            meta["graph_error"] = ("エンティティを1件も抽出できませんでした"
                                                   "（モデルのJSON応答/思考出力を確認）")
                except Exception as ge:  # noqa: BLE001
                    # graph/hierarchy の失敗で文書全体を失敗にしない。
                    # ベクトル索引は完成しているので通常検索は利用可能。
                    err = f"{type(ge).__name__}: {ge}"
                    if meta["hierarchy_status"] == "running":
                        meta["hierarchy_status"] = "failed"
                    if build_graph:
                        meta["graph_status"] = "failed"
                    meta["graph_error"] = err
                    print(f"[IndexManager] {path.name}: 木/グラフ構築に失敗"
                          f"（通常検索は利用可能）: {err}")
            else:
                shutil.rmtree(book_dir, ignore_errors=True)

            if collection_id:
                meta["collection_ids"] = list(dict.fromkeys(
                    meta["collection_ids"] + [collection_id]))
            # 明示タグは collection の有無に関係なく保存する（単一ファイル追加でも
            # GUI のタグが消えない）。既存タグには追記し、正規化して重複を除く。
            if tags:
                meta["tags"] = normalize_tags(meta["tags"] + list(tags))
            meta.update(status="ready", updated_at=_now())
            self._write(self.docs_dir, doc_id, meta)
            self._add_membership(doc_id, collection_id, relative_path, tags,
                                 source_path=resolved)
            self._set_status(doc_id, "ready", index_mode,
                             error=meta.get("graph_error"),
                             note=("グラフのみ失敗" if meta.get("graph_status") == "failed"
                                   else None))
            return meta
        except Exception as e:  # noqa: BLE001  vector 失敗＝文書失敗。記録して再送出
            meta.update(status="failed", vector_status="failed",
                        error=f"{type(e).__name__}: {e}", updated_at=_now())
            self._write(self.docs_dir, doc_id, meta)
            self._set_status(doc_id, "failed", index_mode, error=meta["error"])
            raise

    def _build_book_layer(self, path, doc_id, title, build_graph, layout, ocr,
                          gs: dict, progress, *, fresh: bool = True) -> None:
        """hierarchy/graph 層（BookRAG）を安全設定で構築する。

        fresh=False なら既存の graph_progress.json（チェックポイント）だけを残して
        木を作り直す。パースは決定的なのでノード署名が一致すれば抽出済み結果が
        そのまま再利用される（署名が変わっていればチェックポイントは安全に破棄）。
        fresh=True はチェックポイントごと作り直し。
        """
        from .bookrag import BookRAG

        book_dir = self.bookindex_dir / doc_id
        if book_dir.exists():
            if fresh:
                shutil.rmtree(book_dir)
            else:
                # 旧い木が残ったまま add_book すると木が重複するため、
                # チェックポイント以外を消してから作り直す。
                for p in book_dir.iterdir():
                    if p.name == "graph_progress.json":
                        continue
                    if p.is_dir():
                        shutil.rmtree(p)
                    else:
                        p.unlink()
        book = BookRAG(storage_dir=str(book_dir),
                       max_workers=int(gs.get("graph_max_workers", 1)),
                       max_nodes=int(gs.get("graph_max_nodes", 100)),
                       chunk_chars=int(gs.get("graph_chunk_chars", 2000)),
                       er_use_llm=bool(gs.get("er_use_llm", False)))
        with self._forward_logs(progress):
            book.add_book(path, title=title, doc_id=doc_id, force=True,
                          build_graph=build_graph, layout=layout, ocr=ocr,
                          graph_checkpoint=True)

    def build_graph_only(self, doc_id: str, *, resume: bool = True,
                         graph_settings: dict | None = None, progress=None) -> dict:
        """graph 層だけを（再）構築する。resume=True でチェックポイントから再開。

        vector/hierarchy はそのまま。graph の失敗も文書全体を failed にしない。
        """
        meta = self._read(self.docs_dir, doc_id)
        if not meta:
            raise KeyError(f"未登録の doc_id: {doc_id}")
        src = meta.get("source_path")
        if not src or not Path(src).exists():
            raise FileNotFoundError(f"元ファイルが見つかりません: {src}")
        gs = {**self.graph_settings, **(graph_settings or {})}
        book_dir = self.bookindex_dir / doc_id
        has_ckpt = (book_dir / "graph_progress.json").exists()
        meta.update(graph_status="running", graph_error=None, updated_at=_now())
        self._write(self.docs_dir, doc_id, meta)
        try:
            self._build_book_layer(Path(src), doc_id, meta.get("title"),
                                   True, meta.get("layout", False), False,
                                   gs, progress, fresh=not (resume and has_ckpt))
            from .bookrag import BookRAG

            ok = BookRAG(storage_dir=str(book_dir)).has_graph()
            meta.update(index_mode="graph", graph_index=ok,
                        hierarchy_status="ready",
                        graph_status="ready" if ok else "failed",
                        graph_error=None if ok else "エンティティ0件", updated_at=_now())
            self._write(self.docs_dir, doc_id, meta)
            return meta
        except Exception as e:  # noqa: BLE001
            meta.update(graph_status="failed",
                        graph_error=f"{type(e).__name__}: {e}", updated_at=_now())
            self._write(self.docs_dir, doc_id, meta)
            raise

    def _assign_sections(self, doc_id: str) -> None:
        """チャンクJSONに安定 section_id と見出しパスを付与する（fast 層）。

        見出し（Markdown #、第N章/節/条、番号見出し）をチャンク本文から検出し、
        直近の見出しを引き継ぐ。section_id は見出しパスのハッシュなので
        再構築しても変わらない。見出しの無い文書は "(本文)" 1セクション。
        """
        doc = self._paged.document(doc_id)
        if not doc:
            return
        current = "(本文)"
        changed = False
        for c in doc.get("chunks", []):
            for line in (c.get("text") or "").splitlines():
                s = line.strip()
                if s and len(s) <= 80 and _HEADING_RE.match(s):
                    current = s.lstrip("#").strip()
                    break  # チャンク先頭側の見出しを採用
            if c.get("section") != current:
                changed = True
            c["section"] = current
            c["section_id"] = make_section_id(current)
        if changed or doc.get("chunks"):
            self._write(self.chunks_dir, doc_id, doc)

    def add_folder(self, docs_dir: str | Path, *, index_mode: str = "fast",
                   force: bool = False, layout=False, ocr=False,
                   progress=None, recursive: bool = False,
                   collection_name: str | None = None,
                   tags: list[str] | None = None,
                   graph_settings: dict | None = None) -> dict:
        """フォルダ内の対応文書を **1ファイル=1文書** として順に取り込む。

        - フォルダ全体を1つの文書に結合しない。各ファイルは個別の doc_id を持つ
          独立文書になる（検索は文書ごとに chunk top-k → doc_id 単位で集約）。
        - フォルダは **collection** として登録される（collection_id は絶対パス由来の
          安定ID）。各文書に collection_id / relative_path / 自動タグ（フォルダ名、
          recursive 時はサブフォルダ名も）を関連付ける。
        - 同一内容のファイルが複数フォルダにあっても doc_id は重複登録せず、
          両方の collection への所属関係を記録する。
        - recursive=False（既定）はフォルダ直下のみ。True でサブフォルダも走査。
        - hierarchy/graph 指定時、木/KG を作れない形式（csv/html 等）は
          そのファイルだけ fast に自動降格して取り込む（スキップしない）。
        - 1ファイルの失敗で全体を止めない（status=failed に記録して続行）。
        - 返り値: {"results": [meta...], "added", "skipped", "failed", "errors",
          "collection_id"}
        """
        docs_dir = Path(docs_dir)
        if not docs_dir.is_dir():
            raise NotADirectoryError(f"フォルダではありません: {docs_dir}")
        it = docs_dir.rglob("*") if recursive else docs_dir.iterdir()
        files = sorted(f for f in it
                       if f.is_file() and f.suffix.lower() in FOLDER_EXTS)
        if not files:
            raise FileNotFoundError(
                f"{docs_dir} に対応文書がありません"
                f"（対応: {', '.join(sorted(FOLDER_EXTS))}）")

        # collection を登録（既存なら updated_at 更新・名前/タグはマージ）
        collection_id = make_collection_id(docs_dir)
        self._upsert_collection(collection_id, collection_name or docs_dir.name,
                                str(docs_dir.resolve()), tags or [],
                                recursive=recursive)
        base_tags = list(dict.fromkeys([docs_dir.name] + (tags or [])))

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
            rel = str(f.relative_to(docs_dir))
            # 自動タグ: フォルダ名 + （recursive時）サブフォルダ名
            ftags = base_tags + [p for p in Path(rel).parts[:-1] if p]
            try:
                meta = self.add_document(f, index_mode=eff_mode, force=force,
                                         layout=layout, ocr=ocr, progress=_inner,
                                         collection_id=collection_id,
                                         relative_path=rel, tags=ftags,
                                         graph_settings=graph_settings)
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
                "failed": failed, "errors": errors, "collection_id": collection_id}

    # ---- collection / タグ --------------------------------------------------

    def _upsert_collection(self, cid: str, name: str, folder: str,
                           tags: list[str], *, recursive: bool = False) -> None:
        cur = self._read(self.collections_dir, cid) or {}
        self._write(self.collections_dir, cid, {
            "collection_id": cid,
            "collection_name": name or cur.get("collection_name"),
            "folder": folder,
            "tags": list(dict.fromkeys(cur.get("tags", []) + (tags or []))),
            "recursive": bool(recursive),
            "created_at": cur.get("created_at") or _now(),
            "updated_at": _now(),
        })

    def _memberships(self) -> dict:
        """memberships.json: {doc_id: [{collection_id, relative_path}]}"""
        from .workspace import _read_json_file

        m = _read_json_file(self._memberships_path, {})
        return m if isinstance(m, dict) else {}

    def _add_membership(self, doc_id: str, collection_id: str | None,
                        relative_path: str | None, tags: list[str] | None,
                        source_path: str | None = None) -> None:
        """文書の「所在」を1行追記する（doc メタ側にも反映）。

        行は {collection_id, relative_path, source_path, tags}。
        - collection_id=None は単独登録（フォルダ外）の所在記録。
        - 同じ collection × 同じ所在の行は追記せず更新する
          （source_path の無い旧形式行はここで補完される）。
        文書内容（doc_id）とファイルの所在を分離して持つことで、
        共有内容の一方のファイルだけが改訂されたとき、その所在だけを
        新しい doc_id へ移せる。
        """
        tags = normalize_tags(tags)
        m = self._memberships()
        rows = m.setdefault(doc_id, [])
        for r in rows:
            if r.get("collection_id") != collection_id:
                continue
            same_loc = (source_path and r.get("source_path") == source_path) \
                or (not r.get("source_path")
                    and r.get("relative_path") == relative_path)
            if same_loc:
                if source_path:
                    r["source_path"] = source_path   # 旧形式行の補完
                if relative_path:
                    r["relative_path"] = relative_path
                if tags:
                    r["tags"] = normalize_tags((r.get("tags") or []) + tags)
                break
        else:
            rows.append({"collection_id": collection_id,
                         "relative_path": relative_path,
                         "source_path": source_path,
                         "tags": tags})
        from .workspace import _write_json_file

        _write_json_file(self._memberships_path, m)
        meta = self._read(self.docs_dir, doc_id)
        if meta is not None:
            if collection_id:
                meta["collection_ids"] = list(dict.fromkeys(
                    meta.get("collection_ids", []) + [collection_id]))
            if tags:
                meta["tags"] = normalize_tags(meta.get("tags", []) + tags)
            self._write(self.docs_dir, doc_id, meta)

    def _detach_location(self, old_meta: dict, resolved: str) -> dict | None:
        """旧文書 old_meta から「resolved にあるファイルの所在」だけを外す。

        戻り値: None = この文書は resolved と無関係。
        dict = {"collection_ids": 移った所属, "tags": 移ったタグ,
                "orphaned": 所在が0件になった（=削除してよい）}
        所在が残る場合は旧文書の索引・チャンク・メタを保持し、メタの
        collection_ids / source_path を残存所在から再計算する。
        """
        old_id = old_meta["doc_id"]
        m = self._memberships()
        rows = m.get(old_id, [])
        matching = [r for r in rows if r.get("source_path") == resolved]
        legacy = [r for r in rows if not r.get("source_path")]   # 旧形式（所在不明）
        others = [r for r in rows
                  if r.get("source_path") and r.get("source_path") != resolved]

        if not matching and old_meta.get("source_path") != resolved:
            return None
        if not others and not matching:
            # 旧形式のみ（または行なし）: 所在を切り分けられないため従来どおり全置換
            matching, legacy = legacy, []
        remaining = others + legacy
        orphaned = not remaining

        moved_cids = [r.get("collection_id") for r in matching
                      if r.get("collection_id")]
        moved_tags: list[str] = []
        for r in matching:
            moved_tags += r.get("tags") or []
        if orphaned:
            # 完全置換なら doc レベルの所属・タグも従来どおり全部引き継ぐ
            moved_cids += old_meta.get("collection_ids", [])
            moved_tags += old_meta.get("tags", []) or []

        from .workspace import _write_json_file

        if orphaned:
            m.pop(old_id, None)
        else:
            m[old_id] = remaining
        _write_json_file(self._memberships_path, m)

        if not orphaned:
            # 残存所在からメタを再計算（改訂されたファイルを指したままにしない）
            meta = self._read(self.docs_dir, old_id)
            if meta is not None:
                meta["collection_ids"] = list(dict.fromkeys(
                    r.get("collection_id") for r in remaining
                    if r.get("collection_id")))
                alive = [r.get("source_path") for r in remaining
                         if r.get("source_path")]
                if meta.get("source_path") == resolved and alive:
                    meta["source_path"] = alive[0]
                meta["updated_at"] = _now()
                self._write(self.docs_dir, old_id, meta)
        return {"collection_ids": moved_cids,
                "tags": normalize_tags(moved_tags), "orphaned": orphaned}

    def collections(self) -> list[dict]:
        """登録済み collection の一覧（文書数つき・更新日時の新しい順）。"""
        out = []
        if self.collections_dir.exists():
            counts: dict[str, int] = {}
            for _did, rows in self._memberships().items():
                for r in rows:
                    cid = r.get("collection_id")
                    counts[cid] = counts.get(cid, 0) + 1
            for p in self.collections_dir.glob("*.json"):
                c = self._read_path(p)
                if c:
                    c["doc_count"] = counts.get(c.get("collection_id"), 0)
                    out.append(c)
        out.sort(key=lambda c: c.get("updated_at", ""), reverse=True)
        return out

    def all_tags(self) -> list[str]:
        """全文書のタグ一覧（重複除去・出現順）。"""
        seen = {}
        for m in self.documents():
            for t in m.get("tags", []) or []:
                seen.setdefault(t, None)
        return list(seen)

    def set_tags(self, doc_id: str, tags: list[str]) -> dict:
        """文書のタグを置き換える（GUI の手動タグ編集用）。"""
        meta = self._read(self.docs_dir, doc_id)
        if not meta:
            raise KeyError(f"未登録の doc_id: {doc_id}")
        meta["tags"] = normalize_tags(tags)
        meta["updated_at"] = _now()
        self._write(self.docs_dir, doc_id, meta)
        return meta

    def _scope_doc_ids(self, collection_ids=None, tags=None) -> set[str] | None:
        """collection / タグによる検索範囲（doc_id 集合）。None = 制限なし。

        条件の意味（GUI の件数表示と同一仕様に統一）:
        - 複数 collection: **OR**（いずれかに所属していれば対象）
        - 複数タグ: **AND**（選択したタグをすべて持つ文書だけが対象）
        - collection 条件とタグ条件の組み合わせ: **AND**

        範囲は **候補文書選定の前** に適用する（global top-k 後のフィルタではない）。
        collection 情報の無い旧索引では memberships が空 → collection_ids 指定時は
        空集合（該当なし）になるが、未指定なら全文書が対象（後方互換）。
        """
        if not collection_ids and not tags:
            return None
        allowed: set[str] | None = None
        if collection_ids:
            want = set(collection_ids)
            hit = {did for did, rows in self._memberships().items()
                   if any(r.get("collection_id") in want for r in rows)}
            # メタ側の collection_ids も見る（memberships 消失時の保険）
            for m in self.documents():
                if want & set(m.get("collection_ids", []) or []):
                    hit.add(m["doc_id"])
            allowed = hit
        if tags:
            want_t = set(tags)
            hit_t = {m["doc_id"] for m in self.documents()
                     if want_t <= set(m.get("tags", []) or [])}   # AND: 全タグを持つ
            allowed = hit_t if allowed is None else (allowed & hit_t)
        return allowed

    def rebuild(self, doc_id: str, *, index_mode: str | None = None,
                graph_settings: dict | None = None, progress=None) -> dict:
        """文書を（必要なら別モードで）作り直す（force=True 相当）。

        index_mode 省略時は **現在のモードを維持** する（fast に降格しない）。
        graph_settings（並列数・ノード上限など）は GUI のプリセットから渡される。
        """
        meta = self._read(self.docs_dir, doc_id)
        if not meta:
            raise KeyError(f"未登録の doc_id: {doc_id}")
        src = self._pick_source_path(doc_id, meta)
        if not src:
            raise FileNotFoundError(
                f"元ファイルが見つかりません: {meta.get('source_path')}"
                "（membership の所在にも存在するファイルがありません）")
        return self.add_document(src, title=meta.get("title"),
                                 index_mode=index_mode or meta.get("index_mode", "fast"),
                                 layout=meta.get("layout", False),  # 見出し判定設定を維持
                                 force=True, graph_settings=graph_settings,
                                 progress=progress)

    def _pick_source_path(self, doc_id: str, meta: dict) -> str | None:
        """再構築に使える元ファイルを選ぶ。

        meta.source_path が消えていても、membership に残る所在（別フォルダの
        同一内容ファイル）から存在するものを選択する。見つからなければ None。
        """
        candidates = [meta.get("source_path")]
        candidates += [r.get("source_path")
                       for r in self._memberships().get(doc_id, [])]
        for c in candidates:
            if c and Path(c).exists():
                return c
        return None

    def delete(self, doc_id: str, *, only_if_orphan: bool = False) -> bool:
        """文書を全ストア（索引・チャンク・木/KG・メタ・status）から削除する。

        only_if_orphan=True なら、所在（membership）が残っている文書は削除しない
        （不要データ＝どのフォルダ/ファイルからも参照されない文書だけを掃除する用途）。
        既定の delete は利用者の明示操作なので所在ごと削除する。
        """
        if only_if_orphan and self._memberships().get(doc_id):
            return False
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
        m = self._memberships()
        if doc_id in m:  # 所属関係も掃除
            from .workspace import _write_json_file

            m.pop(doc_id)
            _write_json_file(self._memberships_path, m)
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

    # ---- 検索（範囲限定 → 文書候補選定 → 文書内チャンク → リランク） --------

    def search(self, question: str, *, document_top_n: int = DEFAULT_DOC_TOP_N,
               chunk_top_k_per_doc: int = 4, max_chunks_per_doc: int | None = None,
               use_graph: bool = False, doc_ids: list[str] | None = None,
               collection_ids: list[str] | None = None,
               tags: list[str] | None = None) -> list[SearchHit]:
        """文書間検索。構造:
        collection/tag で範囲限定 → 文書候補選定 → 文書内チャンク候補 → リランク。

        - **doc_ids は利用者が明示選択した検索対象そのもの**。指定時は選択された
          全文書を対象にし、document_top_n では切り捨てない（順序維持で重複除去）。
        - document_top_n は doc_ids **未指定時の自動文書選定にだけ** 適用する。
        - collection_ids / tags は自動選定の**候補範囲**（範囲内で top-N を適用）。
          範囲フィルタは候補選定の前に効く（global top-k 後のフィルタではない）。
        - use_graph=True で graph 索引がある文書は BookRAG 検索、無ければ通常RAG
          へフォールバック（落とさない。fallback_reason に理由を記録）。
        - チャンクは広めに候補を取り、ベクトル+キーワードのハイブリッドで
          リランキングして chunk_top_k_per_doc 件を採用する。
        """
        from types import SimpleNamespace

        cap = max_chunks_per_doc or chunk_top_k_per_doc
        if doc_ids is not None:
            # 明示選択: 全文書を対象（重複は順序維持で除去。top_n で切らない）
            selected_ids = list(dict.fromkeys(str(d) for d in doc_ids if d))
            ranked = []
            for did in selected_ids:
                nodes = self._doc_chunk_candidates(question, did, chunk_top_k_per_doc)
                score = max((s for _n, s in nodes), default=0.0)
                meta = self._read(self.docs_dir, did) or {}
                ranked.append(SimpleNamespace(doc_id=did, score=score,
                                              title=meta.get("title", did),
                                              _cands=nodes))
            ranked.sort(key=lambda r: r.score, reverse=True)
            # 明示選択時は document_top_n で切り捨てない（全件が対象）
        else:
            scope = self._scope_doc_ids(collection_ids, tags)
            ranked = self._rank_documents_scoped(question, scope, document_top_n,
                                                 chunk_top_k_per_doc)

        hits: list[SearchHit] = []
        for r in ranked:
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
                cands = getattr(r, "_cands", None)
                self._normal_chunks(r.doc_id, question, hit, chunk_top_k_per_doc,
                                    cap, candidates=cands)
            hit.used_graph = used_graph
            hits.append(hit)
        return hits

    def _rank_documents_scoped(self, question: str, scope: set[str] | None,
                               top_n: int, chunk_top_k: int):
        """自動文書選定（scope=None なら全文書が候補）。

        - 広めのチャンク候補（cand_k）を取り doc_id で集約。cand_k は top_n に
          比例して増やし、チャンク数の多い1文書が候補を占有しにくくする。
        - scope 指定時は **候補選定の前に** 範囲外を除外し、範囲内の文書が
          広域候補に現れなかった場合は個別検索で補完する（少数派文書の脱落防止）。
        """
        from types import SimpleNamespace

        cand_k = max(top_n * chunk_top_k * 8, 80)
        ranked = self._paged.rank_documents(
            question, candidate_chunk_k=cand_k,
            top_n=top_n if scope is None else max(top_n, len(scope)),
            chunks_per_doc=chunk_top_k)
        if scope is None:
            return ranked[:top_n]
        in_scope = [r for r in ranked if r.doc_id in scope]
        found = {r.doc_id for r in in_scope}
        # 範囲内なのに広域候補に出てこなかった文書を個別に当たって補完
        missing = [d for d in scope if d not in found]
        extras = []
        for did in missing:
            nodes = self._doc_chunk_candidates(question, did, chunk_top_k)
            if nodes:
                score = max(s for _n, s in nodes)
                meta = self._read(self.docs_dir, did) or {}
                extras.append(SimpleNamespace(doc_id=did, score=score,
                                              title=meta.get("title", did),
                                              _cands=nodes))
        merged = sorted(in_scope + extras, key=lambda r: r.score, reverse=True)
        return merged[:top_n]   # 範囲内で top-N（範囲外は混入しない）

    def _doc_chunk_candidates(self, question: str, doc_id: str,
                              top_k: int) -> list[tuple]:
        """1文書内のチャンク候補を広めに取り、ハイブリッドでリランクして返す。

        候補は max(2*top_k, DEFAULT_CHUNK_CANDIDATES) 件取得 → ベクトルスコアに
        キーワード一致ボーナス（数値・型番・規程番号などの取りこぼし対策）を加えて
        並べ替える。返り値: [(NodeWithScore, hybrid_score), ...]
        """
        n_cand = max(2 * top_k, DEFAULT_CHUNK_CANDIDATES)
        nodes = self._paged.retrieve_in_doc(question, doc_id=doc_id, top_m=n_cand)
        terms = _keyword_terms(question)
        out = []
        for n in nodes:
            vec = getattr(n, "score", 0.0) or 0.0
            text = n.node.get_content()
            out.append((n, vec + _keyword_bonus(terms, text)))
        out.sort(key=lambda t: t[1], reverse=True)
        return out

    # ---- 回答生成・要約（検索 + LLM 合成） ----------------------------------

    def ask(self, question: str, *, document_top_n: int = DEFAULT_DOC_TOP_N,
            chunk_top_k_per_doc: int = 4, max_chunks_per_doc: int | None = None,
            use_graph: bool = False, doc_ids: list[str] | None = None,
            collection_ids: list[str] | None = None, tags: list[str] | None = None,
            progress=None) -> DocAnswer:
        """質問に **回答を生成** する（検索 → 文書別の根拠を文脈に LLM で合成）。

        - doc_ids 明示選択時は **選択した全文書** が根拠に含まれる（切り捨てない）。
        - 対象文書が多い（MAP_REDUCE_DOC_THRESHOLD 以上）場合は、全チャンクを
          単一プロンプトに入れず **文書ごとに部分回答 → 統合** の Map-Reduce で処理
          する。どの文書も黙って比較対象から消えない。
        - 単一プロンプト時も CONTEXT_CHAR_BUDGET の予算内で文書ごとに均等配分する。
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
                           use_graph=use_graph, doc_ids=doc_ids,
                           collection_ids=collection_ids, tags=tags)
        if not hits or not any(h.chunks for h in hits):
            return DocAnswer(text="該当する文書が見つかりませんでした。"
                                  "文書が登録済みか、質問の言い換えを確認してください。",
                             hits=hits)

        sys_prompt = ("あなたは文書アシスタントです。以下の抜粋のみに基づいて依頼に日本語で"
                      "応えてください（回答・要約・比較など依頼の種類に従う）。"
                      "どの文書の情報かを文書名で明示し、抜粋に無い内容は推測しないでください。")

        def _doc_block(h: SearchHit, budget: int) -> str:
            per_chunk = max(200, budget // max(1, len(h.chunks)))
            lines = []
            for c in h.chunks:
                loc = "".join([f"§{c['section']} " if c.get("section")
                               and c["section"] != "(本文)" else "",
                               f"p.{c['page']} " if c.get("page") else ""])
                lines.append(f"- {loc}{c['text'][:per_chunk]}")
            return f"### 文書「{h.title}」\n" + "\n".join(lines)

        # Map-Reduce: 文書数が多いときは文書ごとに部分回答してから統合する
        if len(hits) >= MAP_REDUCE_DOC_THRESHOLD:
            partials = []
            total = len(hits) + 1
            for i, h in enumerate(hits):
                emit(f"文書別に回答: {h.title}", i, total)
                try:
                    p = bx.llm_text(
                        f"依頼: {question}\n\n{_doc_block(h, CONTEXT_CHAR_BUDGET)}\n\n"
                        "この文書の抜粋から依頼に関係する内容だけを簡潔に述べてください。"
                        "無ければ「該当情報なし」と答えてください。",
                        system=sys_prompt).strip()
                except Exception as e:  # noqa: BLE001  1文書の失敗で全体を止めない
                    p = f"（この文書の処理に失敗: {type(e).__name__}: {e}）"
                partials.append(f"■ {h.title}\n{p}")
            emit("統合回答を生成", len(hits), total)
            text = bx.llm_text(
                f"依頼: {question}\n\n以下は文書ごとの部分回答です。突き合わせて"
                f"依頼に答えてください。全文書に言及してください。\n\n"
                + "\n\n".join(partials),
                system=sys_prompt).strip()
            emit("完了", total, total)
            return DocAnswer(text=text, hits=hits,
                             per_doc=[{"doc_id": h.doc_id, "title": h.title,
                                       "text": p.split("\n", 1)[-1]}
                                      for h, p in zip(hits, partials)])

        # 単一プロンプト: 文書ごとに予算を均等配分（どの文書も落とさない）
        per_doc_budget = CONTEXT_CHAR_BUDGET // len(hits)
        ctx = "\n\n".join(_doc_block(h, per_doc_budget) for h in hits)
        emit("回答を生成", 1, 2)
        text = bx.llm_text(f"依頼: {question}\n\n文書からの抜粋:\n{ctx}",
                           system=sys_prompt).strip()
        emit("完了", 2, 2)
        return DocAnswer(text=text, hits=hits)

    def summarize(self, instruction: str | None = None, *,
                  doc_ids: list[str] | None = None,
                  collection_ids: list[str] | None = None,
                  tags: list[str] | None = None, chunks_per_doc: int = 6,
                  progress=None) -> DocAnswer:
        """登録文書を **要約** する（文書ごとに部分要約 → 統合要約の Map-Reduce）。

        - doc_ids 省略時は登録済みの全文書が対象（文書ごとに1回 LLM を呼ぶため、
          ローカルLLMでは文書数に比例して時間がかかる）。
        - collection_ids / tags で対象を絞れる（doc_ids 未指定時のスコープ）。
        - instruction で観点を指定できる（例: 「リスク面を中心に」）。
        - 検索ベースではなく各文書のチャンクを頭から均等に読むため、
          「全体を要約」のような特定トピックの無い依頼に強い。
        """
        from . import bookindex as bx

        scope = None
        if doc_ids is None and (collection_ids or tags):
            scope = self._scope_doc_ids(collection_ids, tags)
        metas = [m for m in self.documents()
                 if ((not doc_ids) or m["doc_id"] in set(doc_ids))
                 and (scope is None or m["doc_id"] in scope)]
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

    def _normal_chunks(self, doc_id, question, hit, top_k, cap,
                       candidates=None) -> None:
        """文書内チャンクの採用: 候補（広め・ハイブリッド済み）から上位 cap 件。

        section_id / 見出しパスは chunks JSON（_assign_sections で付与）から引く。
        """
        cands = candidates if candidates is not None \
            else self._doc_chunk_candidates(question, doc_id, top_k)
        sec_by_chunk = {}
        doc = self._paged.document(doc_id) or {}
        for c in doc.get("chunks", []):
            sec_by_chunk[c.get("chunk_id")] = (c.get("section_id"), c.get("section"))
        for n, score in cands[:min(cap, len(cands))]:
            meta = n.node.metadata
            sid, sec = sec_by_chunk.get(n.node.node_id, (None, None))
            hit.chunks.append({
                "text": n.node.get_content().strip(),
                "page": meta.get("page_label"),
                "score": round(score, 4),
                # 出典は文書メタの現在の所在を優先する。チャンク metadata の path は
                # 取り込み時のもので、共有内容の一方が改訂されると旧文書のチャンクが
                # 「改訂後のファイル」を指してしまう（membership 分離の整合）。
                "source": hit.source_path or meta.get("path") or meta.get("source"),
                "chunk_id": n.node.node_id,
                "section_id": sid,
                "section": sec,
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
        あわせて bx.progress_to で progress() の (desc, current, total) も転送し、
        受信側（Studio）がフェーズごとの ETA を計算できるようにする。
        """
        from contextlib import ExitStack, nullcontext

        from . import bookindex as bx

        if progress is None:
            return nullcontext()

        def _fwd(msg):
            try:
                progress({"stage": str(msg), "current": 0, "total": 1, "detail": ""})
            except Exception:  # noqa: BLE001
                pass

        def _fwd_prog(desc, current, total):
            try:
                progress({"stage": str(desc), "current": int(current),
                          "total": int(total or 0), "detail": ""})
            except Exception:  # noqa: BLE001
                pass

        stack = ExitStack()
        stack.enter_context(bx.log_to(_fwd))
        stack.enter_context(bx.progress_to(_fwd_prog))
        return stack

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
