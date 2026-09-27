"""ノートのインデックス（SQLite、``<vault>/.mycel/index.sqlite``）。

- ファイルの mtime / サイズを見て差分だけ更新する（外部エディタでの変更も拾う）
- 全文検索は FTS5 の trigram（日本語も部分一致で引ける）。3 文字未満や
  FTS5 が無い環境では LIKE 検索にフォールバック
- リンク表・タグ表・AI 用のチャンク表・埋め込み表を持つ
- 壊れても .md から作り直せる（``rebuild()``）
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from array import array
from pathlib import Path

from . import links as L
from .vault import Vault, folder_of, title_of, version_of

SCHEMA_VERSION = 2


def _key(s: str) -> str:
    return s.strip().lower()


class Index:
    def __init__(self, vault: Vault):
        self.vault = vault
        self.db_path = vault.internal / "index.sqlite"
        self._lock = threading.RLock()
        self._last_sync = 0.0
        self.rev = 0                          # 何か変わるたびに増える（キャッシュ無効化用）
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
            for t in ("notes", "links", "tags", "chunks", "embeddings", "fts"):
                c.execute(f"DROP TABLE IF EXISTS {t}")
        c.executescript("""
            CREATE TABLE IF NOT EXISTS notes(
                path TEXT PRIMARY KEY, title TEXT, title_key TEXT, path_key TEXT,
                folder TEXT, mtime_ns INTEGER, size INTEGER, text TEXT, props TEXT);
            CREATE INDEX IF NOT EXISTS notes_title ON notes(title_key);
            CREATE TABLE IF NOT EXISTS links(src TEXT, target TEXT, target_key TEXT);
            CREATE INDEX IF NOT EXISTS links_src ON links(src);
            CREATE INDEX IF NOT EXISTS links_target ON links(target_key);
            CREATE TABLE IF NOT EXISTS tags(path TEXT, tag TEXT);
            CREATE INDEX IF NOT EXISTS tags_tag ON tags(tag);
            CREATE INDEX IF NOT EXISTS tags_path ON tags(path);
            CREATE TABLE IF NOT EXISTS chunks(
                path TEXT, ord INTEGER, heading TEXT, text TEXT, hash TEXT);
            CREATE INDEX IF NOT EXISTS chunks_path ON chunks(path);
            CREATE TABLE IF NOT EXISTS embeddings(
                hash TEXT, model TEXT, vec BLOB, PRIMARY KEY(hash, model));
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

    # ------------------------------------------------------------ 同期
    def sync(self, force: bool = False, min_interval: float = 1.5) -> int:
        """ファイルとインデックスの差分を反映する。戻り値は更新件数。"""
        with self._lock:
            now = time.time()
            if not force and now - self._last_sync < min_interval:
                return 0
            self._last_sync = now
            files = {rel: (m, s) for rel, m, s in self.vault.list_files()}
            known = {r["path"]: (r["mtime_ns"], r["size"])
                     for r in self.conn.execute("SELECT path, mtime_ns, size FROM notes")}
            changed = 0
            for rel in known.keys() - files.keys():
                self._remove(rel)
                changed += 1
            for rel, meta in files.items():
                if known.get(rel) != meta:
                    self._update(rel, meta)
                    changed += 1
            if changed:
                self.conn.commit()
                self.rev += 1
            return changed

    def refresh(self, rel: str) -> None:
        """1 ノートだけ即時反映（保存・作成・削除の直後に呼ぶ）。"""
        with self._lock:
            p = self.vault.path(rel)
            if p.is_file():
                st = p.stat()
                self._update(rel, (st.st_mtime_ns, st.st_size))
            else:
                self._remove(rel)
            self.conn.commit()
            self.rev += 1

    def rebuild(self) -> int:
        with self._lock:
            for t in ("notes", "links", "tags", "chunks"):
                self.conn.execute(f"DELETE FROM {t}")
            if self.has_fts:
                self.conn.execute("DELETE FROM fts")
            self.conn.commit()
            return self.sync(force=True)

    def _remove(self, rel: str) -> None:
        c = self.conn
        for t in ("notes", "links", "tags", "chunks"):
            c.execute(f"DELETE FROM {t} WHERE {'src' if t == 'links' else 'path'}=?", (rel,))
        if self.has_fts:
            c.execute("DELETE FROM fts WHERE path=?", (rel,))

    def _update(self, rel: str, meta: tuple[int, int]) -> None:
        try:
            text, _ = self.vault.read(rel)
        except Exception:
            return
        self._remove(rel)
        c = self.conn
        title = title_of(rel)
        props, _, _ = L.split_frontmatter(text)
        c.execute("INSERT INTO notes VALUES(?,?,?,?,?,?,?,?,?)",
                  (rel, title, _key(title), _key(rel[:-3]), folder_of(rel),
                   meta[0], meta[1], text, json.dumps(props, ensure_ascii=False)))
        c.executemany("INSERT INTO links VALUES(?,?,?)",
                      [(rel, t, _key(t)) for t in dict.fromkeys(L.extract_links(text))])
        c.executemany("INSERT INTO tags VALUES(?,?)", [(rel, t) for t in L.extract_tags(text)])
        c.executemany("INSERT INTO chunks VALUES(?,?,?,?,?)",
                      [(rel, i, ch["heading"], ch["text"],
                        version_of(f"{title}\n{ch['text']}".encode("utf-8")))
                       for i, ch in enumerate(L.chunk_note(text))])
        if self.has_fts:
            c.execute("INSERT INTO fts(path, title, text) VALUES(?,?,?)", (rel, title, text))

    # ------------------------------------------------------------ 参照
    def notes(self) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self.conn.execute(
                "SELECT path, title, folder, mtime_ns, size FROM notes ORDER BY path_key")]

    def get(self, rel: str) -> dict | None:
        with self._lock:
            r = self.conn.execute("SELECT * FROM notes WHERE path=?", (rel,)).fetchone()
            return dict(r) if r else None

    def resolve(self, target: str) -> str | None:
        """リンク先の名前からノートのパスを引く（同名が複数ならパスの短い方）。"""
        key = _key(target)
        if key.endswith(".md"):
            key = key[:-3]
        with self._lock:
            if "/" in key:
                r = self.conn.execute("SELECT path FROM notes WHERE path_key=?", (key,)).fetchone()
                if r:
                    return r["path"]
                key = key.rsplit("/", 1)[-1]
            r = self.conn.execute(
                "SELECT path FROM notes WHERE title_key=? ORDER BY length(path), path LIMIT 1",
                (key,)).fetchone()
            return r["path"] if r else None

    def _keys_for(self, rel: str) -> tuple[str, str]:
        return _key(title_of(rel)), _key(rel[:-3])

    def outgoing(self, rel: str) -> list[dict]:
        with self._lock:
            rows = self.conn.execute("SELECT target FROM links WHERE src=?", (rel,)).fetchall()
        return [{"target": r["target"], "path": self.resolve(r["target"])} for r in rows]

    def backlinks(self, rel: str) -> list[dict]:
        tkey, pkey = self._keys_for(rel)
        with self._lock:
            rows = self.conn.execute(
                "SELECT DISTINCT l.src, l.target, n.text FROM links l JOIN notes n ON n.path=l.src "
                "WHERE (l.target_key=? OR l.target_key=?) AND l.src<>? ORDER BY l.src",
                (tkey, pkey, rel)).fetchall()
        out, seen = [], set()
        for r in rows:
            # 同名ノートが複数ある場合、実際にこのノートへ解決されるリンクだけ数える
            if r["src"] in seen or self.resolve(r["target"]) != rel:
                continue
            seen.add(r["src"])
            out.append({"path": r["src"], "title": title_of(r["src"]),
                        "context": L.line_containing(r["text"], "[[" + r["target"])})
        return out

    def unlinked_mentions(self, rel: str, limit: int = 30) -> list[dict]:
        title = title_of(rel)
        if len(title) < 2:
            return []
        linked = {b["path"] for b in self.backlinks(rel)}
        with self._lock:
            rows = self.conn.execute(
                "SELECT path, text FROM notes WHERE path<>? AND instr(lower(text), ?)>0 "
                "ORDER BY path LIMIT ?", (rel, title.lower(), limit * 3)).fetchall()
        out = []
        for r in rows:
            if r["path"] in linked:
                continue
            stripped = L.WIKILINK_RE.sub(" ", r["text"])
            if title.lower() not in stripped.lower():
                continue
            out.append({"path": r["path"], "title": title_of(r["path"]),
                        "context": L.line_containing(stripped, title)})
            if len(out) >= limit:
                break
        return out

    def search(self, query: str, limit: int = 50) -> list[dict]:
        terms = [t for t in query.split() if t]
        if not terms:
            return []
        with self._lock:
            if self.has_fts and all(len(t) >= 3 for t in terms):
                match = " AND ".join('"' + t.replace('"', '""') + '"' for t in terms)
                try:
                    rows = self.conn.execute(
                        "SELECT n.path, n.title, n.text FROM fts f JOIN notes n ON n.path=f.path "
                        "WHERE fts MATCH ? ORDER BY bm25(fts, 0.0, 5.0, 1.0) LIMIT ?",
                        (match, limit)).fetchall()
                except sqlite3.OperationalError:
                    rows = None
            else:
                rows = None
            if rows is None:
                where = " AND ".join("instr(lower(title || char(10) || text), ?)>0" for _ in terms)
                rows = self.conn.execute(
                    f"SELECT path, title, text FROM notes WHERE {where} "
                    "ORDER BY (instr(lower(title), ?)>0) DESC, path_key LIMIT ?",
                    [t.lower() for t in terms] + [terms[0].lower(), limit]).fetchall()
        out = []
        for r in rows:
            snippet = L.line_containing(r["text"], terms[0]) or r["text"][:120]
            out.append({"path": r["path"], "title": r["title"], "snippet": snippet})
        return out

    def tags(self) -> list[dict]:
        with self._lock:
            return [{"tag": r["tag"], "count": r["n"]} for r in self.conn.execute(
                "SELECT tag, count(DISTINCT path) n FROM tags GROUP BY tag ORDER BY n DESC, tag")]

    def notes_with_tag(self, tag: str) -> list[dict]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT DISTINCT n.path, n.title FROM tags t JOIN notes n ON n.path=t.path "
                "WHERE t.tag=? OR t.tag LIKE ? ORDER BY n.path_key", (tag, tag + "/%")).fetchall()
        return [dict(r) for r in rows]

    def note_tags(self, rel: str) -> list[str]:
        with self._lock:
            return [r["tag"] for r in self.conn.execute("SELECT tag FROM tags WHERE path=?", (rel,))]

    def graph(self, center: str | None = None, depth: int = 1) -> dict:
        """ノードとエッジ。``center`` 指定時はそこから ``depth`` ホップ以内だけ。"""
        with self._lock:
            notes = {r["path"]: r["title"] for r in self.conn.execute("SELECT path, title FROM notes")}
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
        all_nodes = set(notes) | set(unresolved)
        if center:
            keep = {center}
            frontier = {center}
            for _ in range(max(1, min(depth, 4))):
                nxt = set()
                for a, b in edges:
                    if a in frontier and b not in keep:
                        nxt.add(b)
                    if b in frontier and a not in keep:
                        nxt.add(a)
                keep |= nxt
                frontier = nxt
            all_nodes &= keep
        deg: dict[str, int] = {}
        for a, b in edges:
            if a in all_nodes and b in all_nodes:
                deg[a] = deg.get(a, 0) + 1
                deg[b] = deg.get(b, 0) + 1
        nodes = [{"id": n, "title": notes.get(n) or unresolved.get(n, n),
                  "exists": n in notes, "folder": folder_of(n) if n in notes else "",
                  "degree": deg.get(n, 0)} for n in sorted(all_nodes)]
        return {"nodes": nodes,
                "edges": [[a, b] for a, b in sorted(edges) if a in all_nodes and b in all_nodes]}

    # ------------------------------------------------------------ AI 用
    def chunks(self) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self.conn.execute(
                "SELECT c.path, c.ord, c.heading, c.text, c.hash, n.title FROM chunks c "
                "JOIN notes n ON n.path=c.path ORDER BY c.path, c.ord")]

    def embeddings(self, model: str) -> dict[str, list[float]]:
        with self._lock:
            rows = self.conn.execute("SELECT hash, vec FROM embeddings WHERE model=?", (model,))
            out = {}
            for r in rows:
                a = array("f")
                a.frombytes(r["vec"])
                out[r["hash"]] = a.tolist()
            return out

    def put_embeddings(self, model: str, items: list[tuple[str, list[float]]]) -> None:
        with self._lock:
            self.conn.executemany(
                "INSERT OR REPLACE INTO embeddings VALUES(?,?,?)",
                [(h, model, array("f", v).tobytes()) for h, v in items])
            self.conn.commit()

    def prune_embeddings(self) -> None:
        with self._lock:
            self.conn.execute("DELETE FROM embeddings WHERE hash NOT IN (SELECT hash FROM chunks)")
            self.conn.commit()


def open_index(vault: Vault) -> Index:
    """壊れた DB は作り直して開く。"""
    try:
        idx = Index(vault)
        idx.sync(force=True)
        return idx
    except sqlite3.DatabaseError:
        for suffix in ("", "-wal", "-shm"):
            Path(str(vault.internal / "index.sqlite") + suffix).unlink(missing_ok=True)
        idx = Index(vault)
        idx.sync(force=True)
        return idx
