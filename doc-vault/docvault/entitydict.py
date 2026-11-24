"""エンティティ辞書（利用者が決めるエンティティ）と、グラフ画面での手直し。

LLM や語の共起で抽出したエンティティは、取り込みのたびに作り直される。利用者が決めたいこと
（正式名・別名・種別・要らない語・関係の追加と削除）は、抽出結果とは別にここへ保存し、
**グラフを組み立てるときに当てる**（graph.KnowledgeGraph）。だから
  - 読み込み済みの文書にも、LLM で読み直さずにすぐ反映される
  - 文書を再処理したり別の LLM で読み直したりしても、手直しは消えない
  - 辞書の行を消せば元に戻る

  entity_dict      … 名前（正式名）・種別・別名・説明・本文から探すか。別名の言及は正式名へまとめる。
                     「本文から探す」の語は、チャンクの本文を文字列で照合して、LLM が拾わなかった箇所も足す
  entity_excludes  … グラフから外す語（別名でまとめた後の名前でも、元の名前でも外す）
  entity_relations … 手で足した関係（hidden=0）と、抽出された関係を外したもの（hidden=1）

グラフ画面の「名前の変更・統合」「種別を変える」「グラフから外す」「関係を追加・外す」も、この 3 つの表への
書き込みになる（辞書の画面で一覧・取り消しができる）。照合キーは graph.norm_key（全角半角・空白・
大文字小文字・法人格の違いを無視）。

種別の一覧（entity_types。空なら標準の 13 種）も利用者が決める:
  - LLM で読み込むときの指示（「種別は次から選ぶ」）に使う。説明を書くと LLM への説明として添える
  - 名前の変更・まとめる（削除）は entity_type_map（元の種別 → 新しい種別）に残し、抽出済みの種別にも
    グラフを組み立てるときに当てる（読み直し不要）。辞書の行の種別も書き換える
  - 種別を足す・説明を変える・標準に戻すは、LLM に読み直させないと抽出済みの文書には効かない。
    そのため版（meta の entity_types_version）を上げ、グラフの読み込み結果に読んだときの版を残して、
    読み直していない文書を数えられるようにする
"""
from __future__ import annotations

import json
import re
import threading
import unicodedata
from dataclasses import dataclass, field

from . import db

MAX_NAME = 80
MAX_TYPE = 20
MAX_DESC = 400
MAX_ALIASES = 50
MIN_MATCH_LEN = 2          # 本文から探す語の最短の長さ（1 文字は誤検出が多すぎる）

_rev_lock = threading.Lock()
_rev = 0                   # 辞書を変えるたびに増やす（グラフのキャッシュの作り直し判定）


def revision() -> int:
    return _rev


def _bump() -> None:
    global _rev
    with _rev_lock:
        _rev += 1


def _nk(name: str) -> str:
    from .graph import norm_key        # graph が entitydict を読むので、循環を避けて呼ぶときに読む
    return norm_key(name)


def standard_types() -> list[str]:
    from .graph import ENTITY_TYPES
    return [t.strip() for t in ENTITY_TYPES.split("/") if t.strip()]


# ---------------------------------------------------------------- 種別の一覧

def entity_types() -> list[dict]:
    """種別の一覧 [{name, description}]（並び順どおり）。まだ決めていなければ標準の種別。"""
    rows = db.query("SELECT name,description FROM entity_types ORDER BY ord, name")
    if rows:
        return [{"name": r["name"], "description": r["description"]} for r in rows]
    return [{"name": t, "description": ""} for t in standard_types()]


def types_customized() -> bool:
    return db.one("SELECT 1 FROM entity_types LIMIT 1") is not None


def types_version() -> int:
    row = db.one("SELECT value FROM meta WHERE key='entity_types_version'")
    try:
        return int(row["value"]) if row else 0
    except ValueError:
        return 0


def _version_stmt() -> tuple:
    return ("INSERT OR REPLACE INTO meta(key,value) VALUES('entity_types_version',?)", (str(types_version() + 1),))


