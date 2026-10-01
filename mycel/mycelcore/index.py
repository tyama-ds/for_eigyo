"""インデックス（SQLite、``<vault>/.mycel/index.sqlite``）。

ノート（Vault 内の .md、編集可）と資料（Word・PDF など、読み取り専用）を同じ表で持つ。

処理時間を抑えるため、ファイルの読み込みは自動では行わない。
- 初回: Vault を開いたときにインデックスが空なら全体を読み込む（初回の読込）
- 以降: 利用者が「変更を確認」（ファイルの日時・サイズを見るだけ）と
  「更新」（増えた・変わった・消えたファイルだけ読み直す）を押したときだけ
- Mycel で保存したノートは、その 1 件だけ即時に反映する（軽い処理）
- どちらも読み込み範囲の一部（フォルダ・外部フォルダ）だけを指定して実行できる

検索:
- 全文検索は FTS5 trigram（日本語も部分一致）。3 文字未満は LIKE
- AI 用の検索は文字バイグラムの転置インデックス（terms 表）で BM25 を計算する。
  索引は更新時に作るので、質問のたびに全資料を処理し直すことはない
"""
from __future__ import annotations

import json
import math
import re
import sqlite3
import threading
import time
from array import array
from collections import Counter
from pathlib import Path

from . import links as L
from .extract import EXT_GROUP, ExtractError, extract
from .scope import VAULT_ID, Scope, is_under, split_id
from .vault import Vault, folder_of, version_of

SCHEMA_VERSION = 3
NOTE_EXTS = (".md", ".markdown")
_PUNCT_RE = re.compile(r"[\s!-/:-@\[-`{-~、。「」『』（）！？・：；，．【】〈〉《》…―｜]+")


def _key(s: str) -> str:
    return s.strip().lower()


def bigrams(text: str) -> list[str]:
    """文字バイグラム（英数字の語はそのまま 1 トークン）。分かち書きなしで日本語を引ける。"""
    out: list[str] = []
    for seg in _PUNCT_RE.split(text.lower()):
        if not seg:
            continue
        if seg.isascii():
            out.append(seg[:40])
            continue
        if len(seg) == 1:
            out.append(seg)
        out.extend(seg[i:i + 2] for i in range(len(seg) - 1))
    return out


class Cancelled(Exception):
    pass


def name_of(path: str) -> str:
    return path.rsplit("/", 1)[-1]


def stem_of(path: str) -> str:
    n = name_of(path)
    return n.rsplit(".", 1)[0] if "." in n else n


