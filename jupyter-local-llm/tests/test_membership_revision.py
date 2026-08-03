"""共有文書の改訂（membership分離）・タグ保存・スコープAND/OR の回帰テスト。

- 内容が同一のファイルを複数フォルダから取り込むと doc_id は1つに統合される。
  その後 **一方のフォルダのファイルだけ** を改訂したとき、旧 doc_id をグローバル
  削除してはならない（別フォルダに残る旧文書の索引まで消えるバグの回帰）。
- 単一ファイル追加の明示タグが保存されること。
- スコープ条件: collection=OR / タグ=AND / 組み合わせ=AND。
外部サーバは使わない（MockLLM/MockEmbedding のみ）。
"""

from __future__ import annotations

import json
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


BODY_V1 = "# 共通規程\n共通規程 第1版 SHAREDV1 の本文。" * 15
BODY_V2 = "# 共通規程\n共通規程 第2版 SHAREDV2 の本文（改訂済み）。" * 15


def _setup_shared(tmp_path):
    """A/B フォルダに同一内容のファイルを置いて取り込む。"""
    from llmlab.indexmanager import IndexManager

    fa, fb = tmp_path / "A", tmp_path / "B"
    _write(fa, "共通規程.txt", BODY_V1)
    _write(fb, "共通規程.txt", BODY_V1)
    im = IndexManager(storage_dir=tmp_path / "index")
    ra = im.add_folder(fa)
    rb = im.add_folder(fb)
    return im, fa, fb, ra, rb


# --------------------------------------------------------------------------
# 仕様の必須回帰テスト（1〜6）
# --------------------------------------------------------------------------

def test_shared_doc_revision_keeps_other_folder_intact(tmp_path):
    _inject()
    im, fa, fb, ra, rb = _setup_shared(tmp_path)

    # (2) 文書は1つ・membership は2つ
    docs = im.documents()
    assert len(docs) == 1
    old_id = docs[0]["doc_id"]
    rows = im._memberships()[old_id]
    assert len(rows) == 2
    assert all(r.get("source_path") for r in rows), "所在に source_path を保存する"

    # (3) A 側だけ内容を変更して再取り込み
    _write(fa, "共通規程.txt", BODY_V2)
    im.add_folder(fa)

    # (4) A の新版と B の旧版が **別々の doc_id** で存在・検索できる
    docs = {m["doc_id"]: m for m in im.documents()}
    assert len(docs) == 2, "旧版が消えていない（グローバル削除しない）"
    new_id = next(d for d in docs if d != old_id)
    assert old_id in docs, "B に残る旧版の索引・メタが保持される"

    old_meta, new_meta = docs[old_id], docs[new_id]
    assert old_meta["collection_ids"] == [rb["collection_id"]], \
        "旧版の所属は B だけになる"
    assert ra["collection_id"] in new_meta["collection_ids"], "新版は A に所属"
    assert str((fb / "共通規程.txt").resolve()) == old_meta["source_path"], \
        "旧版の source_path は改訂されたファイルを指したままにしない"

    hits_old = im.search("共通規程", doc_ids=[old_id])
    hits_new = im.search("共通規程", doc_ids=[new_id])
    assert hits_old and hits_old[0].chunks, "旧版のベクトル索引が生きている"
    assert hits_new and hits_new[0].chunks

    # (5) 両コレクションの doc_count が正しい
    counts = {c["collection_id"]: c["doc_count"] for c in im.collections()}
    assert counts[ra["collection_id"]] == 1
    assert counts[rb["collection_id"]] == 1

    # (6) B 側の旧文書の検索結果に A の新版内容が混入しない
    old_text = " ".join(c["text"] for c in hits_old[0].chunks)
    assert "SHAREDV1" in old_text
    assert "SHAREDV2" not in old_text
    new_text = " ".join(c["text"] for c in hits_new[0].chunks)
    assert "SHAREDV2" in new_text


def test_revision_when_sole_location_fully_replaces(tmp_path):
    """所在が1つだけの文書の改訂は従来どおり完全置換（旧版は残らない）。"""
    _inject()
    from llmlab.indexmanager import IndexManager

    src = tmp_path / "src"
    p = _write(src, "単独.txt", BODY_V1)
    im = IndexManager(storage_dir=tmp_path / "index")
    old = im.add_document(p, tags=["単独"])
    _write(src, "単独.txt", BODY_V2)
    new = im.add_document(p)
    docs = im.documents()
    assert len(docs) == 1 and docs[0]["doc_id"] == new["doc_id"]
    assert old["doc_id"] != new["doc_id"]
    assert "単独" in docs[0]["tags"], "完全置換ではタグを引き継ぐ"


def test_folder_rescan_does_not_touch_other_folder(tmp_path):
    """フォルダの再走査（内容変更なし）で別フォルダの文書を置き換えない。"""
    _inject()
    im, fa, fb, ra, rb = _setup_shared(tmp_path)
    before = {m["doc_id"]: m["updated_at"] for m in im.documents()}
    r = im.add_folder(fa)   # 変更なしの再走査
    assert r["skipped"] == 1 and r["added"] == 0
    docs = im.documents()
    assert len(docs) == 1
    assert set(docs[0]["collection_ids"]) == {ra["collection_id"],
                                              rb["collection_id"]}
    assert before  # 参照維持


