"""監査項目1〜5の回帰テスト: 改訂のトランザクション化・キャッシュヒット時の
所在移動・タグ整合・ユニーク doc_count・rebuild の所属維持。

外部サーバは使わない（MockLLM/MockEmbedding + 挿入スパイ）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


def _inject():
    from llama_index.core import Settings as LI
    from llama_index.core.embeddings import MockEmbedding
    from llama_index.core.llms import MockLLM

    LI.embed_model = MockEmbedding(embed_dim=8)
    LI.llm = MockLLM(max_tokens=32)
    import llmlab.rag as ragmod

    ragmod.apply_llama_settings = lambda: None


def _write(dirp: Path, name: str, text: str) -> Path:
    dirp.mkdir(parents=True, exist_ok=True)
    p = dirp / name
    p.write_text(text, encoding="utf-8")
    return p


V1 = "# 共通規程\n" + "第1版 SHAREDV1 の本文が続く。" * 1200   # 十数チャンク=複数バッチ
V2 = "# 共通規程\n" + "第2版 SHAREDV2 の本文（改訂）。" * 1200


# --------------------------------------------------------------------------
# 1. 改訂のトランザクション化（新版失敗でも旧版・旧membership・旧検索が生存）
# --------------------------------------------------------------------------

def _fail_on_batch(monkeypatch, n_fail=2):
    from llama_index.core.indices.vector_store import VectorStoreIndex

    orig = VectorStoreIndex.insert_nodes
    state = {"n": 0}

    def failing(self, nodes, **kw):
        state["n"] += 1
        if state["n"] == n_fail:
            raise RuntimeError("embedding server down（模擬）")
        return orig(self, nodes, **kw)

    monkeypatch.setattr(VectorStoreIndex, "insert_nodes", failing)
    return orig


def test_revision_failure_preserves_old_version(tmp_path, monkeypatch):
    _inject()
    from llama_index.core.indices.vector_store import VectorStoreIndex

    from llmlab.indexmanager import IndexManager

    src = tmp_path / "src"
    p = _write(src, "規程.txt", V1)
    im = IndexManager(storage_dir=tmp_path / "index", ingest_batch_size=8)
    old = im.add_document(p, tags=["規程"])
    old_id = old["doc_id"]

    # ファイルを新版へ書き換え、2バッチ目で模擬例外
    _write(src, "規程.txt", V2)
    orig = _fail_on_batch(monkeypatch, n_fail=2)
    with pytest.raises(RuntimeError):
        im.add_document(p)

    # 旧版は変更前の状態を維持（ready・membership・検索）
    old_meta = im._read(im.docs_dir, old_id)
    assert old_meta and old_meta["status"] == "ready", "例外後も旧doc_idがready"
    rows = im._memberships().get(old_id, [])
    assert rows, "旧membershipが残る"
    hits = im.search("SHAREDV1", doc_ids=[old_id])
    assert hits and hits[0].chunks, "旧版を検索できる"
    assert "SHAREDV1" in hits[0].chunks[0]["text"]

    # 再実行（成功）で初めて移行される
    monkeypatch.setattr(VectorStoreIndex, "insert_nodes", orig)
    new = im.add_document(p)
    assert new["status"] == "ready"
    assert im._read(im.docs_dir, old_id) is None, "成功後に旧版が置換される"
    assert "規程" in new["tags"], "タグは新版へ引き継がれる"


def test_shared_revision_failure_keeps_both_locations(tmp_path, monkeypatch):
    """共有文書（A/B）の A 改訂が失敗しても、両所在とも変更前のまま。"""
    _inject()
    from llmlab.indexmanager import IndexManager

    fa, fb = tmp_path / "A", tmp_path / "B"
    _write(fa, "共通.txt", V1)
    _write(fb, "共通.txt", V1)
    im = IndexManager(storage_dir=tmp_path / "index", ingest_batch_size=8)
    im.add_folder(fa)
    im.add_folder(fb)
    old_id = im.documents()[0]["doc_id"]

    _write(fa, "共通.txt", V2)
    _fail_on_batch(monkeypatch, n_fail=2)
    r = im.add_folder(fa)          # add_folder は失敗を集計して続行
    assert r["failed"] == 1

    rows = im._memberships().get(old_id, [])
    assert len(rows) == 2, "失敗時は A の所在も外れない（変更前を維持）"
    assert im._read(im.docs_dir, old_id)["status"] == "ready"


# --------------------------------------------------------------------------
# 2. キャッシュヒット時にも所在を移動する
# --------------------------------------------------------------------------

def test_cache_hit_moves_location_from_old_doc(tmp_path, monkeypatch):
    _inject()
    from llama_index.core.indices.vector_store import VectorStoreIndex

    from llmlab.indexmanager import IndexManager

    fa, fb = tmp_path / "A", tmp_path / "B"
    _write(fa, "rule.txt", V1)    # A = V1
    _write(fb, "rule.txt", V2)    # B = V2
    im = IndexManager(storage_dir=tmp_path / "index")
    ra = im.add_folder(fa)
    rb = im.add_folder(fb)
    docs = {m["doc_id"] for m in im.documents()}
    assert len(docs) == 2
    v2_id = next(m["doc_id"] for m in im.documents()
                 if ra["collection_id"] not in m.get("collection_ids", []))
    v1_id = (docs - {v2_id}).pop()

    # A/rule.txt を V2 へ変更して A を再取り込み（内容は索引済み → キャッシュヒット）
    _write(fa, "rule.txt", V2)
    calls = []
    orig = VectorStoreIndex.insert_nodes

    def spy(self, nodes, **kw):
        calls.append(len(nodes))
        return orig(self, nodes, **kw)

    monkeypatch.setattr(VectorStoreIndex, "insert_nodes", spy)
    r = im.add_folder(fa)
    assert r["skipped"] == 1, "既存 V2 の埋め込みを再利用（再埋め込みしない）"
    assert calls == [], "ベクトル挿入は発生しない"

    # V1 は所在0件 → 削除。V2 は A/B の2所在
    assert im._read(im.docs_dir, v1_id) is None, "所在0件の旧版 V1 は削除"
    rows = im._memberships().get(v2_id, [])
    assert len(rows) == 2, "V2 の membership は A と B の2行"
    cids = {r["collection_id"] for r in rows}
    assert cids == {ra["collection_id"], rb["collection_id"]}
    counts = {c["collection_id"]: c["doc_count"] for c in im.collections()}
    assert counts[ra["collection_id"]] == 1 and counts[rb["collection_id"]] == 1
    assert len(im.documents()) == 1, "A が V1 と V2 の両方に残らない"


# --------------------------------------------------------------------------
# 3. タグとmembershipの整合（改訂時の再計算・set_tags の同期）
# --------------------------------------------------------------------------

def test_tag_recomputed_from_remaining_rows(tmp_path):
    _inject()
    from llmlab.indexmanager import IndexManager

    fa, fb = tmp_path / "A", tmp_path / "B"
    _write(fa, "共通.txt", V1)
    _write(fb, "共通.txt", V1)
    im = IndexManager(storage_dir=tmp_path / "index")
    im.add_folder(fa, tags=["タグA"])
    im.add_folder(fb, tags=["タグB"])
    old_id = im.documents()[0]["doc_id"]
    assert {"A", "B", "タグA", "タグB"} <= set(
        im._read(im.docs_dir, old_id)["tags"])

    # A だけ改訂
    _write(fa, "共通.txt", V2)
    new = im.add_folder(fa, tags=["タグA"])
    new_id = next(m["doc_id"] for m in new["results"])

    old_meta = im._read(im.docs_dir, old_id)
    assert set(old_meta["tags"]) == {"B", "タグB"}, \
        "B に残った旧版はB側のタグのみ（Aタグが残らない)"
    new_meta = im._read(im.docs_dir, new_id)
    assert {"A", "タグA"} <= set(new_meta["tags"])

    # タグA のスコープ検索では新版だけが対象
    scope = im._scope_doc_ids(tags=["タグA"])
    assert scope == {new_id}


def test_set_tags_syncs_membership_rows(tmp_path):
    _inject()
    from llmlab.indexmanager import IndexManager

    fa = tmp_path / "A"
    _write(fa, "d.txt", V1)
    im = IndexManager(storage_dir=tmp_path / "index")
    im.add_folder(fa)
    did = im.documents()[0]["doc_id"]

    im.set_tags(did, ["新タグ", " 空白 "])
    meta = im._read(im.docs_dir, did)
    rows = im._memberships()[did]
    assert meta["tags"] == ["新タグ", "空白"]
    for r in rows:
        assert r["tags"] == ["新タグ", "空白"], "メタとmembershipのタグが一致"


# --------------------------------------------------------------------------
# 4. コレクションのユニーク doc_count
# --------------------------------------------------------------------------

def test_collection_doc_count_unique_by_doc_id(tmp_path):
    _inject()
    from llmlab.indexmanager import IndexManager

    fa = tmp_path / "A"
    _write(fa, "copy1.txt", V1)
    _write(fa, "copy2.txt", V1)   # 同一内容のファイルが同じフォルダに2個
    _write(fa, "other.txt", V2)
    im = IndexManager(storage_dir=tmp_path / "index")
    r = im.add_folder(fa)
    rows = im._memberships()
    doc_v1 = next(m["doc_id"] for m in im.documents()
                  if "copy" in (m.get("source_path") or ""))
    assert len(rows[doc_v1]) == 2, "membership 行は所在ごと（2行）"
    counts = {c["collection_id"]: c["doc_count"] for c in im.collections()}
    assert counts[r["collection_id"]] == 2, \
        "doc_count はユニーク doc_id 数（V1文書1 + V2文書1 = 2）"


# --------------------------------------------------------------------------
# 5. rebuild が偽の単独 membership を増やさない
# --------------------------------------------------------------------------

def test_rebuild_preserves_membership(tmp_path):
    _inject()
    from llmlab.indexmanager import IndexManager

    fa = tmp_path / "A"
    _write(fa, "d.txt", V1)
    im = IndexManager(storage_dir=tmp_path / "index")
    ra = im.add_folder(fa, tags=["タグA"])
    did = im.documents()[0]["doc_id"]
    before = im._memberships()[did]
    assert len(before) == 1

    meta = im.rebuild(did)
    rows = im._memberships()[meta["doc_id"]]
    assert len(rows) == 1, "collection_id=None の余分な行を作らない"
    assert rows[0]["collection_id"] == ra["collection_id"], "所属を維持"
    assert rows[0].get("relative_path") == "d.txt"
    assert "タグA" in (rows[0].get("tags") or [])
    counts = {c["collection_id"]: c["doc_count"] for c in im.collections()}
    assert counts[ra["collection_id"]] == 1


def test_rebuild_single_file_no_duplicate_row(tmp_path):
    _inject()
    from llmlab.indexmanager import IndexManager

    p = _write(tmp_path / "src", "s.txt", V1)
    im = IndexManager(storage_dir=tmp_path / "index")
    did = im.add_document(p)["doc_id"]
    im.rebuild(did)
    rows = im._memberships()[did]
    assert len(rows) == 1, "単独文書の再構築でも行は増えない"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))


def test_single_then_folder_promotes_membership_row(tmp_path):
    """単独追加→同フォルダ取り込みで、None所在行がコレクション行へ昇格する
    （同一所在の行を重複させない・タグは統合）。"""
    _inject()
    from llmlab.indexmanager import IndexManager

    fd = tmp_path / "D"
    q = _write(fd, "x.txt", V1)
    im = IndexManager(storage_dir=tmp_path / "index")
    im.add_document(q, tags=["手動"])
    r = im.add_folder(fd)
    rows = im._memberships()[im.documents()[0]["doc_id"]]
    assert len(rows) == 1, "同一所在の行は1行に統合される"
    assert rows[0]["collection_id"] == r["collection_id"], "コレクション行へ昇格"
    assert {"手動", "D"} <= set(rows[0]["tags"]), "タグは統合される"
