"""アプリ本体: Vault・インデックス・AI・プラグインをまとめ、HTTP 層から呼ばれる操作を提供する。"""
from __future__ import annotations

import shutil
import threading
import time
from pathlib import Path

from . import links as L
from .ai import AIService
from .config import load_config, save_config, vault_path
from .index import Index, open_index
from .plugins import NoteEvent, PluginContext, PluginManager
from .vault import Vault, VaultError, folder_of, normalize_rel, title_of

BASE = Path(__file__).resolve().parent.parent
PLUGIN_DIR = BASE / "plugins"
SAMPLE_DIR = BASE / "sample_vault"


class MycelApp:
    def __init__(self, config_path: Path | None = None, vault_override: str | None = None,
                 seed_sample: bool = True, plugin_dir: Path | None = None):
        self.config_path = config_path
        self.vault_override = vault_override
        self.seed_sample = seed_sample
        self.plugins = PluginManager(plugin_dir or PLUGIN_DIR)
        self._lock = threading.RLock()
        self.vault: Vault
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
        with self._lock:
            cfg = self.config()
            root = Path(self.vault_override).expanduser() if self.vault_override else vault_path(cfg)
            fresh = not root.exists() or not any(root.iterdir())
            if fresh and self.seed_sample and SAMPLE_DIR.is_dir():
                shutil.copytree(SAMPLE_DIR, root, dirs_exist_ok=True)
            if getattr(self, "index", None):
                self.index.close()
            self.vault = Vault(root)
            self.index = open_index(self.vault)
            self.ai = AIService(self.index, self.config)
            self.plugins.load(cfg["plugins"], PluginContext(self))
            self.plugins.emit("on_vault_opened")

    def _author(self) -> str:
        return self.config()["user_name"]

    # ------------------------------------------------------------ 参照
    def tree(self) -> dict:
        self.index.sync()
        return {"notes": self.index.notes(), "folders": self.vault.list_folders()}

    def note(self, rel: str) -> dict:
        rel = normalize_rel(rel)
        self.index.sync()
        text, version = self.vault.read(rel)
        props, _, _ = L.split_frontmatter(text)
        return {
            "path": rel, "title": title_of(rel), "folder": folder_of(rel),
            "text": text, "version": version, "props": props,
            "headings": L.extract_headings(text),
            "tags": L.extract_tags(text),
            "outgoing": self.index.outgoing(rel),
            "backlinks": self.index.backlinks(rel),
            "unlinked": self.index.unlinked_mentions(rel),
        }

    def links_info(self, rel: str) -> dict:
        rel = normalize_rel(rel)
        return {"backlinks": self.index.backlinks(rel), "unlinked": self.index.unlinked_mentions(rel),
                "outgoing": self.index.outgoing(rel)}

    # ------------------------------------------------------------ 変更
    def save(self, rel: str, text: str, base_version: str | None) -> dict:
        rel = normalize_rel(rel)
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
        old, new = normalize_rel(old), normalize_rel(new)
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
        rel = normalize_rel(rel)
        with self._lock:
            where = self.vault.delete(rel)
            self.index.refresh(rel)
        self.plugins.emit("on_deleted", NoteEvent("deleted", rel, self._author()))
        return {"path": rel, "trash": where}

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
        folder = self.config()["template_folder"].strip().strip("/")
        if not folder:
            return []
        return [n for n in self.index.notes() if n["folder"] == folder or n["folder"].startswith(folder + "/")]

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
