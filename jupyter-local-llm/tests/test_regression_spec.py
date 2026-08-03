"""大型仕様（doc_ids 全件・コレクション/タグ・top-k再設計・グラフ安全化）の回帰テスト。

仕様の必須テスト15項目のうち本ファイルが 1〜9・12〜14 を、
tests/test_progress_eta.py が 10（進捗 0..total）・11（ETAリセット）をカバーする。
外部サーバは使わない（MockLLM / MockEmbedding / 決定的ハッシュ埋め込みのみ）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from llama_index.core.embeddings import MockEmbedding  # noqa: E402


class HashEmbedding(MockEmbedding):
    """トークン出現を次元へ写す決定的な疑似埋め込み（ランキング検証用）。

    MockEmbedding は全テキストが同一ベクトルになるため順位を検証できない。
    こちらは語の重なりがコサイン類似度に反映される（外部サーバ不要のまま）。
    """

    def _emb(self, text: str):
        import hashlib
        import re

        v = [0.0] * self.embed_dim
        for tok in re.findall(r"[A-Za-z0-9]+|[ぁ-んァ-ヶ一-龠]{1,2}",
                              (text or "").lower()):
            v[int(hashlib.md5(tok.encode()).hexdigest(), 16) % self.embed_dim] += 1.0
        n = sum(x * x for x in v) ** 0.5 or 1.0
        return [x / n for x in v]

    def _get_text_embedding(self, text):  # noqa: D102
        return self._emb(text)

    def _get_query_embedding(self, query):  # noqa: D102
        return self._emb(query)

    async def _aget_text_embedding(self, text):  # noqa: D102
        return self._emb(text)

    async def _aget_query_embedding(self, query):  # noqa: D102
        return self._emb(query)


def _inject(embedding=None):
    from llama_index.core import Settings as LI
    from llama_index.core.llms import MockLLM

    LI.embed_model = embedding or MockEmbedding(embed_dim=8)
    LI.llm = MockLLM(max_tokens=32)
    import llmlab.rag as ragmod

    ragmod.apply_llama_settings = lambda: None


def _write(dirp: Path, name: str, text: str) -> Path:
    dirp.mkdir(parents=True, exist_ok=True)
    p = dirp / name
    p.write_text(text, encoding="utf-8")
    return p


def _make_docs(tmp_path: Path, n: int):
    """n 文書（1文書=1固有語 ALPHAxx）を fast で登録して (im, ids) を返す。"""
    from llmlab.indexmanager import IndexManager

    src = tmp_path / "src"
    im = IndexManager(storage_dir=tmp_path / "index")
    ids = []
    for i in range(1, n + 1):
        p = _write(src, f"doc{i:02d}.txt",
                   f"# 第{i}章\nALPHA{i:02d} の規定はここに書かれている。" * 12)
        meta = im.add_document(p, title=f"文書{i:02d}", index_mode="fast")
        ids.append(meta["doc_id"])
    return im, ids


# --------------------------------------------------------------------------
# 1・2. doc_ids 明示選択は document_top_n で切り捨てない（search / ask）
# --------------------------------------------------------------------------

def test_search_returns_all_13_selected_docs_despite_top_n_4(tmp_path):
    _inject()
    im, ids = _make_docs(tmp_path, 13)
    hits = im.search("規定について", doc_ids=ids, document_top_n=4)
    assert len(hits) == 13
    assert {h.doc_id for h in hits} == set(ids)
    assert all(h.chunks for h in hits), "選択された全文書にチャンクが付く"


def test_search_docids_deduped_keeps_all_unique(tmp_path):
    _inject()
    im, ids = _make_docs(tmp_path, 4)
    hits = im.search("規定", doc_ids=ids + ids, document_top_n=2)  # 重複入り
    assert sorted(h.doc_id for h in hits) == sorted(ids)


def test_ask_grounds_all_13_selected_docs(tmp_path, monkeypatch):
    _inject()
    import llmlab.bookindex as bx

    prompts = []
    monkeypatch.setattr(bx, "llm_text",
                        lambda prompt, **kw: (prompts.append(prompt) or "回答"))
    im, ids = _make_docs(tmp_path, 13)
    r = im.ask("13文書を比較してください", doc_ids=ids, document_top_n=4)
    assert {h.doc_id for h in r.hits} == set(ids)
    assert {p["doc_id"] for p in (r.per_doc or [])} == set(ids), \
        "Map-Reduce の部分回答が全13文書をカバーする"


# --------------------------------------------------------------------------
# 3. doc_ids 未指定なら document_top_n が効く（自動選定）
# --------------------------------------------------------------------------

def test_top_n_applies_when_no_docids(tmp_path):
    _inject(HashEmbedding(embed_dim=64))
    im, ids = _make_docs(tmp_path, 13)
    hits = im.search("ALPHA07 の規定", document_top_n=3)
    assert len(hits) == 3, "自動選定は top_n で絞る"
    assert hits[0].doc_id == ids[6], "質問語 ALPHA07 を含む文書が1位"


# --------------------------------------------------------------------------
# 4・5. コレクション/タグはスコープ（範囲外は候補にすら入らない・範囲内で top-N）
# --------------------------------------------------------------------------

def _two_folders(tmp_path):
    from llmlab.indexmanager import IndexManager

    fa, fb = tmp_path / "規程A", tmp_path / "規程B"
    _write(fa, "cat.txt", "# 猫\nCATFOOD の給与規定。" * 15)
    _write(fa, "dog.txt", "# 犬\nDOGFOOD の給与規定。" * 15)
    _write(fb, "bird.txt", "# 鳥\nBIRDSEED の給与規定。" * 15)
    im = IndexManager(storage_dir=tmp_path / "index")
    ra = im.add_folder(fa)
    rb = im.add_folder(fb)
    return im, ra, rb


def test_scope_excludes_docs_outside_collection(tmp_path):
    _inject(HashEmbedding(embed_dim=64))
    im, ra, rb = _two_folders(tmp_path)
    a_ids = {m["doc_id"] for m in im.documents()
             if ra["collection_id"] in m.get("collection_ids", [])}

    # サニティ: スコープ無しなら BIRDSEED 文書が1位に来る質問
    free = im.search("BIRDSEED の規定")
    assert free[0].doc_id not in a_ids

    hits = im.search("BIRDSEED の規定", collection_ids=[ra["collection_id"]])
    assert hits, "範囲内の文書は返る（補完される）"
    assert {h.doc_id for h in hits} <= a_ids, "範囲外の文書は候補にすら入らない"


def test_top_n_within_collection(tmp_path):
    _inject(HashEmbedding(embed_dim=64))
    from llmlab.indexmanager import IndexManager

    f = tmp_path / "五文書"
    for i in range(5):
        _write(f, f"d{i}.txt", f"# {i}\nBETA{i} の規定。" * 15)
    im = IndexManager(storage_dir=tmp_path / "index")
    r = im.add_folder(f)
    ids = {m["doc_id"] for m in im.documents()}
    hits = im.search("BETA3 の規定", collection_ids=[r["collection_id"]],
                     document_top_n=2)
    assert len(hits) == 2, "範囲内で top-N が効く"
    assert {h.doc_id for h in hits} <= ids


def test_tag_scope_filters(tmp_path):
    _inject(HashEmbedding(embed_dim=64))
    im, ra, rb = _two_folders(tmp_path)
    hits = im.search("給与規定", tags=["規程B"])  # フォルダ名の自動タグ
    b_ids = {m["doc_id"] for m in im.documents()
             if "規程B" in (m.get("tags") or [])}
    assert hits and {h.doc_id for h in hits} <= b_ids


# --------------------------------------------------------------------------
# 6. 同一内容ファイルが2フォルダにあっても doc_id は1つ・所属は2つ
# --------------------------------------------------------------------------

def test_same_content_in_two_folders_single_doc_two_memberships(tmp_path):
    _inject()
    from llmlab.indexmanager import IndexManager

    body = "# 共通\n共通規程の本文 SHARED01。" * 15
    f1, f2 = tmp_path / "総務", tmp_path / "人事"
    _write(f1, "共通規程.txt", body)
    _write(f2, "共通規程.txt", body)
    im = IndexManager(storage_dir=tmp_path / "index")
    r1 = im.add_folder(f1)
    r2 = im.add_folder(f2)

    docs = im.documents()
    assert len(docs) == 1, "同一内容は doc_id 1つ（重複登録しない）"
    meta = docs[0]
    assert set(meta["collection_ids"]) == {r1["collection_id"], r2["collection_id"]}
    assert {"総務", "人事"} <= set(meta["tags"]), "両フォルダの自動タグを持つ"
    cols = {c["collection_id"]: c for c in im.collections()}
    assert cols[r1["collection_id"]]["doc_count"] == 1
    assert cols[r2["collection_id"]]["doc_count"] == 1
    # どちらのコレクションをスコープにしても見つかる
    for cid in (r1["collection_id"], r2["collection_id"]):
        hits = im.search("共通規程", collection_ids=[cid])
        assert [h.doc_id for h in hits] == [meta["doc_id"]]


def test_recursive_folder_adds_subfolder_tags(tmp_path):
    _inject()
    from llmlab.indexmanager import IndexManager

    root = tmp_path / "規程集"
    _write(root / "労務", "a.txt", "# 労\n労務規程の本文。" * 15)
    _write(root, "b.txt", "# 全\n全体方針の本文。" * 15)
    im = IndexManager(storage_dir=tmp_path / "index")

    r0 = im.add_folder(root)                    # 既定: 非再帰
    assert r0["added"] == 1, "recursive=False はサブフォルダを読まない"

    r1 = im.add_folder(root, recursive=True)
    assert r1["added"] == 1
    sub = next(m for m in im.documents() if m["title"] == "a")
    assert {"規程集", "労務"} <= set(sub["tags"]), "サブフォルダ名が自動タグになる"


# --------------------------------------------------------------------------
# 7. グラフ設定の伝搬（IndexManager → BookRAG）と graph_checkpoint
# --------------------------------------------------------------------------

def test_graph_settings_propagate_to_bookrag(tmp_path, monkeypatch):
    _inject()
    import llmlab.bookrag as brmod
    from llmlab.indexmanager import IndexManager

    captured = {}

    class FakeBookRAG:
        def __init__(self, storage_dir=None, **kw):
            captured.update(kw)

        def add_book(self, *a, **kw):
            captured["add_book"] = kw

        def has_graph(self):
            return True

    monkeypatch.setattr(brmod, "BookRAG", FakeBookRAG)
    p = _write(tmp_path / "src", "g.txt", "# 章\nグラフ対象の本文。" * 15)
    im = IndexManager(storage_dir=tmp_path / "index",
                      graph_max_workers=2, graph_chunk_chars=1234)
    meta = im.add_document(p, index_mode="graph",
                           graph_settings={"graph_max_workers": 1,
                                           "graph_max_nodes": 42})
    assert captured["max_workers"] == 1, "呼び出し時指定が最優先"
    assert captured["max_nodes"] == 42
    assert captured["chunk_chars"] == 1234, "コンストラクタ設定も伝搬"
    assert captured["er_use_llm"] is False, "既定は LLM 名寄せなし"
    assert captured["add_book"]["graph_checkpoint"] is True
    assert meta["graph_status"] == "ready"


def test_default_graph_settings_are_safe(tmp_path):
    _inject()
    from llmlab.bookrag import BookRAG
    from llmlab.indexmanager import SAFE_GRAPH_DEFAULTS, IndexManager

    im = IndexManager(storage_dir=tmp_path / "index")
    assert im.graph_settings["graph_max_workers"] == 1, "既定は1並列（8並列にしない）"
    assert im.graph_settings["graph_max_nodes"] <= 100
    assert im.graph_settings["er_use_llm"] is False
    assert SAFE_GRAPH_DEFAULTS["graph_max_workers"] == 1
    assert BookRAG.__init__.__defaults__ is not None  # 位置デフォルト無しでもOK
    import inspect

    sig = inspect.signature(BookRAG.__init__)
    assert sig.parameters["max_workers"].default <= 2, "BookRAG 単体の既定も安全側"


# --------------------------------------------------------------------------
# 8. グラフ失敗でも文書は利用可能（vector 検索が生きる）
# --------------------------------------------------------------------------

def test_vector_search_survives_graph_failure(tmp_path, monkeypatch):
    _inject()
    import llmlab.bookrag as brmod
    from llmlab.indexmanager import IndexManager

    class BoomBookRAG:
        def __init__(self, **kw):
            pass

        def add_book(self, *a, **kw):
            raise RuntimeError("graph 構築失敗（模擬）")

        def has_graph(self):
            return False

    monkeypatch.setattr(brmod, "BookRAG", BoomBookRAG)
    p = _write(tmp_path / "src", "g.txt", "# 章\nGAMMA01 の規定。" * 15)
    im = IndexManager(storage_dir=tmp_path / "index")
    meta = im.add_document(p, index_mode="graph")

    assert meta["status"] == "ready", "グラフ失敗で文書全体を failed にしない"
    assert meta["vector_status"] == "ready"
    assert meta["graph_status"] == "failed"
    assert "RuntimeError" in (meta["graph_error"] or "")
    hits = im.search("GAMMA01 の規定", doc_ids=[meta["doc_id"]])
    assert hits and hits[0].chunks, "通常検索は利用可能"
    st = im._read(im.status_dir, meta["doc_id"])
    assert st.get("note") == "グラフのみ失敗"


# --------------------------------------------------------------------------
# 9. チェックポイント再開（完了済みノードを再抽出しない・完走で削除・署名ガード）
# --------------------------------------------------------------------------

def _mini_bookindex():
    import llmlab.bookindex as bx

    bi = bx.BookIndex()
    root = bi.add_node(type="Section", content="", book="B", title="root", level=1)
    bi.roots.append(root.id)
    ids = []
    for i in range(5):
        n = bi.add_node(type="Text", book="B", parent=root.id,
                        content=f"ノード{i}の本文です。" * 8)
        root.children.append(n.id)
        ids.append(n.id)
    return bi, ids


def test_checkpoint_resume_skips_completed_nodes(tmp_path, monkeypatch):
    _inject()
    import llmlab.bookindex as bx

    bi, node_ids = _mini_bookindex()
    ckpt = tmp_path / "graph_progress.json"

    calls1: list[int] = []

    def extract1(node, fail_fast=True):
        calls1.append(node.id)
        return {"entities": [{"name": f"E{node.id}", "type": "T",
                              "description": "d"}], "relations": []}

    monkeypatch.setattr(bx, "_extract_graph", extract1)
    monkeypatch.setattr(bx, "embed",
                        lambda texts: (_ for _ in ()).throw(RuntimeError("embed down")))
    # 1回目: 抽出は完了するが ER 直前の埋め込みで失敗 → チェックポイントが残る
    with pytest.raises(RuntimeError):
        bx.build_graph(bi, node_ids, max_workers=1,
                       checkpoint_path=str(ckpt))
    assert ckpt.exists()
    assert sorted(calls1) == sorted(node_ids), "初回は全ノードを抽出"

    calls2: list[int] = []

    def extract2(node, fail_fast=True):
        calls2.append(node.id)
        return {"entities": [], "relations": []}

    monkeypatch.setattr(bx, "_extract_graph", extract2)
    monkeypatch.setattr(bx, "embed",
                        lambda texts: np.ones((len(texts), 4), dtype=np.float32))
    # 2回目: 完了済みノードは再抽出せず、チェックポイントの結果で完走する
    bx.build_graph(bi, node_ids, max_workers=1, checkpoint_path=str(ckpt))
    assert calls2 == [], "完了済みノードを再抽出しない"
    assert bi.entities, "チェックポイントの抽出結果から KG が構築される"
    assert not ckpt.exists(), "完走したらチェックポイントを削除する"


def test_checkpoint_signature_guard_discards_stale(tmp_path):
    _inject()
    import llmlab.bookindex as bx

    bi, node_ids = _mini_bookindex()
    ckpt = tmp_path / "graph_progress.json"
    data = {"nodes": {str(node_ids[0]): {"status": "ok", "entities": [],
                                         "relations": [], "error": None}}}
    bx._save_checkpoint(str(ckpt), data, bi)
    assert bx._load_checkpoint(str(ckpt), bi).get("nodes"), "署名一致なら読み込む"
    bi.nodes[node_ids[0]].content += "（内容が変わった）"
    assert bx._load_checkpoint(str(ckpt), bi).get("nodes") == {}, \
        "ノード内容が変わったら抽出済み結果を破棄（空から再抽出）"


def test_extraction_caps_are_bounded():
    """抽出上限が仕様の安全レンジ内（出力 600〜1000 トークン・件数上限あり）。"""
    import llmlab.bookindex as bx

    assert 1 <= bx.MAX_ENTITIES_PER_NODE <= 20
    assert 1 <= bx.MAX_RELATIONS_PER_NODE <= 30
    src = Path(bx.__file__).read_text(encoding="utf-8")
    assert "max_tokens=800 if fail_fast else 1000" in src, \
        "抽出の max_tokens は 600〜1000 に制限（2000〜4000 にしない）"


# --------------------------------------------------------------------------
# 12. 長文書の後半セクションが落ちない（文書内候補のリランク）
# --------------------------------------------------------------------------

def test_long_doc_tail_section_not_dropped(tmp_path):
    _inject(HashEmbedding(embed_dim=64))
    from llmlab.indexmanager import IndexManager

    filler = "".join(f"## 第{i}節\n一般的な説明文 FILLER{i:02d} が続く。" * 10
                     for i in range(1, 16))
    body = filler + "\n## 附則\nZB-9000 手当は 12000 円とする。"
    p = _write(tmp_path / "src", "long.txt", body)
    im = IndexManager(storage_dir=tmp_path / "index")
    meta = im.add_document(p, title="長い規程")
    assert meta["chunk_count"] >= 4, "複数チャンクに分かれる長文書"

    hits = im.search("ZB-9000 手当の金額", doc_ids=[meta["doc_id"]],
                     chunk_top_k_per_doc=2)
    assert any("ZB-9000" in c["text"] for c in hits[0].chunks), \
        "文書末尾のセクションも候補に入りリランクで採用される"
    assert all("chunk_id" in c and "section_id" in c for c in hits[0].chunks), \
        "チャンクは doc_id/section_id/chunk_id で追跡できる"


# --------------------------------------------------------------------------
# 13. 13文書の比較は「1つの巨大プロンプト」ではなく Map-Reduce
# --------------------------------------------------------------------------

def test_13_doc_compare_uses_map_reduce_not_giant_prompt(tmp_path, monkeypatch):
    _inject()
    import llmlab.bookindex as bx
    from llmlab.indexmanager import CONTEXT_CHAR_BUDGET

    prompts: list[str] = []
    monkeypatch.setattr(bx, "llm_text",
                        lambda prompt, **kw: (prompts.append(prompt) or "部分回答"))
    im, ids = _make_docs(tmp_path, 13)
    r = im.ask("全文書を比較してください", doc_ids=ids)

    assert len(prompts) == 14, "13件の部分回答 + 1件の統合（巨大単発ではない）"
    for p in prompts[:13]:
        assert p.count("### 文書「") == 1, "Map は1文書ずつ処理する"
        assert len(p) <= CONTEXT_CHAR_BUDGET * 2, "プロンプトは予算内に収まる"
    reduce_p = prompts[13]
    for i in range(1, 14):
        assert f"文書{i:02d}" in reduce_p, "統合プロンプトが全文書に言及する"
    assert len(r.per_doc or []) == 13


def test_few_docs_single_prompt_with_budget(tmp_path, monkeypatch):
    _inject()
    import llmlab.bookindex as bx
    from llmlab.indexmanager import MAP_REDUCE_DOC_THRESHOLD

    prompts: list[str] = []
    monkeypatch.setattr(bx, "llm_text",
                        lambda prompt, **kw: (prompts.append(prompt) or "回答"))
    im, ids = _make_docs(tmp_path, MAP_REDUCE_DOC_THRESHOLD - 1)
    im.ask("要点は？", doc_ids=ids)
    assert len(prompts) == 1, "少数文書は単一プロンプト（無駄な多段呼び出しをしない）"
    assert prompts[0].count("### 文書「") == len(ids), "全文書が文脈に入る"


# --------------------------------------------------------------------------
# 14. コレクション情報の無い旧インデックスも読める（後方互換）
# --------------------------------------------------------------------------

def test_old_index_without_collection_fields_still_works(tmp_path):
    _inject()
    from llmlab.indexmanager import IndexManager

    im, ids = _make_docs(tmp_path, 2)
    storage = tmp_path / "index"
    # 旧フォーマットを模擬: 新設フィールドを剥がし、collections/memberships を消す
    for p in (storage / "docs").glob("*.json"):
        meta = json.loads(p.read_text(encoding="utf-8"))
        for k in ("collection_ids", "relative_path", "tags", "vector_status",
                  "hierarchy_status", "graph_status", "graph_error"):
            meta.pop(k, None)
        p.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    import shutil

    shutil.rmtree(storage / "collections", ignore_errors=True)
    (storage / "memberships.json").unlink(missing_ok=True)

    im2 = IndexManager(storage_dir=storage)
    docs = im2.documents()
    assert {m["doc_id"] for m in docs} == set(ids), "旧メタも一覧できる"
    hits = im2.search("規定", doc_ids=[ids[0]])
    assert hits and hits[0].chunks, "旧メタの文書も検索できる"
    assert im2.collections() == []
    assert im2.all_tags() == []
    assert im2.search("規定", tags=["存在しないタグ"]) == [], \
        "未知タグはヒット0（例外にしない）"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
