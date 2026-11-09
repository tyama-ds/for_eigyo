"""フォルダ（文書の置き場所）の一覧・作成・名前の変更・移動・削除と、相談の範囲（相談先）。

フォルダは documents.folder の "/" 区切りのパス。文書の無いフォルダも残せるよう folders 表にも登録する。
名前の変更・移動は配下の文書・サブフォルダ・相談の範囲もまとめて書き換える。削除しても文書は消さず、
1つ上のフォルダへ移す。
"""
from __future__ import annotations

import json

from . import db
from .indexer import clean_folder, ensure_folder


def _ancestors(path: str) -> list[str]:
    parts = path.split("/") if path else []
    return ["/".join(parts[:i + 1]) for i in range(len(parts))]


def _parent(path: str) -> str:
    return path.rsplit("/", 1)[0] if "/" in path else ""


def tree() -> list[dict]:
    """[{path, name, depth, count（配下を含む文書数）, direct（直下の文書数）}]。先頭は全体（path=""）。"""
    direct = {r["folder"]: r["n"] for r in db.query("SELECT folder, COUNT(*) n FROM documents GROUP BY folder")}
    paths: set[str] = set()
    for p in [r["path"] for r in db.query("SELECT path FROM folders")] + [f for f in direct if f]:
        paths.update(_ancestors(p))
    out = [{"path": "", "name": "すべての文書", "depth": 0, "count": sum(direct.values()),
            "direct": direct.get("", 0)}]
    for p in sorted(paths):
        out.append({"path": p, "name": p.rsplit("/", 1)[-1], "depth": p.count("/") + 1,
                    "count": sum(n for f, n in direct.items() if f == p or f.startswith(p + "/")),
                    "direct": direct.get(p, 0)})
    return out


def create(path: str) -> str:
    path = clean_folder(path)
    if not path:
        raise ValueError("フォルダ名が空です")
    ensure_folder(path)
    return path


def _remap(mapper) -> int:
    """文書・フォルダ・相談の範囲のフォルダを mapper(path) -> 新しい path | None で書き換える。"""
    stmts = []
    moved = 0
    for r in db.query("SELECT id, folder FROM documents"):
        new = mapper(r["folder"])
        if new is not None and new != r["folder"]:
            stmts.append(("UPDATE documents SET folder=?, updated_at=? WHERE id=?", (new, db.now(), r["id"])))
            moved += 1
    rows = [r["path"] for r in db.query("SELECT path FROM folders")]
    new_paths = set()
    for p in rows:
        new = mapper(p)
        if new is None or new == p:
            new_paths.add(p)
            continue
        stmts.append(("DELETE FROM folders WHERE path=?", (p,)))
        if new:
            new_paths.add(new)
    t = db.now()
    for p in new_paths:
        for a in _ancestors(p):
            stmts.append(("INSERT OR IGNORE INTO folders(path,created_at) VALUES(?,?)", (a, t)))
    for r in db.query("SELECT id, scope FROM chats"):
        try:
            scope = json.loads(r["scope"] or "{}")
        except ValueError:
            continue
        changed = False
        folders = [f for f in scope.get("folders") or [] if isinstance(f, str)]
        if scope.get("folder"):
            folders.append(scope["folder"])
        out = []
        for f in folders:
            new = mapper(f)
            if new is not None and new != f:
                changed = True
                f = new
            if f not in out:
                out.append(f)
        if changed:
            scope["folders"] = out
            scope.pop("folder", None)
            stmts.append(("UPDATE chats SET scope=? WHERE id=?", (json.dumps(scope, ensure_ascii=False), r["id"])))
    if stmts:
        db.write_many(stmts)
    db.bump_revision()
    return moved