def types_prompt() -> str:
    """LLM への「種別は次から選ぶ」。説明があれば行を分けて添える（標準のままなら従来と同じ 1 行）。"""
    types = entity_types()
    line = " / ".join(t["name"] for t in types)
    notes = [f"  - {t['name']}: {t['description']}" for t in types if t["description"]]
    return line + ("\n" + "\n".join(notes) if notes else "")


def type_map() -> dict[str, str]:
    return {r["src"]: r["dst"] for r in db.query("SELECT src,dst FROM entity_type_map")}


def _ensure_types() -> list[tuple]:
    """標準の種別のまま初めて変えるときに、標準の一覧を表へ書き出す文。"""
    if types_customized():
        return []
    return [("INSERT INTO entity_types(name,description,ord,updated_at) VALUES(?,?,?,?)", (t, "", i, db.now()))
            for i, t in enumerate(standard_types())]


def _clean_type(value) -> str:
    return clean_name(value, MAX_TYPE).replace("/", "・")


def add_type(name: str, description: str = "") -> dict:
    name = _clean_type(name)
    if not name:
        raise ValueError("種別の名前を入れてください")
    if any(t["name"] == name for t in entity_types()):
        raise ValueError(f"種別「{name}」は既にあります")
    desc = str(description or "").strip()[:200]
    order = len(entity_types())
    db.write_many(_ensure_types() + [
        ("INSERT INTO entity_types(name,description,ord,updated_at) VALUES(?,?,?,?)", (name, desc, order, db.now())),
        ("DELETE FROM entity_type_map WHERE src=?", (name,)),       # まとめた種別を足し直したら付け替えをやめる
        _version_stmt()])
    _bump()
    return {"name": name, "description": desc}


def _retarget(old: str, new: str) -> list[tuple]:
    """種別 old を new へ付け替える文（抽出済みの種別・前の付け替え・辞書の行）。"""
    return [("UPDATE entity_type_map SET dst=? WHERE dst=?", (new, old)),
            ("DELETE FROM entity_type_map WHERE src=dst", ()),
            ("INSERT OR REPLACE INTO entity_type_map(src,dst) VALUES(?,?)", (old, new)),
            ("DELETE FROM entity_type_map WHERE src=?", (new,)),
            ("UPDATE entity_dict SET type=? WHERE type=?", (new, old))]


def update_type(old: str, name: str, description: str | None = None) -> dict:
    """種別の名前・説明を変える。名前の変更は抽出済みの種別にもすぐ当てる（説明の変更は読み直しで効く）。"""
    types = entity_types()
    cur = next((t for t in types if t["name"] == old), None)
    if cur is None:
        raise ValueError(f"種別「{old}」が見つかりません")
    name = _clean_type(name) or old
    if name != old and any(t["name"] == name for t in types):
        raise ValueError(f"種別「{name}」は既にあります（まとめるときは「{old}」の削除でまとめ先に選んでください）")
    desc = cur["description"] if description is None else str(description).strip()[:200]
    stmts = _ensure_types()
    stmts.append(("UPDATE entity_types SET name=?, description=?, updated_at=? WHERE name=?",
                  (name, desc, db.now(), old)))
    if name != old:
        stmts += _retarget(old, name)
    if desc != cur["description"]:
        stmts.append(_version_stmt())
    db.write_many(stmts)
    _bump()
    return {"name": name, "description": desc}


def delete_type(name: str, into: str) -> None:
    """種別を一覧から外し、その種別のエンティティを into（一覧にある種別）へまとめる。

    一覧に無い種別（LLM が一覧の外から付けた種別）も、同じようにまとめ先へ付け替えられる。
    """
    name = clean_name(name, MAX_TYPE)
    if not name:
        raise ValueError("種別を指定してください")
    types = entity_types()
    if into == name or not any(t["name"] == into for t in types):
        raise ValueError("まとめ先の種別を一覧から選んでください")
    stmts = _ensure_types() + [("DELETE FROM entity_types WHERE name=?", (name,))] \
        if any(t["name"] == name for t in types) else []
    db.write_many(stmts + _retarget(name, into))
    _bump()