class Index:
    def __init__(self, vault: Vault, scope: Scope):
        self.vault = vault
        self.scope = scope
        self.db_path = vault.internal / "index.sqlite"
        self._lock = threading.RLock()
        self.rev = 0                                   # 変わるたびに増える（キャッシュ無効化用）
        self.pending: dict[str, str] = {}              # パス → added / modified / deleted（最後の確認結果）
        self.last_scan: float | None = None
        self.relations = None                          # 資料同士のつながり（app が設定する）
        self.conn = self._open()
        self.has_fts = self._init_schema()

    # ------------------------------------------------------------ 初期化
    def _open(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _init_schema(self) -> bool:
        c = self.conn
        ver = c.execute("PRAGMA user_version").fetchone()[0]
        if ver != SCHEMA_VERSION:
            for t in ("notes", "items", "links", "tags", "chunks", "terms", "embeddings", "fts", "meta"):
                c.execute(f"DROP TABLE IF EXISTS {t}")
        c.executescript("""
            CREATE TABLE IF NOT EXISTS items(
                path TEXT PRIMARY KEY, kind TEXT, grp TEXT, source TEXT,
                title TEXT, title_key TEXT, stem_key TEXT, path_key TEXT, folder TEXT,
                mtime_ns INTEGER, size INTEGER, text TEXT, props TEXT,
                status TEXT, error TEXT, indexed_at REAL);
            CREATE INDEX IF NOT EXISTS items_title ON items(title_key);
            CREATE INDEX IF NOT EXISTS items_stem ON items(stem_key);
            CREATE INDEX IF NOT EXISTS items_pathkey ON items(path_key);
            CREATE TABLE IF NOT EXISTS links(src TEXT, target TEXT, target_key TEXT);
            CREATE INDEX IF NOT EXISTS links_src ON links(src);
            CREATE INDEX IF NOT EXISTS links_target ON links(target_key);
            CREATE TABLE IF NOT EXISTS tags(path TEXT, tag TEXT);
            CREATE INDEX IF NOT EXISTS tags_tag ON tags(tag);
            CREATE INDEX IF NOT EXISTS tags_path ON tags(path);
            CREATE TABLE IF NOT EXISTS chunks(
                id INTEGER PRIMARY KEY AUTOINCREMENT, path TEXT, ord INTEGER,
                heading TEXT, text TEXT, hash TEXT, len INTEGER);
            CREATE INDEX IF NOT EXISTS chunks_path ON chunks(path);
            CREATE TABLE IF NOT EXISTS terms(chunk_id INTEGER, term TEXT, tf INTEGER);
            CREATE INDEX IF NOT EXISTS terms_term ON terms(term);
            CREATE INDEX IF NOT EXISTS terms_chunk ON terms(chunk_id);
            CREATE TABLE IF NOT EXISTS embeddings(
                hash TEXT, model TEXT, vec BLOB, PRIMARY KEY(hash, model));
            CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
        """)
        has_fts = True
        try:
            c.execute("CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5("
                      "path UNINDEXED, title, text, tokenize='trigram')")
        except sqlite3.OperationalError:
            has_fts = False
        c.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        c.commit()
        return has_fts

    def close(self) -> None:
        with self._lock:
            self.conn.close()

    def meta(self, key: str) -> str | None:
        with self._lock:
            r = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
            return r["value"] if r else None

    def set_meta(self, key: str, value: str) -> None:
        with self._lock:
            self.conn.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (key, value))
            self.conn.commit()

    def is_empty(self) -> bool:
        with self._lock:
            return self.conn.execute("SELECT 1 FROM items LIMIT 1").fetchone() is None

    # ------------------------------------------------------------ 確認（走査のみ）
    def _indexed_meta(self, prefixes: list[str] | None) -> dict[str, tuple[int, int]]:
        with self._lock:
            rows = self.conn.execute("SELECT path, mtime_ns, size FROM items").fetchall()
        out = {}
        for r in rows:
            if prefixes is None or any(self._under(r["path"], p) for p in prefixes):
                out[r["path"]] = (r["mtime_ns"], r["size"])
        return out

    @staticmethod
    def _under(path: str, prefix: str) -> bool:
        """prefix の配下か。"" は Vault 全体（@ で始まらないパス）、"@s1" は外部フォルダ全体。"""
        if prefix == "":
            return not path.startswith("@")
        return is_under(path, prefix)

    def scan(self, prefixes: list[str] | None = None, cancel=None, progress=None) -> dict:
        """ファイルの日時・サイズだけを見て、インデックスとの差分を調べる（本文は読まない）。"""
        found: dict[str, tuple] = {}
        for i, (path, abs_p, mtime, size) in enumerate(self.scope.walk(prefixes)):
            found[path] = (abs_p, mtime, size)
            if cancel is not None and cancel.is_set():
                raise Cancelled()
            if progress and i % 200 == 0:
                progress("確認中", i, 0, path)
        known = self._indexed_meta(prefixes)
        added = sorted(p for p in found if p not in known)
        modified = sorted(p for p in found if p in known and known[p] != found[p][1:])
        deleted = sorted(p for p in known if p not in found)
        with self._lock:
            for p in [p for p in self.pending if prefixes is None or any(self._under(p, x) for x in prefixes)]:
                del self.pending[p]
            for lst, state in ((added, "added"), (modified, "modified"), (deleted, "deleted")):
                for p in lst:
                    self.pending[p] = state
            self.last_scan = time.time()
        return {"added": added, "modified": modified, "deleted": deleted,
                "total": len(found), "unchanged": len(found) - len(added) - len(modified),
                "_found": found}

    # ------------------------------------------------------------ 更新（差分だけ読み込み）
    def update(self, prefixes: list[str] | None = None, cancel=None, progress=None) -> dict:
        diff = self.scan(prefixes, cancel=cancel, progress=progress)
        found = diff.pop("_found")
        work = diff["added"] + diff["modified"]
        total = len(work) + len(diff["deleted"])
        done = errors = 0
        for path in diff["deleted"]:
            with self._lock:
                self._remove(path)
                self.pending.pop(path, None)
                self.conn.commit()
            done += 1
        if diff["deleted"]:
            self.rev += 1
        for path in work:
            if cancel is not None and cancel.is_set():
                self.rev += 1
                raise Cancelled()
            if progress:
                progress("読み込み中", done, total, path)
            abs_p, mtime, size = found[path]
            item = self._read(path, abs_p)
            with self._lock:
                self._store(path, item, mtime, size)
                self.pending.pop(path, None)
                self.conn.commit()
            errors += item["status"] != "ok"
            done += 1
            if done % 20 == 0:
                self.rev += 1
        self.rev += 1
        self.set_meta("last_update", str(time.time()))
        diff.update(errors=errors)
        return diff

    def refresh(self, path: str) -> None:
        """1 件だけ即時に反映する（Mycel でノートを保存・作成・削除したとき）。"""
        try:
            abs_p = self.scope.abs_path(path)
        except Exception:  # noqa: BLE001 - 範囲外
            abs_p = None
        with self._lock:
            if abs_p is not None and abs_p.is_file():
                st = abs_p.stat()
                if self.scope.file_state(path, st.st_size) == "in":
                    self._store(path, self._read(path, abs_p), st.st_mtime_ns, st.st_size)
                else:
                    self._remove(path)
            else:
                self._remove(path)
            self.pending.pop(path, None)
            self.conn.commit()
            self.rev += 1

    def rename_path(self, old: str, new: str) -> None:
        """ファイルの移動・名前変更を、本文を読み直さずに反映する（大きな PDF でも速い）。"""
        with self._lock:
            self._rename(old, new)
            self.conn.commit()
            self.rev += 1

    def rename_prefix(self, old: str, new: str) -> int:
        """フォルダの移動・名前変更。配下のファイルをまとめて付け替える。"""
        with self._lock:
            paths = [r["path"] for r in self.conn.execute("SELECT path FROM items")
                     if is_under(r["path"], old)]
            for p in paths:
                self._rename(p, new + p[len(old):])
            self.conn.commit()
            self.rev += 1
            return len(paths)

    def remove_prefix(self, prefix: str) -> int:
        with self._lock:
            paths = [r["path"] for r in self.conn.execute("SELECT path FROM items") if is_under(r["path"], prefix)]
            for p in paths:
                self._remove(p)
                self.pending.pop(p, None)
            self.conn.commit()
            self.rev += 1
            return len(paths)

    def _rename(self, old: str, new: str) -> None:
        c = self.conn
        if c.execute("SELECT 1 FROM items WHERE path=?", (old,)).fetchone() is None:
            return
        self._remove(new)
        note = self._is_note(new)
        title = stem_of(new) if note else name_of(new)
        sid, _ = split_id(new)
        path_key = _key(new[:-3] if note and new.lower().endswith(".md") else new)
        c.execute("UPDATE items SET path=?, title=?, title_key=?, stem_key=?, path_key=?, folder=?, source=? "
                  "WHERE path=?", (new, title, _key(title), _key(stem_of(new)), path_key, folder_of(new), sid, old))
        c.execute("UPDATE chunks SET path=? WHERE path=?", (new, old))
        c.execute("UPDATE links SET src=? WHERE src=?", (new, old))
        c.execute("UPDATE tags SET path=? WHERE path=?", (new, old))
        if self.has_fts:
            c.execute("UPDATE fts SET path=?, title=? WHERE path=?", (new, title, old))
        if old in self.pending:
            self.pending[new] = self.pending.pop(old)

    def link_sources_with_prefix(self, prefix: str) -> list[str]:
        """[[フォルダ/…]] の形でフォルダ配下を指しているノート。"""
        key = _key(prefix).rstrip("/") + "/"
        with self._lock:
            rows = self.conn.execute("SELECT DISTINCT src FROM links WHERE substr(target_key, 1, ?)=?",
                                     (len(key), key)).fetchall()
        return [r["src"] for r in rows]

    def rebuild(self, cancel=None, progress=None) -> dict:
        with self._lock:
            for t in ("items", "links", "tags", "chunks", "terms"):
                self.conn.execute(f"DELETE FROM {t}")
            if self.has_fts:
                self.conn.execute("DELETE FROM fts")
            self.conn.commit()
            self.pending.clear()
        return self.update(None, cancel=cancel, progress=progress)

    # ------------------------------------------------------------ 内部: 読み込みと保存
    def _is_note(self, path: str) -> bool:
        return not path.startswith("@") and path.lower().endswith(NOTE_EXTS)

    def _read(self, path: str, abs_p: Path) -> dict:
        try:
            if self._is_note(path):
                text = abs_p.read_bytes().decode("utf-8", errors="replace")
            else:
                text = extract(abs_p)
            return {"status": "ok", "text": text, "error": ""}
        except ExtractError as e:
            return {"status": "error", "text": "", "error": str(e)}
        except OSError as e:
            return {"status": "error", "text": "", "error": f"ファイルを開けません: {e}"}

    def _remove(self, path: str) -> None:
        c = self.conn
        ids = [r[0] for r in c.execute("SELECT id FROM chunks WHERE path=?", (path,))]
        if ids:
            c.executemany("DELETE FROM terms WHERE chunk_id=?", [(i,) for i in ids])
        c.execute("DELETE FROM chunks WHERE path=?", (path,))
        c.execute("DELETE FROM items WHERE path=?", (path,))
        c.execute("DELETE FROM links WHERE src=?", (path,))
        c.execute("DELETE FROM tags WHERE path=?", (path,))
        if self.has_fts:
            c.execute("DELETE FROM fts WHERE path=?", (path,))

    def _store(self, path: str, item: dict, mtime: int, size: int) -> None:
        self._remove(path)
        c = self.conn
        note = self._is_note(path)
        name = name_of(path)
        title = stem_of(path) if note else name
        text = item["text"]
        sid, _ = split_id(path)
        props = L.split_frontmatter(text)[0] if note else {}
        path_key = _key(path[:-3] if note and path.lower().endswith(".md") else path)
        c.execute("INSERT INTO items VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (path, "note" if note else "doc", EXT_GROUP.get(Path(path).suffix.lower(), ""), sid,
                   title, _key(title), _key(stem_of(path)), path_key, folder_of(path),
                   mtime, size, text, json.dumps(props, ensure_ascii=False),
                   item["status"], item["error"], time.time()))
        if item["status"] != "ok":
            return
        if note:
            c.executemany("INSERT INTO links VALUES(?,?,?)",
                          [(path, t, _key(t)) for t in dict.fromkeys(L.extract_links(text))])
            c.executemany("INSERT INTO tags VALUES(?,?)", [(path, t) for t in L.extract_tags(text)])
        for i, ch in enumerate(L.chunk_note(text)):
            body = f"{title} {ch['heading']}\n{ch['text']}"
            toks = Counter(bigrams(body))
            cur = c.execute("INSERT INTO chunks(path, ord, heading, text, hash, len) VALUES(?,?,?,?,?,?)",
                            (path, i, ch["heading"], ch["text"],
                             version_of(f"{title}\n{ch['text']}".encode("utf-8")), sum(toks.values())))
            cid = cur.lastrowid
            c.executemany("INSERT INTO terms VALUES(?,?,?)", [(cid, t, n) for t, n in toks.items()])
        if self.has_fts:
            c.execute("INSERT INTO fts(path, title, text) VALUES(?,?,?)", (path, title, text))

    # ------------------------------------------------------------ 状態
    def status(self) -> dict:
        with self._lock:
            rows = self.conn.execute(
                "SELECT kind, status, count(*) n FROM items GROUP BY kind, status").fetchall()
            chunks = self.conn.execute("SELECT count(*) FROM chunks").fetchone()[0]
        counts = {"notes": 0, "docs": 0, "errors": 0}
        for r in rows:
            counts["notes" if r["kind"] == "note" else "docs"] += r["n"]
            if r["status"] != "ok":
                counts["errors"] += r["n"]
        pend = Counter(self.pending.values())
        last = self.meta("last_update")
        return {**counts, "chunks": chunks,
                "pending": {"added": pend.get("added", 0), "modified": pend.get("modified", 0),
                            "deleted": pend.get("deleted", 0)},
                "last_update": float(last) if last else None, "last_scan": self.last_scan}

    def folder_stats(self, prefix: str) -> dict:
        """範囲ダイアログ用: フォルダ配下の読込済み件数・エラー・未反映の件数。"""
        with self._lock:
            rows = self.conn.execute("SELECT path, status FROM items").fetchall()
        indexed = errors = 0
        for r in rows:
            if self._under(r["path"], prefix):
                indexed += 1
                errors += r["status"] != "ok"
        pend = Counter(s for p, s in self.pending.items() if self._under(p, prefix))
        return {"indexed": indexed, "errors": errors, "added": pend.get("added", 0),
                "modified": pend.get("modified", 0), "deleted": pend.get("deleted", 0)}

    def item_states(self, paths: list[str]) -> dict[str, dict]:
        with self._lock:
            out = {}
            for p in paths:
                r = self.conn.execute("SELECT status, error, mtime_ns, size FROM items WHERE path=?",
                                      (p,)).fetchone()
                out[p] = dict(r) if r else None
            return out

    # ------------------------------------------------------------ 参照
    def notes(self) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self.conn.execute(
                "SELECT path, title, folder, kind, grp, source, status, mtime_ns, size FROM items "
                "ORDER BY path_key")]

    def get(self, path: str) -> dict | None:
        with self._lock:
            r = self.conn.execute("SELECT * FROM items WHERE path=?", (path,)).fetchone()
            return dict(r) if r else None

    def resolve(self, target: str) -> str | None:
        """リンク先の名前からパスを引く。ノート名 → 資料のファイル名 → 資料の拡張子なしの名前の順。"""
        key = _key(target)
        if not key:
            return None
        with self._lock:
            if "/" in key:
                for k in (key, key[:-3] if key.endswith(".md") else None):
                    if k:
                        r = self.conn.execute("SELECT path FROM items WHERE path_key=?", (k,)).fetchone()
                        if r:
                            return r["path"]
                key = key.rsplit("/", 1)[-1]
            if key.endswith(".md"):
                key = key[:-3]
            for col in ("title_key", "stem_key"):
                r = self.conn.execute(
                    f"SELECT path FROM items WHERE {col}=? ORDER BY kind='note' DESC, length(path), path LIMIT 1",
                    (key,)).fetchone()
                if r:
                    return r["path"]
            return None

    def _keys_for(self, path: str) -> set[str]:
        keys = {_key(stem_of(path)), _key(name_of(path)), _key(path)}
        if path.lower().endswith(".md"):
            keys.add(_key(path[:-3]))
        return keys

    def outgoing(self, path: str) -> list[dict]:
        with self._lock:
            rows = self.conn.execute("SELECT target FROM links WHERE src=?", (path,)).fetchall()
        return [{"target": r["target"], "path": self.resolve(r["target"])} for r in rows]

    def backlinks(self, path: str) -> list[dict]:
        keys = list(self._keys_for(path))
        q = ",".join("?" * len(keys))
        with self._lock:
            rows = self.conn.execute(
                f"SELECT DISTINCT l.src, l.target, n.text, n.title FROM links l JOIN items n ON n.path=l.src "
                f"WHERE l.target_key IN ({q}) AND l.src<>? ORDER BY l.src", (*keys, path)).fetchall()
        out, seen = [], set()
        for r in rows:
            if r["src"] in seen or self.resolve(r["target"]) != path:
                continue
            seen.add(r["src"])
            out.append({"path": r["src"], "title": r["title"],
                        "context": L.line_containing(r["text"], "[[" + r["target"])})
        return out

    def unlinked_mentions(self, path: str, limit: int = 30) -> list[dict]:
        word = stem_of(path)
        if len(word) < 2:
            return []
        linked = {b["path"] for b in self.backlinks(path)}
        with self._lock:
            rows = self.conn.execute(
                "SELECT path, title, text FROM items WHERE kind='note' AND path<>? AND instr(lower(text), ?)>0 "
                "ORDER BY path LIMIT ?", (path, word.lower(), limit * 3)).fetchall()
        out = []
        for r in rows:
            if r["path"] in linked:
                continue
            stripped = L.WIKILINK_RE.sub(" ", r["text"])
            if word.lower() not in stripped.lower():
                continue
            out.append({"path": r["path"], "title": r["title"],
                        "context": L.line_containing(stripped, word)})
            if len(out) >= limit:
                break
        return out

    def search(self, query: str, limit: int = 50) -> list[dict]:
        terms = [t for t in query.split() if t]
        if not terms:
            return []
        with self._lock:
            rows = None
            if self.has_fts and all(len(t) >= 3 for t in terms):
                match = " AND ".join('"' + t.replace('"', '""') + '"' for t in terms)
                try:
                    rows = self.conn.execute(
                        "SELECT n.path, n.title, n.text, n.kind FROM fts f JOIN items n ON n.path=f.path "
                        "WHERE fts MATCH ? ORDER BY bm25(fts, 0.0, 5.0, 1.0) LIMIT ?",
                        (match, limit)).fetchall()
                except sqlite3.OperationalError:
                    rows = None
            if rows is None:
                where = " AND ".join("instr(lower(title || char(10) || text), ?)>0" for _ in terms)
                rows = self.conn.execute(
                    f"SELECT path, title, text, kind FROM items WHERE {where} "
                    "ORDER BY (instr(lower(title), ?)>0) DESC, kind='note' DESC, path_key LIMIT ?",
                    [t.lower() for t in terms] + [terms[0].lower(), limit]).fetchall()
        return [{"path": r["path"], "title": r["title"], "kind": r["kind"],
                 "snippet": L.line_containing(r["text"], terms[0]) or r["text"][:120]} for r in rows]

    def tags(self) -> list[dict]:
        with self._lock:
            return [{"tag": r["tag"], "count": r["n"]} for r in self.conn.execute(
                "SELECT tag, count(DISTINCT path) n FROM tags GROUP BY tag ORDER BY n DESC, tag")]

    def notes_with_tag(self, tag: str) -> list[dict]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT DISTINCT n.path, n.title FROM tags t JOIN items n ON n.path=t.path "
                "WHERE t.tag=? OR t.tag LIKE ? ORDER BY n.path_key", (tag, tag + "/%")).fetchall()
        return [dict(r) for r in rows]

    def graph(self, center: str | None = None, depth: int = 1, all_docs: bool = False,
              extra: dict | None = None) -> dict:
        """ノードとエッジ。資料はリンクされているものだけ（all_docs=True で全部）。
        extra = {"nodes": {id: (title, kind, grp)}, "edges": [(path, id)]} で人物・組織のノードを足せる。"""
        with self._lock:
            items = {r["path"]: (r["title"], r["kind"], r["grp"])
                     for r in self.conn.execute("SELECT path, title, kind, grp FROM items")}
            rows = self.conn.execute("SELECT src, target FROM links").fetchall()
        edges: set[tuple[str, str]] = set()
        unresolved: dict[str, str] = {}
        for r in rows:
            dst = self.resolve(r["target"])
            if dst is None:
                uid = "?" + _key(r["target"])
                unresolved.setdefault(uid, r["target"])
                dst = uid
            if dst != r["src"]:
                edges.add((r["src"], dst))
        rel_edges: set[tuple[str, str]] = set()
        for a, b in (self.relations.pairs() if self.relations else []):
            if a in items and b in items and (b, a) not in rel_edges:
                rel_edges.add((a, b))
        ent_nodes = (extra or {}).get("nodes", {})
        ent_edges = {(a, b) for a, b in (extra or {}).get("edges", []) if a in items and b in ent_nodes}
        linked = {a for e in edges | rel_edges for a in e}
        nodes_set = {p for p, (_, kind, _) in items.items() if kind == "note" or all_docs or p in linked}
        nodes_set |= set(unresolved)
        if ent_edges:
            nodes_set |= {b for _, b in ent_edges} | {a for a, _ in ent_edges}
        all_edges = edges | rel_edges | ent_edges
        if center:
            keep, frontier = {center}, {center}
            for _ in range(max(1, min(depth, 4))):
                nxt = set()
                for a, b in all_edges:
                    if a in frontier and b not in keep:
                        nxt.add(b)
                    if b in frontier and a not in keep:
                        nxt.add(a)
                keep |= nxt
                frontier = nxt
            nodes_set &= keep
        deg: dict[str, int] = {}
        for a, b in all_edges:
            if a in nodes_set and b in nodes_set:
                deg[a] = deg.get(a, 0) + 1
                deg[b] = deg.get(b, 0) + 1
        nodes = []
        for n in sorted(nodes_set):
            if n in ent_nodes:
                title, kind, grp = ent_nodes[n]
                nodes.append({"id": n, "title": title, "exists": True, "kind": kind, "grp": grp, "folder": "",
                              "degree": deg.get(n, 0)})
                continue
            title, kind, grp = items.get(n, (unresolved.get(n, n), "", ""))
            nodes.append({"id": n, "title": title, "exists": n in items, "kind": kind or "missing",
                          "grp": grp, "folder": folder_of(n) if n in items else "", "degree": deg.get(n, 0)})
        out_edges = [[a, b] for a, b in sorted(edges) if a in nodes_set and b in nodes_set]
        out_edges += [[a, b, "rel"] for a, b in sorted(rel_edges - edges)
                      if a in nodes_set and b in nodes_set and (b, a) not in edges]
        out_edges += [[a, b, "ent"] for a, b in sorted(ent_edges) if a in nodes_set and b in nodes_set]
        return {"nodes": nodes, "edges": out_edges}

    # ------------------------------------------------------------ AI 用の検索（転置インデックス）
    def search_chunks(self, query: str, k: int = 50, prefixes: list[str] | None = None,
                      exclude_prefixes: list[str] | None = None) -> list[tuple[int, float]]:
        """BM25 で上位のチャンク (id, スコア) を返す。"""
        q = list(dict.fromkeys(bigrams(query)))
        if not q:
            return []
        with self._lock:
            n_row = self.conn.execute("SELECT count(*), avg(len) FROM chunks").fetchone()
            n, avgdl = n_row[0] or 0, (n_row[1] or 1.0)
            if not n:
                return []
            dfs = {}
            for i in range(0, len(q), 400):
                part = q[i:i + 400]
                for r in self.conn.execute(
                        f"SELECT term, count(*) c FROM terms WHERE term IN ({','.join('?' * len(part))}) "
                        "GROUP BY term", part):
                    dfs[r["term"]] = r["c"]
            if not dfs:
                return []
            # どこにでも出る語（「します」など）は除き、珍しい語から最大 64 個を使う
            limit_df = max(30, int(n * 0.3))
            useful = [t for t in dfs if dfs[t] <= limit_df] or sorted(dfs, key=dfs.get)[:8]
            useful = sorted(useful, key=lambda t: dfs[t])[:64]
            idf = {t: math.log(1 + (n - dfs[t] + 0.5) / (dfs[t] + 0.5)) for t in useful}
            rows = self.conn.execute(
                f"SELECT t.chunk_id, t.term, t.tf, c.len, c.path FROM terms t JOIN chunks c ON c.id=t.chunk_id "
                f"WHERE t.term IN ({','.join('?' * len(useful))})", useful).fetchall()
        k1, b = 1.4, 0.75
        scores: dict[int, float] = {}
        paths: dict[int, str] = {}
        for r in rows:
            f = r["tf"]
            s = idf[r["term"]] * f * (k1 + 1) / (f + k1 * (1 - b + b * (r["len"] or 1) / avgdl))
            scores[r["chunk_id"]] = scores.get(r["chunk_id"], 0.0) + s
            paths[r["chunk_id"]] = r["path"]
        out = []
        for cid, s in sorted(scores.items(), key=lambda x: -x[1]):
            p = paths[cid]
            if prefixes is not None and not any(self._under(p, x) for x in prefixes):
                continue
            if exclude_prefixes and any(x and is_under(p, x) for x in exclude_prefixes):
                continue
            out.append((cid, s))
            if len(out) >= k:
                break
        return out

    def chunk_refs(self) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self.conn.execute("SELECT id, path, hash FROM chunks")]

    def get_chunks(self, ids: list[int]) -> dict[int, dict]:
        if not ids:
            return {}
        with self._lock:
            rows = self.conn.execute(
                f"SELECT c.id, c.path, c.ord, c.heading, c.text, c.hash, n.title, n.kind FROM chunks c "
                f"JOIN items n ON n.path=c.path WHERE c.id IN ({','.join('?' * len(ids))})", ids).fetchall()
        return {r["id"]: dict(r) for r in rows}

    def chunk_count(self) -> int:
        with self._lock:
            return self.conn.execute("SELECT count(*) FROM chunks").fetchone()[0]

    def embedded_count(self, model: str) -> int:
        with self._lock:
            return self.conn.execute(
                "SELECT count(*) FROM chunks c WHERE EXISTS (SELECT 1 FROM embeddings e "
                "WHERE e.hash=c.hash AND e.model=?)", (model,)).fetchone()[0]

    def chunks_without_embedding(self, model: str, prefixes: list[str] | None = None) -> list[dict]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT c.hash, c.path, c.heading, c.text, n.title FROM chunks c JOIN items n ON n.path=c.path "
                "WHERE NOT EXISTS (SELECT 1 FROM embeddings e WHERE e.hash=c.hash AND e.model=?)",
                (model,)).fetchall()
        out, seen = [], set()
        for r in rows:
            if r["hash"] in seen:
                continue
            if prefixes is not None and not any(self._under(r["path"], p) for p in prefixes):
                continue
            seen.add(r["hash"])
            out.append(dict(r))
        return out

    def embeddings(self, model: str) -> dict[str, list[float]]:
        with self._lock:
            out = {}
            for r in self.conn.execute("SELECT hash, vec FROM embeddings WHERE model=?", (model,)):
                a = array("f")
                a.frombytes(r["vec"])
                out[r["hash"]] = a.tolist()
            return out

    def put_embeddings(self, model: str, items: list[tuple[str, list[float]]]) -> None:
        with self._lock:
            self.conn.executemany("INSERT OR REPLACE INTO embeddings VALUES(?,?,?)",
                                  [(h, model, array("f", v).tobytes()) for h, v in items])
            self.conn.commit()

    def prune_embeddings(self) -> None:
        with self._lock:
            self.conn.execute("DELETE FROM embeddings WHERE hash NOT IN (SELECT hash FROM chunks)")
            self.conn.commit()


def open_index(vault: Vault, scope: Scope) -> Index:
    """壊れた DB は作り直して開く（本文の読み込みはここでは行わない）。"""
    try:
        return Index(vault, scope)
    except sqlite3.DatabaseError:
        for suffix in ("", "-wal", "-shm"):
            Path(str(vault.internal / "index.sqlite") + suffix).unlink(missing_ok=True)
        return Index(vault, scope)


__all__ = ["Index", "open_index", "bigrams", "Cancelled", "VAULT_ID"]
