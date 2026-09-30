"""アプリ本体: Vault・読み込み範囲・インデックス・AI・プラグインをまとめ、HTTP 層から呼ばれる操作を提供する。"""
from __future__ import annotations

import os
import shutil
import threading
import time
from pathlib import Path

from . import links as L
from .ai import AIService
from .config import embed_configured, load_config, save_config, vault_path
from .extract import EXT_GROUP, KINDS, TYPE_GROUPS, pdf_available
from .index import Index, open_index
from .ingest import Ingestor
from .jobs import JobRunner
from .plugins import NoteEvent, PluginContext, PluginManager
from .scope import Scope, ScopeError, browse, split_id
from .vault import Vault, VaultError, folder_of, normalize_rel, title_of, version_of

BASE = Path(__file__).resolve().parent.parent
PLUGIN_DIR = BASE / "plugins"
SAMPLE_DIR = BASE / "sample_vault"
IMPORT_FOLDER = "取り込み"


class MycelApp:
    def __init__(self, config_path: Path | None = None, vault_override: str | None = None,
                 seed_sample: bool = True, plugin_dir: Path | None = None, initial_load: str = "background"):
        """initial_load: インデックスが空のときの初回読込。background / sync / none"""
        self.config_path = config_path
        self.vault_override = vault_override
        self.seed_sample = seed_sample
        self.initial_load = initial_load
        self.plugins = PluginManager(plugin_dir or PLUGIN_DIR)
        self.jobs = JobRunner()
        self.ingest = Ingestor(self)
        self._lock = threading.RLock()
        self.vault: Vault
        self.scope: Scope
        self.index: Index
        self.ai: AIService
        self.open_vault()

    # ------------------------------------------------------------ 設定と Vault
    def config(self) -> dict:
        return load_config(self.config_path)

    def update_config(self, update: dict) -> dict:
        before = self.config()
        cfg = save_config(update, self.config_path)
        if "vault_path" in update and cfg["vault_path"] != before["vault_path"]:
            self.vault_override = None
            self.open_vault()
        elif cfg["plugins"] != before["plugins"]:
            self.plugins.load(cfg["plugins"], PluginContext(self))
        return cfg

    def open_vault(self) -> None:
        if self.jobs.running():
            self.jobs.cancel()
            self.jobs.wait(30)
        with self._lock:
            cfg = self.config()
            root = Path(self.vault_override).expanduser() if self.vault_override else vault_path(cfg)
            fresh = not root.exists() or not any(root.iterdir())
            if fresh and self.seed_sample and SAMPLE_DIR.is_dir():
                shutil.copytree(SAMPLE_DIR, root, dirs_exist_ok=True)
            if getattr(self, "index", None):
                self.index.close()
            self.vault = Vault(root)
            self.scope = Scope(self.vault.root, self.vault.internal)
            self.index = open_index(self.vault, self.scope)
            self.ai = AIService(self.index, self.config)
            self.plugins.load(cfg["plugins"], PluginContext(self))
            self.plugins.emit("on_vault_opened")
        # 初回の読込（インデックスが空のときだけ）。以降の読み込みは利用者の「更新」で行う
        if self.index.is_empty() and self.index.meta("last_update") is None and self.initial_load != "none":
            self.update_index(None, wait=self.initial_load == "sync", label="初回の読込")
        elif self.initial_load == "background":
            # 起動時は「変更の確認」だけ（日時とサイズを見るだけで本文は読まない）
            self.check_index(None)

    def _author(self) -> str:
        return self.config()["user_name"]

    # ------------------------------------------------------------ パス
    def _path(self, path: str) -> str:
        """画面から来たパスを正規化する。ノートは .md を補い、資料・外部フォルダはそのまま。"""
        if not isinstance(path, str) or not path.strip():
            raise VaultError("パスが空です")
        path = path.replace("\\", "/").strip().strip("/")
        ext = Path(path).suffix.lower()
        if path.startswith("@") or (ext in KINDS and ext not in (".md", ".markdown")):
            self.scope.abs_path(path)          # 範囲外・不正なパスはここで拒否
            return path
        return normalize_rel(path)

    def _is_doc(self, path: str) -> bool:
        return path.startswith("@") or not path.lower().endswith((".md", ".markdown"))

    def _require_note(self, path: str) -> None:
        if self._is_doc(path):
            raise VaultError("資料は読み取り専用です（「ノートとして取り込む」で編集できるノートを作れます）", 403)

    # ------------------------------------------------------------ 参照
    def tree(self) -> dict:
        sources = [{"id": s.id, "label": s.label, "prefix": "" if s.id == "vault" else f"@{s.id}", "editable": s.editable,
                    "path": str(s.root)} for s in self.scope.sources()]
        return {"notes": self.index.notes(), "folders": self.vault.list_folders(), "sources": sources}

    def note(self, path: str) -> dict:
        path = self._path(path)
        if self._is_doc(path):
            return self._doc(path)
        text, version = self.vault.read(path)
        props, _, _ = L.split_frontmatter(text)
        return {
            "path": path, "title": title_of(path), "folder": folder_of(path), "kind": "note",
            "readonly": False, "text": text, "version": version, "props": props,
            "headings": L.extract_headings(text), "tags": L.extract_tags(text),
            "outgoing": self.index.outgoing(path), "backlinks": self.index.backlinks(path),
            "unlinked": self.index.unlinked_mentions(path),
        }

    def _doc(self, path: str) -> dict:
        item = self.index.get(path)
        abs_p = self.scope.abs_path(path)
        if item is None:
            if not abs_p.is_file():
                raise VaultError(f"資料が見つかりません: {path}", 404)
            item = {"text": "", "status": "pending", "error": "", "title": abs_p.name, "grp": EXT_GROUP.get(abs_p.suffix.lower(), "")}
        stale = False
        if abs_p.is_file() and item.get("mtime_ns") is not None:
            st = abs_p.stat()
            stale = (st.st_mtime_ns, st.st_size) != (item["mtime_ns"], item["size"])
        text = item["text"] or ""
        return {
            "path": path, "title": item["title"], "folder": folder_of(path), "kind": "doc",
            "grp": item.get("grp", ""), "readonly": True, "text": text,
            "version": f"{item.get('mtime_ns')}-{item.get('size')}", "props": {},
            "status": item["status"], "error": item.get("error", ""), "stale": stale,
            "exists": abs_p.is_file(), "headings": L.extract_headings(text), "tags": [],
            "outgoing": [], "backlinks": self.index.backlinks(path),
            "unlinked": self.index.unlinked_mentions(path),
            "source_path": str(abs_p), "indexed_at": item.get("indexed_at"),
        }

    def note_version(self, path: str) -> dict:
        """開いているノートが外部で変わったかを調べる軽い確認（1 ファイルだけ）。"""
        path = self._path(path)
        p = self.scope.abs_path(path)
        if not p.is_file():
            return {"path": path, "exists": False, "version": None}
        if self._is_doc(path):
            st = p.stat()
            return {"path": path, "exists": True, "version": f"{st.st_mtime_ns}-{st.st_size}"}
        return {"path": path, "exists": True, "version": version_of(p.read_bytes())}

    def links_info(self, path: str) -> dict:
        path = self._path(path)
        return {"backlinks": self.index.backlinks(path), "unlinked": self.index.unlinked_mentions(path),
                "outgoing": self.index.outgoing(path)}

    def raw_file(self, path: str) -> Path:
        path = self._path(path)
        p = self.scope.abs_path(path)
        if not p.is_file() or Path(p.name).suffix.lower() not in KINDS:
            raise VaultError("ファイルが見つかりません", 404)
        return p

    # ------------------------------------------------------------ 変更
    def save(self, rel: str, text: str, base_version: str | None) -> dict:
        rel = self._path(rel)
        self._require_note(rel)
        if not isinstance(text, str):
            raise VaultError("本文が不正です")
        existed = self.vault.exists(rel)
        ev = NoteEvent("saved" if existed else "created", rel, self._author(), text=text)
        self.plugins.before_save(ev)
        with self._lock:
            ev.version = self.vault.write(rel, text, base_version)
            self.index.refresh(rel)
        self.plugins.emit("on_saved" if existed else "on_created", ev)
        return {"path": rel, "version": ev.version}

    def create(self, rel: str | None = None, title: str = "", folder: str = "",
               text: str | None = None, template: str = "") -> dict:
        if not rel:
            title = (title or "").strip() or self._untitled(folder)
            rel = f"{folder.strip().strip('/')}/{title}" if folder.strip().strip("/") else title
        if rel.startswith("@"):
            raise VaultError("外部フォルダにはノートを作れません", 403)
        rel = normalize_rel(rel)
        if text is None:
            text = self._from_template(template, title_of(rel)) if template else f"# {title_of(rel)}\n\n"
        ev = NoteEvent("created", rel, self._author(), text=text)
        self.plugins.before_save(ev)
        with self._lock:
            ev.version = self.vault.create(rel, text)
            self.index.refresh(rel)
        self.plugins.emit("on_created", ev)
        return {"path": rel, "version": ev.version}

    def _untitled(self, folder: str) -> str:
        base = "無題のノート"
        folder = folder.strip().strip("/")
        for i in range(1, 1000):
            name = base if i == 1 else f"{base} {i}"
            rel = f"{folder}/{name}" if folder else name
            if not self.vault.exists(rel):
                return name
        return f"{base} {int(time.time())}"

    def rename(self, old: str, new: str, update_links: bool = True) -> dict:
        old = self._path(old)
        self._require_note(old)
        new = normalize_rel(new)
        if old == new:
            return {"path": new, "updated": []}
        old_title, new_title = title_of(old), title_of(new)
        # 同名が別フォルダにあると [[名前]] が曖昧になるので、その場合はパスで書く
        link_to = new_title
        with self._lock:
            referrers = [b["path"] for b in self.index.backlinks(old)] if update_links else []
            self.vault.move(old, new)
            self.index.refresh(old)
            self.index.refresh(new)
            if self.index.resolve(new_title) != new:
                link_to = new[:-3]
            updated = []
            for src in referrers:
                src = new if src == old else src
                if self._is_doc(src):
                    continue
                text, ver = self.vault.read(src)
                out = text
                for name in {old_title, old[:-3]}:
                    out, _ = L.rewrite_links(out, name, link_to)
                if out != text:
                    ev = NoteEvent("saved", src, self._author(), text=out)
                    ev.version = self.vault.write(src, out, ver)
                    self.index.refresh(src)
                    self.plugins.emit("on_saved", ev)
                    updated.append(src)
        self.plugins.emit("on_renamed", NoteEvent("renamed", new, self._author(), old_path=old))
        return {"path": new, "updated": updated}

    def delete(self, rel: str) -> dict:
        rel = self._path(rel)
        self._require_note(rel)
        with self._lock:
            where = self.vault.delete(rel)
            self.index.refresh(rel)
        self.plugins.emit("on_deleted", NoteEvent("deleted", rel, self._author()))
        return {"path": rel, "trash": where}

    def import_doc(self, path: str, folder: str = IMPORT_FOLDER) -> dict:
        """資料の本文を、編集できる Markdown ノートとして Vault に保存する。"""
        path = self._path(path)
        if not self._is_doc(path):
            raise VaultError("すでにノートです")
        d = self._doc(path)
        if d["status"] != "ok":
            raise VaultError(d["error"] or "この資料はまだ読み込まれていません（「更新」で読み込んでください）")
        stem = d["title"].rsplit(".", 1)[0] if "." in d["title"] else d["title"]
        folder = (folder or "").strip().strip("/")
        name = stem
        n = 2
        while self.vault.exists(f"{folder}/{name}" if folder else name):
            name = f"{stem} ({n})"
            n += 1
        head = (f"---\n元の資料: [[{path}]]\n取り込み日: {time.strftime('%Y-%m-%d')}\n---\n")
        body = d["text"]
        if not body.lstrip().startswith("# "):
            body = f"# {stem}\n\n{body}"
        return self.create(f"{folder}/{name}" if folder else name, text=head + body + "\n")

    # ------------------------------------------------------------ デイリーノートとテンプレート
    def daily(self, date: str | None = None) -> dict:
        date = date or time.strftime("%Y-%m-%d")
        cfg = self.config()
        folder = cfg["daily_folder"].strip().strip("/")
        rel = normalize_rel(f"{folder}/{date}" if folder else date)
        if self.vault.exists(rel):
            return {"path": rel, "created": False}
        tpl = self._template_path("デイリーノート") or self._template_path("日報")
        text = self._fill(self.vault.read(tpl)[0], date) if tpl else f"# {date}\n\n## やること\n- [ ] \n\n## メモ\n"
        self.create(rel, text=text)
        return {"path": rel, "created": True}

    def templates(self) -> list[dict]:
        """テンプレートはインデックスではなくフォルダを直接見る（読み込み範囲の外でも使える）。"""
        folder = self.config()["template_folder"].strip().strip("/")
        if not folder:
            return []
        base = self.vault.root / folder
        if not base.is_dir():
            return []
        out = []
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
            for name in sorted(filenames):
                if name.lower().endswith(".md") and not name.startswith("."):
                    rel = (Path(dirpath) / name).relative_to(self.vault.root).as_posix()
                    out.append({"path": rel, "title": title_of(rel), "folder": folder_of(rel)})
        return out

    def _template_path(self, name: str) -> str | None:
        for n in self.templates():
            if n["title"] == name or n["path"] == name:
                return n["path"]
        return None

    def _fill(self, text: str, title: str) -> str:
        return (text.replace("{{title}}", title)
                    .replace("{{date}}", time.strftime("%Y-%m-%d"))
                    .replace("{{time}}", time.strftime("%H:%M"))
                    .replace("{{author}}", self._author()))

    def _from_template(self, template: str, title: str) -> str:
        tpl = self._template_path(template) or (template if self.vault.exists(template) else None)
        if not tpl:
            raise VaultError(f"テンプレートが見つかりません: {template}", 404)
        return self._fill(self.vault.read(tpl)[0], title)

    def render_template(self, template: str, title: str) -> str:
        return self._from_template(template, title)

    # ------------------------------------------------------------ 読み込み（インデックス）
    @staticmethod
    def _prefixes(value) -> list[str] | None:
        if value is None:
            return None
        if not isinstance(value, list):
            raise VaultError("範囲の指定が不正です")
        out = []
        for p in value:
            if isinstance(p, str):
                p = p.replace("\\", "/").strip().strip("/")
                if ".." in p.split("/"):
                    raise VaultError("範囲の指定が不正です")
                out.append(p)
        return out

    def _label(self, prefixes: list[str] | None) -> str:
        if prefixes is None:
            return "すべて"
        names = []
        for p in prefixes:
            if p == "":
                names.append("Vault")
            elif p.startswith("@") and "/" not in p:
                try:
                    names.append(self.scope.source(split_id(p)[0]).label)
                except ScopeError:
                    names.append(p)
            else:
                names.append(p.rsplit("/", 1)[-1])
        return "、".join(names[:3]) + (f" ほか {len(names) - 3} 件" if len(names) > 3 else "")

    def update_index(self, prefixes=None, wait: bool = False, label: str = "") -> dict:
        """差分だけを読み込む（新規・変更・削除）。埋め込みモデルがあれば意味検索の索引も作る。"""
        prefixes = self._prefixes(prefixes)
        index, ai = self.index, self.ai

        def run(job):
            res = index.update(prefixes, cancel=job.cancel, progress=job.progress)
            res["embedded"] = 0
            if embed_configured(self.config()):
                job.progress("意味検索の索引", 0, 0, "")
                try:
                    res["embedded"] = ai.embed_pending(prefixes, cancel=job.cancel, progress=job.progress)
                except Exception as e:  # noqa: BLE001 - LLM の不調で読み込み結果は失わない
                    if e.__class__.__name__ == "Cancelled":
                        raise
                    res["embed_error"] = str(e)
            return {k: (len(v) if isinstance(v, list) else v) for k, v in res.items()}

        return self.jobs.start("update", label or f"更新（{self._label(prefixes)}）", prefixes, run, wait=wait)

    def check_index(self, prefixes=None, wait: bool = False) -> dict:
        """ファイルの日時・サイズだけを見て、未反映の変更を数える（本文は読まない）。"""
        prefixes = self._prefixes(prefixes)
        index = self.index

        def run(job):
            res = index.scan(prefixes, cancel=job.cancel, progress=job.progress)
            res.pop("_found", None)
            return {k: (len(v) if isinstance(v, list) else v) for k, v in res.items()}

        return self.jobs.start("scan", f"変更を確認（{self._label(prefixes)}）", prefixes, run, wait=wait)

    def rebuild_index(self, wait: bool = False) -> dict:
        index, ai = self.index, self.ai

        def run(job):
            res = index.rebuild(cancel=job.cancel, progress=job.progress)
            if embed_configured(self.config()):
                res["embedded"] = ai.embed_pending(None, cancel=job.cancel, progress=job.progress)
            return {k: (len(v) if isinstance(v, list) else v) for k, v in res.items()}

        return self.jobs.start("update", "作り直し（すべて）", None, run, wait=wait)

    def index_status(self) -> dict:
        st = self.index.status()
        st["job"] = self.jobs.status()
        st["pdf_available"] = pdf_available()
        return st

    # ------------------------------------------------------------ AI 取り込み
    def ingest_save(self, ids: list[str]) -> dict:
        """下書きをノートとして保存する。埋め込みモデルがあれば、保存したものだけ意味検索に登録する。"""
        res = self.ingest.save(ids)
        res["embedding"] = False
        if res["paths"] and embed_configured(self.config()):
            try:
                self.update_index(res["paths"], label="取り込んだノートの登録")
                res["embedding"] = True
            except Exception:  # noqa: BLE001 - 他の処理中なら次の「更新」で登録される
                pass
        return res

    # ------------------------------------------------------------ 読み込み範囲
    def scope_info(self) -> dict:
        srcs = []
        for s in self.scope.sources():
            prefix = "" if s.id == "vault" else f"@{s.id}"
            srcs.append({"id": s.id, "label": s.label, "path": str(s.root), "editable": s.editable,
                         "prefix": prefix, "exists": s.root.is_dir(), "stats": self.index.folder_stats(prefix)})
        return {"sources": srcs, "exclude": self.scope.data["exclude"], "types": self.scope.data["types"],
                "max_mb": self.scope.data["max_mb"],
                "type_groups": [{"id": k, "label": v["label"], "exts": v["exts"]} for k, v in TYPE_GROUPS.items()],
                "pdf_available": pdf_available(), "status": self.index_status()}

    def save_scope(self, update: dict) -> dict:
        self.scope.save(update)
        return self.scope_info()

    def scope_tree(self, path: str) -> dict:
        """フォルダ 1 階層分。各フォルダ・ファイルに読み込み状態を付ける。"""
        listing = self.scope.list_dir(path)
        for f in listing["folders"]:
            f["stats"] = self.index.folder_stats(f["path"])
        states = self.index.item_states([f["path"] for f in listing["files"]])
        for f in listing["files"]:
            it = states.get(f["path"])
            pend = self.index.pending.get(f["path"])
            if f["state"] != "in":
                f["status"] = "indexed_out" if it else f["state"]   # 範囲外なのに読込済み → 更新で消える
            elif it is None:
                f["status"] = "new"
            elif it["status"] != "ok":
                f["status"] = "error"
                f["error"] = it["error"]
            elif pend == "modified":
                f["status"] = "modified"
            else:
                f["status"] = "indexed"
        listing["stats"] = self.index.folder_stats(path if path else "")
        return listing

    @staticmethod
    def browse(path: str) -> dict:
        return browse(path)