def reset_types() -> None:
    """標準の種別に戻す（付け替えもやめる。辞書の行に決めた種別はそのまま）。"""
    db.write_many([("DELETE FROM entity_types", ()), ("DELETE FROM entity_type_map", ()), _version_stmt()])
    _bump()


def stale_readings() -> list[dict]:
    """種別を足す・説明を変える前に LLM で作ったグラフ（使用中のもの）。読み直すと新しい種別で抽出する。"""
    version = types_version()
    if version == 0:
        return []
    out = []
    for r in db.query("SELECT r.id,r.doc_id,r.label,r.data,d.title FROM readings r JOIN documents d ON d.id=r.doc_id "
                      "WHERE r.stage='graph' AND r.active=1 AND r.provider NOT IN ('terms','manual') "
                      "AND r.mode!='terms' ORDER BY d.title"):
        try:
            seen = int(json.loads(r["data"] or "{}").get("types_version") or 0)
        except (ValueError, TypeError):
            seen = 0
        if seen < version:
            out.append({"reading_id": r["id"], "doc_id": r["doc_id"], "title": r["title"], "label": r["label"]})
    return out


def clean_name(value, limit: int = MAX_NAME) -> str:
    return " ".join(str(value or "").split())[:limit]


def split_aliases(value) -> list[str]:
    """別名の入力（リスト、または「、」「|」「;」区切りの文字列）を名前のリストにする。"""
    if isinstance(value, (list, tuple)):
        items = value
    else:
        items = re.split(r"[|;；、\n]", str(value or ""))
    return [clean_name(v) for v in items if clean_name(v)]


def _clean_aliases(values, own_key: str) -> list[str]:
    out, seen = [], {own_key}
    for v in split_aliases(values):
        k = _nk(v)
        if k and k not in seen:
            seen.add(k)
            out.append(v)
    return out[:MAX_ALIASES]


# ---------------------------------------------------------------- 読み出し

def _entry(row) -> dict:
    return {"key": row["key"], "name": row["name"], "type": row["type"],
            "aliases": json.loads(row["aliases"] or "[]"), "description": row["description"],
            "match": bool(row["match"]), "updated_at": row["updated_at"]}


def entries() -> list[dict]:
    return [_entry(r) for r in db.query("SELECT * FROM entity_dict ORDER BY name")]


def get(key: str) -> dict | None:
    row = db.one("SELECT * FROM entity_dict WHERE key=?", (key,))
    return _entry(row) if row else None


def excludes() -> list[dict]:
    return [{"key": r["key"], "name": r["name"]}
            for r in db.query("SELECT key,name FROM entity_excludes ORDER BY name")]


def relations() -> list[dict]:
    return [{"source": r["source"], "target": r["target"], "source_name": r["source_name"],
             "target_name": r["target_name"], "description": r["description"], "strength": r["strength"],
             "hidden": bool(r["hidden"]), "updated_at": r["updated_at"]}
            for r in db.query("SELECT * FROM entity_relations ORDER BY updated_at DESC")]


def state() -> dict:
    """辞書の画面用（登録したエンティティ・外す語・手で足した/外した関係・種別の一覧と候補）。"""
    ents = entries()
    type_list = entity_types()
    types = [t["name"] for t in type_list]
    for e in ents:
        if e["type"] and e["type"] not in types:
            types.append(e["type"])
    stale = stale_readings()
    return {"entries": ents, "excludes": excludes(), "relations": relations(), "types": types,
            "type_list": type_list, "types_customized": types_customized(), "types_version": types_version(),
            "type_map": type_map(), "stale": {"count": len(stale), "docs": stale[:20]}}


def _owners(skip: set[str] = frozenset()) -> dict[str, dict]:
    """照合キー → それを名前か別名に持つ辞書の行（skip の行は除く）。"""
    out: dict[str, dict] = {}
    for e in entries():
        if e["key"] in skip:
            continue
        out[e["key"]] = e
        for a in e["aliases"]:
            out.setdefault(_nk(a), e)
    return out


