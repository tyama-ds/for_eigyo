"""読み込み結果（どの LLM で作った要約・グラフ・埋め込みか）の記録と切り替え。

文書の要約・ナレッジグラフ・埋め込みは、作った LLM（接続先 × モデル × 方式）ごとに1件の「読み込み結果」
として残す。同じ LLM で読み込み直すとその結果を作り直し、別の LLM で読み込むと結果が増える（前の結果は
消えない）。文書ごと・段階ごとに使う結果を1つ選び（active）、検索・グラフ・相談はそれを使う。

  - 要約   … readings.data に {summary, keywords}。選ぶと documents.summary / keywords へ写す
  - グラフ … entity_mentions / relation_mentions の reading_id。チャンクごとの記録は graph_chunks
  - 埋め込み … vectors の space（プロファイル:埋め込みモデル）ごと。質問も同じ空間で埋め込む必要があるため、
              検索に使えるのは設定中の埋め込みモデルの空間だけ（ほかの空間は保持のみ）
"""
from __future__ import annotations

import json
from urllib.parse import urlparse

from . import db

STAGES = ("summary", "graph", "embed")
STAGE_LABEL = {"summary": "文書要約", "graph": "ナレッジグラフ", "embed": "埋め込み"}
GRAPH_MODES = ("standard", "fast", "terms")
MODE_LABEL = {"standard": "標準", "fast": "高速", "terms": "語の共起（LLM なし）"}
STATUS_LABEL = {"running": "処理中", "done": "完了", "partial": "一部", "paused": "一時停止", "error": "中止"}


# ---------------------------------------------------------------- LLM の識別

def _host(url: str) -> str:
    if not url:
        return ""
    try:
        u = urlparse(url if "//" in url else f"http://{url}")
        return u.netloc or url
    except ValueError:
        return url


def ingest_model(p: dict) -> str:
    """取り込みに実際に使うモデル名（llm._payload と同じ決め方。Azure はデプロイ名）。"""
    if p.get("provider") != "azure" and p.get("ingest_model"):
        return p["ingest_model"]
    return p.get("model") or ""


def identity(cfg: dict, profile: str) -> dict:
    p = cfg["profiles"][profile]
    return {"profile": profile, "provider": p.get("provider", ""), "model": ingest_model(p),
            "endpoint": p.get("base_url") or ""}


def terms_identity() -> dict:
    return {"profile": "", "provider": "terms", "model": "", "endpoint": ""}


def embed_identity(cfg: dict, profile: str, space: str) -> dict:
    p = cfg["profiles"][profile]
    return {"profile": profile, "provider": p.get("embed_provider", ""), "model": p.get("embed_model", ""),
            "endpoint": p.get("embed_base_url") or p.get("base_url") or "", "space": space}


def key_of(ident: dict, stage: str, mode: str = "") -> str:
    if stage == "embed":
        return ident["space"]
    if ident["provider"] == "terms":
        return "terms"
    return "|".join([ident["profile"], ident["provider"], ident["model"], _host(ident["endpoint"]),
                     mode if stage == "graph" else ""])


def label_of(ident: dict, stage: str, mode: str = "") -> str:
    if ident["provider"] == "terms":
        return MODE_LABEL["terms"]
    where = "ローカル" if ident["profile"] == "local" else "クラウド"
    host = _host(ident["endpoint"]) if ident["provider"] != "anthropic" else ""
    label = f"{ident['model'] or '（モデル未設定）'}（{where}{'・' + host if host else ''}）"
    if stage == "graph" and mode == "fast":
        label += "・高速"
    return label


# ---------------------------------------------------------------- 読み書き

def _row(r) -> dict:
    d = dict(r)
    for key, empty in (("data", {}), ("warnings", [])):
        try:
            d[key] = json.loads(d.get(key) or json.dumps(empty))
        except ValueError:
            d[key] = empty
    d["active"] = bool(d.get("active"))
    return d


def get(rid: str) -> dict | None:
    r = db.one("SELECT * FROM readings WHERE id=?", (rid,))
    return _row(r) if r else None


def find(doc_id: str, stage: str, llm_key: str) -> dict | None:
    r = db.one("SELECT * FROM readings WHERE doc_id=? AND stage=? AND llm_key=?", (doc_id, stage, llm_key))
    return _row(r) if r else None


def active(doc_id: str, stage: str) -> dict | None:
    r = db.one("SELECT * FROM readings WHERE doc_id=? AND stage=? AND active=1", (doc_id, stage))
    return _row(r) if r else None


def list_for(doc_id: str) -> list[dict]:
    out = []
    for r in db.query("SELECT * FROM readings WHERE doc_id=? ORDER BY stage, created_at DESC", (doc_id,)):
        d = _row(r)
        if d["stage"] == "graph":
            d["chunks"] = chunk_counts(d["id"])
            d["entities"] = db.one("SELECT COUNT(DISTINCT entity_key) n FROM entity_mentions WHERE reading_id=?",
                                   (d["id"],))["n"]
            d["relations"] = db.one("SELECT COUNT(*) n FROM relation_mentions WHERE reading_id=?",
                                    (d["id"],))["n"]
        out.append(d)
    return out


