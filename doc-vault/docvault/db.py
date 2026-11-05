"""永続化（SQLite + 原本ファイル）。

data/ 以下:
  docvault.sqlite3  … 文書メタデータ・チャンク・ベクトル・グラフ・会話
  files/<id>.<ext>  … アップロードされた原本（ビューア・ダウンロード用）

共有運用を見据えて documents / chats に owner・visibility 列を持つ（現状は "local" 固定）。
接続はスレッドごとに持ち、書き込みはロックで直列化する（WAL）。
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
DATA_DIR = BASE / "data"

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  filename TEXT NOT NULL,
  ext TEXT NOT NULL,
  size INTEGER NOT NULL DEFAULT 0,
  sha256 TEXT NOT NULL,
  folder TEXT NOT NULL DEFAULT '',
  tags TEXT NOT NULL DEFAULT '[]',
  sensitivity TEXT NOT NULL DEFAULT 'internal',
  owner TEXT NOT NULL DEFAULT 'local',
  visibility TEXT NOT NULL DEFAULT 'private',
  status TEXT NOT NULL DEFAULT 'queued',
  stage TEXT NOT NULL DEFAULT '',
  progress REAL NOT NULL DEFAULT 0,
  error TEXT NOT NULL DEFAULT '',
  warnings TEXT NOT NULL DEFAULT '[]',
  flags TEXT NOT NULL DEFAULT '{}',
  pages INTEGER,
  chars INTEGER NOT NULL DEFAULT 0,
  summary TEXT NOT NULL DEFAULT '',
  keywords TEXT NOT NULL DEFAULT '[]',
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_docs_folder ON documents(folder);
CREATE INDEX IF NOT EXISTS idx_docs_sha ON documents(sha256);

CREATE TABLE IF NOT EXISTS chunks (
  id TEXT PRIMARY KEY,
  doc_id TEXT NOT NULL,
  ord INTEGER NOT NULL,
  locator TEXT NOT NULL DEFAULT '',
  heading TEXT NOT NULL DEFAULT '',
  page INTEGER,
  text TEXT NOT NULL,
  keywords TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS idx_chunks_doc ON chunks(doc_id, ord);

CREATE TABLE IF NOT EXISTS vectors (
  chunk_id TEXT NOT NULL,
  space TEXT NOT NULL,
  doc_id TEXT NOT NULL,
  vec BLOB NOT NULL,
  PRIMARY KEY (chunk_id, space)
);
CREATE INDEX IF NOT EXISTS idx_vectors_doc ON vectors(doc_id);

CREATE TABLE IF NOT EXISTS entity_mentions (
  entity_key TEXT NOT NULL,
  name TEXT NOT NULL,
  type TEXT NOT NULL DEFAULT '',
  description TEXT NOT NULL DEFAULT '',
  doc_id TEXT NOT NULL,
  chunk_id TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_em_key ON entity_mentions(entity_key);
CREATE INDEX IF NOT EXISTS idx_em_doc ON entity_mentions(doc_id);
CREATE INDEX IF NOT EXISTS idx_em_chunk ON entity_mentions(chunk_id);

CREATE TABLE IF NOT EXISTS relation_mentions (
  source TEXT NOT NULL,
  target TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  strength INTEGER NOT NULL DEFAULT 5,
  doc_id TEXT NOT NULL,
  chunk_id TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rm_doc ON relation_mentions(doc_id);
CREATE INDEX IF NOT EXISTS idx_rm_pair ON relation_mentions(source, target);

CREATE TABLE IF NOT EXISTS community_cache (
  signature TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  summary TEXT NOT NULL,
  sensitivity TEXT NOT NULL,
  created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS chats (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL DEFAULT '',
  owner TEXT NOT NULL DEFAULT 'local',
  mode TEXT NOT NULL DEFAULT 'auto',
  scope TEXT NOT NULL DEFAULT '{}',
  touched_internal INTEGER NOT NULL DEFAULT 0,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
  id TEXT PRIMARY KEY,
  chat_id TEXT NOT NULL,
  role TEXT NOT NULL,
  content TEXT NOT NULL,
  meta TEXT NOT NULL DEFAULT '{}',
  created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_msg_chat ON messages(chat_id, created_at);

CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

_local = threading.local()
_write_lock = threading.RLock()
_revision = 0             # 検索インデックスの再構築判定用（プロセス内）
_rev_lock = threading.Lock()


def set_data_dir(path: Path) -> None:
    """データ保存先を変更する（--data-dir / テスト用）。既存接続は閉じる。"""
    global DATA_DIR
    DATA_DIR = Path(path)
    close()


def files_dir() -> Path:
    d = DATA_DIR / "files"
    d.mkdir(parents=True, exist_ok=True)
    return d


def conn() -> sqlite3.Connection:
    c = getattr(_local, "conn", None)
    path = str(DATA_DIR / "docvault.sqlite3")
    if c is None or getattr(_local, "path", None) != path:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(path, timeout=30, check_same_thread=False)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        c.execute("PRAGMA foreign_keys=OFF")
        with _write_lock:
            c.executescript(SCHEMA)
        _local.conn, _local.path = c, path
    return c


def close() -> None:
    c = getattr(_local, "conn", None)
    if c is not None:
        c.close()
    _local.conn = None


def bump_revision() -> int:
    global _revision
    with _rev_lock:
        _revision += 1
        return _revision


def revision() -> int:
    return _revision


def write(sql: str, params=()) -> None:
    with _write_lock:
        c = conn()
        c.execute(sql, params)
        c.commit()


def write_many(statements: list[tuple[str, tuple]]) -> None:
    """複数の書き込みを1トランザクションで。"""
    with _write_lock:
        c = conn()
        try:
            for sql, params in statements:
                c.execute(sql, params)
            c.commit()
        except Exception:
            c.rollback()
            raise


def executemany(sql: str, rows: list[tuple]) -> None:
    with _write_lock:
        c = conn()
        c.executemany(sql, rows)
        c.commit()


def query(sql: str, params=()) -> list[sqlite3.Row]:
    return conn().execute(sql, params).fetchall()


def one(sql: str, params=()):
    return conn().execute(sql, params).fetchone()


def new_id(prefix: str = "") -> str:
    return prefix + uuid.uuid4().hex[:12]


def now() -> float:
    return time.time()


# ---------------------------------------------------------------- 文書

DOC_JSON_FIELDS = ("tags", "warnings", "flags", "keywords")


def doc_dict(row) -> dict:
    d = dict(row)
    for key in DOC_JSON_FIELDS:
        try:
            d[key] = json.loads(d.get(key) or ("{}" if key == "flags" else "[]"))
        except ValueError:
            d[key] = {} if key == "flags" else []
    return d


def get_doc(doc_id: str) -> dict | None:
    row = one("SELECT * FROM documents WHERE id=?", (doc_id,))
    return doc_dict(row) if row else None


def list_docs() -> list[dict]:
    return [doc_dict(r) for r in query("SELECT * FROM documents ORDER BY updated_at DESC")]


def update_doc(doc_id: str, **fields) -> None:
    if not fields:
        return
    for key in DOC_JSON_FIELDS:
        if key in fields and not isinstance(fields[key], str):
            fields[key] = json.dumps(fields[key], ensure_ascii=False)
    fields["updated_at"] = now()
    cols = ", ".join(f"{k}=?" for k in fields)
    write(f"UPDATE documents SET {cols} WHERE id=?", (*fields.values(), doc_id))


def delete_doc_data(doc_id: str, *, keep_doc: bool = False) -> None:
    """文書に紐づく派生データ（チャンク・ベクトル・グラフ）を消す。keep_doc=False なら本体も。"""
    stmts = [("DELETE FROM chunks WHERE doc_id=?", (doc_id,)),
             ("DELETE FROM vectors WHERE doc_id=?", (doc_id,)),
             ("DELETE FROM entity_mentions WHERE doc_id=?", (doc_id,)),
             ("DELETE FROM relation_mentions WHERE doc_id=?", (doc_id,))]
    if not keep_doc:
        stmts.append(("DELETE FROM documents WHERE id=?", (doc_id,)))
    write_many(stmts)
    bump_revision()


def doc_chunks(doc_id: str) -> list[dict]:
    rows = query("SELECT * FROM chunks WHERE doc_id=? ORDER BY ord", (doc_id,))
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["keywords"] = json.loads(d["keywords"] or "[]")
        except ValueError:
            d["keywords"] = []
        out.append(d)
    return out


# ---------------------------------------------------------------- 会話

def create_chat(title: str = "", mode: str = "auto", scope: dict | None = None) -> dict:
    cid = new_id("c")
    t = now()
    write("INSERT INTO chats(id,title,mode,scope,created_at,updated_at) VALUES(?,?,?,?,?,?)",
          (cid, title or "新しい相談", mode, json.dumps(scope or {}, ensure_ascii=False), t, t))
    return get_chat(cid)


def chat_dict(row) -> dict:
    d = dict(row)
    try:
        d["scope"] = json.loads(d.get("scope") or "{}")
    except ValueError:
        d["scope"] = {}
    d["touched_internal"] = bool(d.get("touched_internal"))
    return d


def get_chat(chat_id: str) -> dict | None:
    row = one("SELECT * FROM chats WHERE id=?", (chat_id,))
    return chat_dict(row) if row else None


def list_chats() -> list[dict]:
    return [chat_dict(r) for r in query("SELECT * FROM chats ORDER BY updated_at DESC LIMIT 200")]


def update_chat(chat_id: str, **fields) -> None:
    if "scope" in fields and not isinstance(fields["scope"], str):
        fields["scope"] = json.dumps(fields["scope"], ensure_ascii=False)
    if "touched_internal" in fields:
        fields["touched_internal"] = int(bool(fields["touched_internal"]))
    fields["updated_at"] = now()
    cols = ", ".join(f"{k}=?" for k in fields)
    write(f"UPDATE chats SET {cols} WHERE id=?", (*fields.values(), chat_id))


def delete_chat(chat_id: str) -> None:
    write_many([("DELETE FROM messages WHERE chat_id=?", (chat_id,)),
                ("DELETE FROM chats WHERE id=?", (chat_id,))])


def add_message(chat_id: str, role: str, content: str, meta: dict | None = None) -> dict:
    mid = new_id("m")
    t = now()
    write("INSERT INTO messages(id,chat_id,role,content,meta,created_at) VALUES(?,?,?,?,?,?)",
          (mid, chat_id, role, content, json.dumps(meta or {}, ensure_ascii=False), t))
    write("UPDATE chats SET updated_at=? WHERE id=?", (t, chat_id))
    return {"id": mid, "chat_id": chat_id, "role": role, "content": content,
            "meta": meta or {}, "created_at": t}


def chat_messages(chat_id: str) -> list[dict]:
    out = []
    for r in query("SELECT * FROM messages WHERE chat_id=? ORDER BY created_at, rowid", (chat_id,)):
        d = dict(r)
        try:
            d["meta"] = json.loads(d["meta"] or "{}")
        except ValueError:
            d["meta"] = {}
        out.append(d)
    return out