# ---------------------------------------------------------------- 辞書の編集

def _write_entry(e: dict, *, old_key: str | None = None, extra: list[tuple] | None = None) -> None:
    stmts = list(extra or [])
    if old_key and old_key != e["key"]:
        stmts.append(("DELETE FROM entity_dict WHERE key=?", (old_key,)))
    stmts.append(("INSERT OR REPLACE INTO entity_dict(key,name,type,aliases,description,match,updated_at) "
                  "VALUES(?,?,?,?,?,?,?)",
                  (e["key"], e["name"], e.get("type") or "", json.dumps(e.get("aliases") or [], ensure_ascii=False),
                   e.get("description") or "", 1 if e.get("match") else 0, db.now())))
    db.write_many(stmts)
    _bump()


def save_entry(data: dict, old_key: str | None = None) -> dict:
    """辞書の画面から 1 件を登録・更新する（old_key は編集前の行）。名前・別名が他の行と重なるときは断る。"""
    name = clean_name(data.get("name"))
    key = _nk(name)
    if not key:
        raise ValueError("名前を入れてください")
    old_key = old_key or None
    if old_key and not get(old_key):
        raise ValueError("編集中の行が見つかりません（ほかの画面で消された可能性があります）")
    aliases = _clean_aliases(data.get("aliases") or [], key)
    owners = _owners({old_key} if old_key else set())
    if key in owners:
        other = owners[key]
        raise ValueError(f"「{name}」は登録済みです" + ("" if other["key"] == key else f"（「{other['name']}」の別名）"))
    for a in aliases:
        if _nk(a) in owners:
            raise ValueError(f"別名「{a}」は「{owners[_nk(a)]['name']}」の名前か別名として登録済みです")
    e = {"key": key, "name": name, "type": clean_name(data.get("type"), MAX_TYPE), "aliases": aliases,
         "description": str(data.get("description") or "").strip()[:MAX_DESC],
         "match": bool(data.get("match", True))}
    # 辞書に登録した語は、外す語の一覧から消す（登録した意図を優先する）
    _write_entry(e, old_key=old_key, extra=[("DELETE FROM entity_excludes WHERE key=?", (key,))])
    return e


def delete_entry(key: str) -> None:
    db.write("DELETE FROM entity_dict WHERE key=?", (key,))
    _bump()