def actives_by_doc() -> dict[str, dict[str, dict]]:
    """{doc_id: {stage: 読み込み結果（要約の本文は除く）}}（文書一覧の表示用）。"""
    out: dict[str, dict[str, dict]] = {}
    for r in db.query("SELECT id,doc_id,stage,label,mode,status,model,profile,provider,updated_at "
                      "FROM readings WHERE active=1"):
        out.setdefault(r["doc_id"], {})[r["stage"]] = dict(r)
    counts = {}
    for r in db.query("SELECT doc_id, COUNT(*) n FROM readings GROUP BY doc_id"):
        counts[r["doc_id"]] = r["n"]
    for doc_id, n in counts.items():
        out.setdefault(doc_id, {})["count"] = n
    return out


def chunk_counts(rid: str) -> dict[str, int]:
    return {r["status"]: r["n"] for r in db.query(
        "SELECT status, COUNT(*) n FROM graph_chunks WHERE reading_id=? GROUP BY status", (rid,))}


def chunk_log(rid: str) -> list[dict]:
    rows = db.query("SELECT g.*, c.ord, c.locator FROM graph_chunks g LEFT JOIN chunks c ON c.id=g.chunk_id "
                    "WHERE g.reading_id=? ORDER BY c.ord", (rid,))
    return [dict(r) for r in rows]


def start(doc_id: str, stage: str, ident: dict, mode: str = "", *, opts: dict | None = None,
          restart: bool = True) -> dict:
    """読み込みを始める（同じ LLM・方式の結果があれば作り直し、無ければ新しく作る）。

    その段階で使っている結果がまだ無ければ、作り始めた結果をすぐ「使用中」にする（グラフは途中まで
    の結果も検索に出る）。使っている結果があれば、新しい結果は完了したときに切り替える。
    """
    key = key_of(ident, stage, mode)
    t = db.now()
    old = find(doc_id, stage, key)
    data = dict(old["data"]) if old else {}
    if opts is not None:
        data["opts"] = {k: v for k, v in opts.items() if k in ("profile", "model", "graph_mode")}
    if old:
        rid = old["id"]
        if restart:
            clear_graph(rid)
            data = {"opts": data.get("opts", {})}
        db.write("UPDATE readings SET status='running', label=?, data=?, updated_at=? WHERE id=?",
                 (label_of(ident, stage, mode), json.dumps(data, ensure_ascii=False), t, rid))
    else:
        rid = db.new_id("r")
        is_first = active(doc_id, stage) is None
        db.write("INSERT INTO readings(id,doc_id,stage,llm_key,label,profile,provider,model,endpoint,mode,"
                 "status,active,data,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (rid, doc_id, stage, key, label_of(ident, stage, mode), ident["profile"], ident["provider"],
                  ident["model"], _host(ident["endpoint"]), mode, "running", int(is_first),
                  json.dumps(data, ensure_ascii=False), t, t))
        if is_first:
            refresh_flags(doc_id)
    return get(rid)


def finish(rid: str, status: str, *, data: dict | None = None, warnings: list[str] | None = None,
           seconds: float | None = None, activate: bool = True) -> dict | None:
    r = get(rid)
    if not r:
        return None
    merged = dict(r["data"])
    merged.update(data or {})
    db.write("UPDATE readings SET status=?, data=?, warnings=?, seconds=?, updated_at=? WHERE id=?",
             (status, json.dumps(merged, ensure_ascii=False), json.dumps(warnings or [], ensure_ascii=False),
              round(seconds if seconds is not None else r["seconds"], 1), db.now(), rid))
    if status in ("done", "partial") and (activate or r["active"]):
        set_active(rid)
    else:
        refresh_flags(r["doc_id"])
    return get(rid)


def record_done(doc_id: str, stage: str, ident: dict, data: dict, *, mode: str = "", seconds: float = 0,
                activate: bool = True) -> dict:
    """要約・埋め込みのように一度に作る結果を、完成した形で登録する。"""
    r = start(doc_id, stage, ident, mode, restart=False)
    return finish(r["id"], "done", data=data, seconds=seconds, activate=activate)


def set_active(rid: str) -> dict | None:
    r = get(rid)
    if not r:
        return None
    doc_id, stage = r["doc_id"], r["stage"]
    with db._write_lock:
        db.write_many([("UPDATE readings SET active=0 WHERE doc_id=? AND stage=?", (doc_id, stage)),
                       ("UPDATE readings SET active=1 WHERE id=?", (rid,))])
        if stage == "summary":
            db.update_doc(doc_id, summary=str(r["data"].get("summary") or ""),
                          keywords=list(r["data"].get("keywords") or []))
        elif stage == "graph":
            restore_chunk_keywords(doc_id)
    db.bump_revision()
    refresh_flags(doc_id)
    return get(rid)