def rename(src: str, dst: str) -> dict:
    """フォルダの名前の変更・移動（配下ごと）。dst は新しいフルパス。"""
    src, dst = clean_folder(src), clean_folder(dst)
    if not src:
        raise ValueError("「すべての文書」は移動できません")
    if not dst:
        raise ValueError("移動先のフォルダ名が空です")
    if dst == src:
        return {"path": dst, "moved": 0}
    if dst.startswith(src + "/"):
        raise ValueError("フォルダを自分の中へは移動できません")
    if any(r["path"] == dst for r in db.query("SELECT path FROM folders")) or \
            db.one("SELECT 1 FROM documents WHERE folder=? OR folder LIKE ? LIMIT 1", (dst, dst + "/%")):
        raise ValueError(f"「{dst}」はすでにあります")

    def mapper(f: str):
        if f == src:
            return dst
        if f.startswith(src + "/"):
            return dst + f[len(src):]
        return None
    return {"path": dst, "moved": _remap(mapper)}


def delete(path: str) -> dict:
    """フォルダを消す。中の文書とサブフォルダは1つ上のフォルダへ移す（文書は消さない）。"""
    path = clean_folder(path)
    if not path:
        raise ValueError("「すべての文書」は削除できません")
    parent = _parent(path)

    def mapper(f: str):
        if f == path:
            return parent
        if f.startswith(path + "/"):
            rest = f[len(path) + 1:]
            return f"{parent}/{rest}" if parent else rest
        return None
    moved = _remap(mapper)
    db.write("DELETE FROM folders WHERE path=?", (path,))
    return {"parent": parent, "moved": moved}


def move_docs(doc_ids: list[str], folder: str) -> int:
    folder = clean_folder(folder)
    ids = [str(x) for x in doc_ids if str(x)][:1000]
    if not ids:
        return 0
    t = db.now()
    db.write_many([("UPDATE documents SET folder=?, updated_at=? WHERE id=?", (folder, t, i)) for i in ids])
    if folder:
        ensure_folder(folder)
    db.bump_revision()
    return len(ids)


# ---------------------------------------------------------------- 相談先（相談の範囲）

def scope_folders(scope: dict) -> list[str]:
    out = [clean_folder(f) for f in (scope.get("folders") or []) if isinstance(f, str)]
    if scope.get("folder"):
        out.append(clean_folder(scope["folder"]))
    return [f for i, f in enumerate(out) if f and f not in out[:i]]


def clean_scope(scope: dict) -> dict:
    scope = scope if isinstance(scope, dict) else {}
    return {"folders": scope_folders(scope)[:20],
            "doc_ids": [str(x) for x in (scope.get("doc_ids") or []) if str(x)][:200],
            "tags": [str(x) for x in (scope.get("tags") or []) if str(x)][:10]}


def in_scope(doc: dict, scope: dict) -> bool:
    """文書が相談先に入るか: フォルダ（配下を含む）か個別に選んだ文書のどれか。タグはさらに絞り込む。"""
    folders = scope_folders(scope)
    ids = set(scope.get("doc_ids") or [])
    if folders or ids:
        hit = doc["id"] in ids or any(doc["folder"] == f or doc["folder"].startswith(f + "/") for f in folders)
        if not hit:
            return False
    tags = set(scope.get("tags") or [])
    return not tags or bool(tags & set(doc.get("tags") or []))


def describe_scope(scope: dict, docs: list[dict] | None = None) -> dict:
    """相談先の説明（画面・回答の記録用）: ラベル・文書数・区分ごとの数。"""
    docs = docs if docs is not None else db.list_docs()
    folders = scope_folders(scope)
    ids = list(scope.get("doc_ids") or [])
    titles = {d["id"]: d["title"] for d in docs}
    hits = [d for d in docs if in_scope(d, scope)]
    parts = []
    if folders:
        parts.append("📁 " + "、".join(folders[:3]) + (f" ほか {len(folders) - 3}" if len(folders) > 3 else ""))
    if ids:
        names = [titles.get(i, "（削除された文書）") for i in ids]
        parts.append("📄 " + "、".join(n[:20] for n in names[:2]) + (f" ほか {len(names) - 2}" if len(names) > 2 else ""))
    label = " ＋ ".join(parts) or "📚 すべての文書"
    if scope.get("tags"):
        label += "（タグ: " + "、".join(scope["tags"][:3]) + "）"
    return {"label": label, "docs": len(hits), "all": not (folders or ids or scope.get("tags")),
            "internal": sum(1 for d in hits if d["sensitivity"] != "public"),
            "public": sum(1 for d in hits if d["sensitivity"] == "public")}