def bulk_add(text: str) -> dict:
    """まとめて登録。1 行に 1 件: 名前,種別,別名1|別名2（タブ区切りでもよい。Excel から貼り付けられる）。

    既にある名前は、種別（書いてあれば）と別名を足す。名前・別名がほかの行と重なる行は飛ばして理由を返す。
    """
    added, updated, errors = 0, 0, []
    for no, line in enumerate(str(text or "").splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        cols = [c.strip() for c in (line.split("\t") if "\t" in line else re.split(r"[,，]", line, maxsplit=2))]
        name = clean_name(cols[0])
        if not name or name in ("名前", "name"):
            continue
        typ = clean_name(cols[1], MAX_TYPE) if len(cols) > 1 else ""
        aliases = split_aliases(cols[2]) if len(cols) > 2 else []
        cur = get(_nk(name))
        try:
            if cur:
                save_entry({**cur, "type": typ or cur["type"], "aliases": cur["aliases"] + aliases}, old_key=cur["key"])
                updated += 1
            else:
                save_entry({"name": name, "type": typ, "aliases": aliases, "match": True})
                added += 1
        except ValueError as e:
            errors.append(f"{no} 行目: {e}")
    return {"added": added, "updated": updated, "errors": errors}


def set_excludes(names) -> None:
    """外す語の一覧を置き換える（辞書の画面のテキスト欄。1 行に 1 語）。"""
    rows, seen = [], set()
    for n in split_aliases(names if isinstance(names, (list, tuple)) else str(names or "").splitlines()):
        k = _nk(n)
        if k and k not in seen:
            seen.add(k)
            rows.append((k, n, db.now()))
    db.write_many([("DELETE FROM entity_excludes", ())]
                  + [("INSERT INTO entity_excludes(key,name,updated_at) VALUES(?,?,?)", r) for r in rows])
    _bump()


# ---------------------------------------------------------------- 関係

def _pair(a: str, b: str) -> tuple[str, str]:
    return (a, b) if a <= b else (b, a)


def add_relation(a_name: str, b_name: str, description: str = "", strength: int = 7) -> dict:
    a_name, b_name = clean_name(a_name), clean_name(b_name)
    ka, kb = _nk(a_name), _nk(b_name)
    if not ka or not kb:
        raise ValueError("両端のエンティティの名前を入れてください")
    if ka == kb:
        raise ValueError("同じエンティティどうしは結べません")
    try:
        strength = max(1, min(10, int(strength)))
    except (TypeError, ValueError):
        strength = 7
    (s, t) = _pair(ka, kb)
    sn, tn = (a_name, b_name) if s == ka else (b_name, a_name)
    db.write("INSERT OR REPLACE INTO entity_relations(source,target,source_name,target_name,description,strength,"
             "hidden,updated_at) VALUES(?,?,?,?,?,?,0,?)",
             (s, t, sn, tn, str(description or "").strip()[:300], strength, db.now()))
    _bump()
    return {"source": s, "target": t}


def hide_relation(a_key: str, b_key: str, a_name: str = "", b_name: str = "") -> None:
    """抽出された関係（手で足した関係も）をグラフから外す。"""
    if not a_key or not b_key or a_key == b_key:
        raise ValueError("関係の両端を指定してください")
    (s, t) = _pair(a_key, b_key)
    sn, tn = (a_name, b_name) if s == a_key else (b_name, a_name)
    db.write("INSERT OR REPLACE INTO entity_relations(source,target,source_name,target_name,description,strength,"
             "hidden,updated_at) VALUES(?,?,?,?,'',0,1,?)", (s, t, clean_name(sn) or s, clean_name(tn) or t, db.now()))
    _bump()


def delete_relation(a_key: str, b_key: str) -> None:
    """手で足した関係・外した関係の取り消し。"""
    (s, t) = _pair(a_key, b_key)
    db.write("DELETE FROM entity_relations WHERE source=? AND target=?", (s, t))
    _bump()


# ---------------------------------------------------------------- グラフ画面の手直し

def rename(key: str, current_name: str, new_name: str) -> dict:
    """エンティティの名前を変える。ほかのエンティティ（名前か別名）と同じ名前にすると、そちらへ統合する。

    元の名前は別名として残すので、文書の言及（LLM の抽出結果）は新しい名前へまとまる。
    返り値: {"key": 変更後の照合キー, "name": 表示名, "merged": 統合したか}
    """
    new_name = clean_name(new_name)
    nk = _nk(new_name)
    if not nk:
        raise ValueError("新しい名前を入れてください")
    cur = get(key)
    owner = _owners({key}).get(nk)
    if nk == key or (cur and nk in {_nk(a) for a in cur["aliases"]}):
        # 表記だけ変える（同じ照合キー）、または自分の別名を正式名にする
        e = cur or {"key": key, "name": current_name, "type": "", "aliases": [], "description": "", "match": False}
        aliases = [a for a in e["aliases"] if _nk(a) != nk]
        if nk != key:
            aliases.append(e["name"])
        _write_entry({**e, "key": nk, "name": new_name, "aliases": _clean_aliases(aliases, nk)}, old_key=key)
        return {"key": nk, "name": new_name, "merged": False}
    target_key = owner["key"] if owner else nk
    merged = owner is not None or _has_mentions(nk)
    e = _fold(key, current_name, target_key, new_name if not owner else owner["name"])
    return {"key": e["key"], "name": e["name"], "merged": merged}


def _has_mentions(key: str) -> bool:
    return db.one("SELECT 1 FROM entity_mentions WHERE entity_key=? LIMIT 1", (key,)) is not None


def _fold(src_key: str, src_name: str, dst_key: str, dst_name: str) -> dict:
    """src を dst へまとめる（src の名前・別名を dst の別名にし、src の行は消す）。"""
    if src_key == dst_key:
        raise ValueError("同じエンティティです")
    src = get(src_key)
    dst = get(dst_key) or {"key": dst_key, "name": dst_name, "type": "", "aliases": [], "description": "",
                           "match": False}
    aliases = dst["aliases"] + [src_name] + ([src["name"], *src["aliases"]] if src else [])
    e = {**dst, "aliases": _clean_aliases(aliases, dst_key)}
    if src:
        e["type"] = e["type"] or src["type"]
        e["description"] = e["description"] or src["description"]
        e["match"] = e["match"] or src["match"]
    extra = [("DELETE FROM entity_dict WHERE key=?", (src_key,))] if src else []
    _write_entry(e, extra=extra)
    return e


def set_type(key: str, name: str, type_: str) -> dict:
    type_ = clean_name(type_, MAX_TYPE)
    if not type_:
        raise ValueError("種別を入れてください")
    e = get(key) or {"key": key, "name": clean_name(name) or key, "type": "", "aliases": [], "description": "",
                     "match": False}
    e["type"] = type_
    _write_entry(e)
    return e


def exclude_entity(key: str, name: str) -> None:
    """グラフから外す。辞書に登録してあれば、その行を消して名前と別名をすべて外す語にする。"""
    e = get(key)
    names = [clean_name(name) or key] + ([e["name"], *e["aliases"]] if e else [])
    rows, seen = [], set()
    for n in names:
        k = _nk(n)
        if k and k not in seen:
            seen.add(k)
            rows.append(("INSERT OR REPLACE INTO entity_excludes(key,name,updated_at) VALUES(?,?,?)", (k, n, db.now())))
    if key not in seen:
        rows.append(("INSERT OR REPLACE INTO entity_excludes(key,name,updated_at) VALUES(?,?,?)",
                     (key, clean_name(name) or key, db.now())))
    if e:
        rows.append(("DELETE FROM entity_dict WHERE key=?", (key,)))
    db.write_many(rows)
    _bump()


# ---------------------------------------------------------------- グラフを組み立てるときに当てる

@dataclass
class Rules:
    entries: dict[str, dict] = field(default_factory=dict)
    alias: dict[str, str] = field(default_factory=dict)          # 別名の照合キー → 辞書の行の key
    excludes: set[str] = field(default_factory=set)
    relations: list[dict] = field(default_factory=list)          # 手で足した関係（照合キーは canon 済み）
    hidden: set[tuple[str, str]] = field(default_factory=set)    # 外した関係（canon 済み・並べ替え済み）
    type_map: dict[str, str] = field(default_factory=dict)       # 名前を変えた・まとめた種別 → 今の種別

    def canon(self, key: str) -> str:
        return self.alias.get(key, key)

    def map_type(self, type_: str) -> str:
        for _ in range(8):                       # 付け替えは書き込み時に畳んでいるが、念のため連鎖もたどる
            nxt = self.type_map.get(type_)
            if not nxt or nxt == type_:
                break
            type_ = nxt
        return type_

    def excluded(self, raw_key: str, key: str | None = None) -> bool:
        return raw_key in self.excludes or (key or self.canon(raw_key)) in self.excludes

    @property
    def match_entries(self) -> list[dict]:
        return [e for e in self.entries.values() if e["match"] and e["key"] not in self.excludes]


def load_rules() -> Rules:
    r = Rules()
    for e in entries():
        r.entries[e["key"]] = e
    for e in r.entries.values():
        for a in e["aliases"]:
            k = _nk(a)
            if k and k not in r.entries:          # ほかの行の正式名は別名にしない（行が優先）
                r.alias.setdefault(k, e["key"])
    r.excludes = {x["key"] for x in excludes()}
    r.type_map = type_map()
    for rel in relations():
        a, b = r.canon(rel["source"]), r.canon(rel["target"])
        if a == b:
            continue
        if rel["hidden"]:
            r.hidden.add(_pair(a, b))
        else:
            r.relations.append({**rel, "source": a, "target": b})
    return r


_SENT_RE = re.compile(r"[^。！？!?\n]+[。！？!?]?")
_ASCII_EDGE = re.compile(r"[A-Za-z0-9]")


def _trie_regex(words: list[str]) -> str:
    """語の一覧を、共通の頭をまとめた正規表現にする（語を | で並べるより何倍も速い）。

    空白は「空白 0 個以上」、英数字で終わる語は後ろに英数字が続かないとき（単語の区切り）だけ一致させる。
    """
    trie: dict = {}
    for w in words:
        node = trie
        for ch in w:
            node = node.setdefault(ch, {})
        node[""] = True

    def emit(node: dict, prev: str) -> str:
        alts = [(r"\s*" if ch == " " else re.escape(ch)) + emit(node[ch], ch) for ch in sorted(k for k in node if k)]
        if "" in node:
            alts.append(r"(?![A-Za-z0-9])" if _ASCII_EDGE.match(prev) else "")
        if len(alts) == 1:
            return alts[0]
        return "(?:" + "|".join(alts) + ")"
    return emit(trie, "") if trie else ""


def _terms_regex(surfaces: list[str]) -> re.Pattern:
    ascii_head = [w for w in surfaces if _ASCII_EDGE.match(w)]
    other = [w for w in surfaces if not _ASCII_EDGE.match(w)]
    parts = []
    if ascii_head:
        parts.append(r"(?<![A-Za-z0-9])" + _trie_regex(ascii_head))
    if other:
        parts.append(_trie_regex(other))
    return re.compile("|".join(f"(?:{p})" for p in parts), re.IGNORECASE)


def norm_text(text: str) -> str:
    return unicodedata.normalize("NFKC", text or "")


_hits_lock = threading.Lock()
_hits_cache: dict[tuple, dict] = {}


def text_hits(rules: Rules) -> dict[str, tuple[str, str, dict[str, list[tuple[int, int]]]]]:
    """「本文から探す」語を全チャンクの本文から探す（文書・辞書が変わるまでキャッシュ）。

    返り値: チャンク ID → (文書 ID, 本文（NFKC）, {辞書の行の key: [(開始, 終了)]})。見つかったチャンクだけ。
    """
    terms: dict[str, str] = {}             # 照合キー → 辞書の行の key
    for e in rules.match_entries:
        for t in [e["name"], *e["aliases"]]:
            t = " ".join(norm_text(t).split())
            if len(t.replace(" ", "")) >= MIN_MATCH_LEN:
                terms.setdefault(_nk(t), e["key"])
                terms.setdefault("\0" + t.casefold(), e["key"])
    if not terms:
        return {}
    cache_key = (db.revision(), revision(), str(db.DATA_DIR))
    with _hits_lock:
        if cache_key in _hits_cache:
            return _hits_cache[cache_key]
    rx = _terms_regex(sorted({k[1:] for k in terms if k.startswith("\0")}))
    out: dict[str, tuple[str, str, dict]] = {}
    for row in db.query("SELECT id,doc_id,text FROM chunks"):
        text = norm_text(row["text"])
        found: dict[str, list[tuple[int, int]]] = {}
        for m in rx.finditer(text):
            s = m.group(0)
            key = terms.get("\0" + " ".join(s.split()).casefold()) or terms.get(_nk(s))
            if key:
                found.setdefault(key, []).append(m.span())
        if found:
            out[row["id"]] = (row["doc_id"], text, found)
    with _hits_lock:
        if len(_hits_cache) > 4:
            _hits_cache.clear()
        _hits_cache[cache_key] = out
    return out


def sentence_spans(text: str) -> list[tuple[int, int]]:
    return [m.span() for m in _SENT_RE.finditer(text)]