def restore_chunk_keywords(doc_id: str) -> None:
    """チャンクのキーワード（DecisionRAG の節の説明に使う）を、使用中のグラフの結果に合わせる。"""
    act = active(doc_id, "graph")
    if not act:
        db.write("UPDATE chunks SET keywords='[]' WHERE doc_id=?", (doc_id,))
        return
    db.write("UPDATE chunks SET keywords=COALESCE((SELECT g.keywords FROM graph_chunks g "
             "WHERE g.reading_id=? AND g.chunk_id=chunks.id AND g.status='done'), '[]') WHERE doc_id=?",
             (act["id"], doc_id))


def clear_graph(rid: str) -> None:
    db.write_many([("DELETE FROM entity_mentions WHERE reading_id=?", (rid,)),
                   ("DELETE FROM relation_mentions WHERE reading_id=?", (rid,)),
                   ("DELETE FROM graph_chunks WHERE reading_id=?", (rid,))])


def delete(rid: str) -> dict | None:
    """読み込み結果を消す。使用中だったら、残りの中で新しい完了済みの結果へ切り替える。"""
    r = get(rid)
    if not r:
        return None
    doc_id, stage = r["doc_id"], r["stage"]
    if stage == "graph":
        clear_graph(rid)
    elif stage == "embed":
        space = r["data"].get("space") or r["llm_key"]
        db.write("DELETE FROM vectors WHERE doc_id=? AND space=?", (doc_id, space))
        from .retrieval import INDEX
        INDEX.drop_vectors(doc_id, space)
    db.write("DELETE FROM readings WHERE id=?", (rid,))
    if r["active"]:
        nxt = db.one("SELECT id FROM readings WHERE doc_id=? AND stage=? AND status IN ('done','partial') "
                     "ORDER BY updated_at DESC LIMIT 1", (doc_id, stage))
        if nxt:
            set_active(nxt["id"])
        else:
            if stage == "summary":
                db.update_doc(doc_id, summary="", keywords=[])
            elif stage == "graph":
                restore_chunk_keywords(doc_id)
            db.bump_revision()
    refresh_flags(doc_id)
    return r


def refresh_flags(doc_id: str) -> None:
    """文書一覧の表示用フラグ（graph / summary / vector）を、使用中の読み込み結果に合わせる。"""
    with db._write_lock:
        doc = db.get_doc(doc_id)
        if not doc:
            return
        flags = dict(doc["flags"])
        for stage, flag in (("graph", "graph"), ("summary", "summary"), ("embed", "vector")):
            act = active(doc_id, stage)
            for k in (flag, f"{flag}_label", f"{flag}_mode", f"{flag}_status"):
                flags.pop(k, None)
            if not act:
                continue
            # graph / summary は作ったプロファイル（local / cloud。LLM なしは terms）、vector は埋め込み空間
            flags[flag] = act["data"].get("space", act["llm_key"]) if stage == "embed" \
                else (act["profile"] or act["provider"])
            flags[f"{flag}_label"] = act["label"]
            flags[f"{flag}_status"] = act["status"]
            if stage == "graph":
                flags["graph_mode"] = act["mode"] or "standard"
        db.update_doc(doc_id, flags=flags)


def save_chunk(rid: str, doc_id: str, chunk_id: str, status: str, *, data: dict | None = None,
               finish: str = "", reason: str = "", attempts: int = 1, seconds: float = 0.0) -> None:
    """グラフ抽出の1チャンク分の記録（と抽出結果）を保存する。"""
    from .graph import mention_rows
    stmts = [("DELETE FROM entity_mentions WHERE reading_id=? AND chunk_id=?", (rid, chunk_id)),
             ("DELETE FROM relation_mentions WHERE reading_id=? AND chunk_id=?", (rid, chunk_id))]
    keywords: list = []
    n_ent = n_rel = 0
    if data:
        em, rm = mention_rows(doc_id, chunk_id, data, rid)
        stmts += [("INSERT INTO entity_mentions(entity_key,name,type,description,doc_id,chunk_id,reading_id) "
                   "VALUES(?,?,?,?,?,?,?)", row) for row in em]
        stmts += [("INSERT INTO relation_mentions(source,target,description,strength,doc_id,chunk_id,reading_id) "
                   "VALUES(?,?,?,?,?,?,?)", row) for row in rm]
        keywords = list(data.get("keywords") or [])
        n_ent, n_rel = len(em), len(rm)
    stmts.append(("INSERT OR REPLACE INTO graph_chunks(reading_id,chunk_id,doc_id,status,keywords,entities,"
                  "relations,finish,reason,attempts,seconds,at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                  (rid, chunk_id, doc_id, status, json.dumps(keywords, ensure_ascii=False), n_ent, n_rel,
                   finish[:40], reason[:500], attempts, round(seconds, 1), db.now())))
    if data and db.one("SELECT 1 FROM readings WHERE id=? AND active=1", (rid,)):
        stmts.append(("UPDATE chunks SET keywords=? WHERE id=?",
                      (json.dumps(keywords, ensure_ascii=False), chunk_id)))
    db.write_many(stmts)