def test_rebuild_picks_existing_source_from_memberships(tmp_path):
    """meta.source_path のファイルが消えても membership の所在から再構築できる。"""
    _inject()
    im, fa, fb, ra, rb = _setup_shared(tmp_path)
    doc = im.documents()[0]
    # meta.source_path 側のファイルを削除（もう一方のフォルダには残っている）
    gone = Path(doc["source_path"])
    gone.unlink()
    meta = im.rebuild(doc["doc_id"])
    assert meta["status"] in ("ready", "skipped")
    assert Path(meta["source_path"]).exists()


def test_delete_only_if_orphan(tmp_path):
    _inject()
    im, fa, fb, ra, rb = _setup_shared(tmp_path)
    did = im.documents()[0]["doc_id"]
    assert im.delete(did, only_if_orphan=True) is False, \
        "所在が残る文書は only_if_orphan で消えない"
    assert im.documents(), "文書は残っている"
    assert im.delete(did) is True, "明示削除は従来どおり全削除"
    assert im.documents() == []


def test_legacy_memberships_without_source_path_still_replace(tmp_path):
    """旧形式（source_path の無い membership / meta のみ）でも従来どおり動く。"""
    _inject()
    from llmlab.indexmanager import IndexManager

    src = tmp_path / "src"
    p = _write(src, "旧形式.txt", BODY_V1)
    im = IndexManager(storage_dir=tmp_path / "index")
    old = im.add_document(p)
    # membership を旧形式に劣化させる（source_path キーを剥がす）
    mpath = im._memberships_path
    m = json.loads(mpath.read_text(encoding="utf-8"))
    for rows in m.values():
        for r in rows:
            r.pop("source_path", None)
            r.pop("tags", None)
    mpath.write_text(json.dumps(m, ensure_ascii=False), encoding="utf-8")

    _write(src, "旧形式.txt", BODY_V2)
    new = im.add_document(p)
    docs = im.documents()
    assert len(docs) == 1 and docs[0]["doc_id"] == new["doc_id"] != old["doc_id"], \
        "旧形式は所在を切り分けられないため従来どおり完全置換"


# --------------------------------------------------------------------------
# V2: 単一ファイルのタグ保存
# --------------------------------------------------------------------------

def test_single_file_tags_saved_without_collection(tmp_path):
    _inject()
    from llmlab.indexmanager import IndexManager

    p = _write(tmp_path / "src", "t.txt", "# 章\nタグ検証の本文。" * 15)
    im = IndexManager(storage_dir=tmp_path / "index")
    meta = im.add_document(p, tags=[" 規程 ", "2024", "", "規程"])
    assert meta["tags"] == ["規程", "2024"], "正規化（空白除去・空捨て・重複除去）"
    assert im.documents()[0]["tags"] == ["規程", "2024"]

    # 再取り込み（変更なし skip）で既存タグを消さない・追記できる
    again = im.add_document(p, tags=["人事"])
    assert again["tags"] == ["規程", "2024", "人事"]

    # set_tags は置換
    im.set_tags(meta["doc_id"], ["新タグ"])
    assert im.documents()[0]["tags"] == ["新タグ"]

    # タグはスコープ検索でも効く
    hits = im.search("タグ検証", tags=["新タグ"])
    assert [h.doc_id for h in hits] == [meta["doc_id"]]


# --------------------------------------------------------------------------
# V3: スコープの AND/OR 仕様
# --------------------------------------------------------------------------

def _tagged_corpus(tmp_path):
    from llmlab.indexmanager import IndexManager

    im = IndexManager(storage_dir=tmp_path / "index")
    src = tmp_path / "src"
    d1 = im.add_document(_write(src, "d1.txt", "# 1\n文書1の本文。" * 15),
                         tags=["規程", "2024"])
    d2 = im.add_document(_write(src, "d2.txt", "# 2\n文書2の本文。" * 15),
                         tags=["規程"])
    d3 = im.add_document(_write(src, "d3.txt", "# 3\n文書3の本文。" * 15),
                         tags=["2024"])
    return im, d1["doc_id"], d2["doc_id"], d3["doc_id"]


def test_multiple_tags_are_and(tmp_path):
    _inject()
    im, d1, d2, d3 = _tagged_corpus(tmp_path)
    scope = im._scope_doc_ids(tags=["規程", "2024"])
    assert scope == {d1}, "複数タグは AND（すべて持つ文書だけ）"
    assert im._scope_doc_ids(tags=["規程"]) == {d1, d2}


def test_multiple_collections_are_or_and_combo_is_and(tmp_path):
    _inject()
    from llmlab.indexmanager import IndexManager

    fa, fb = tmp_path / "A", tmp_path / "B"
    _write(fa, "a.txt", "# a\nフォルダAの文書。" * 15)
    _write(fb, "b.txt", "# b\nフォルダBの文書。" * 15)
    im = IndexManager(storage_dir=tmp_path / "index")
    ra, rb = im.add_folder(fa), im.add_folder(fb)
    ids = {m["title"]: m["doc_id"] for m in im.documents()}

    both = im._scope_doc_ids(collection_ids=[ra["collection_id"],
                                             rb["collection_id"]])
    assert both == set(ids.values()), "複数 collection は OR"

    combo = im._scope_doc_ids(collection_ids=[ra["collection_id"],
                                              rb["collection_id"]],
                              tags=["A"])   # フォルダ名の自動タグ
    assert combo == {ids["a"]}, "collection 条件とタグ条件の組み合わせは AND"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
