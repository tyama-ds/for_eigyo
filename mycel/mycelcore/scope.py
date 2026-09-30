"""読み込み範囲（どのフォルダ・どの形式を読み込むか）。

設定は Vault ごとに ``<vault>/.mycel/scope.json`` に保存する。

- ソース: Vault 本体（常にあり・ノートは編集可）と、追加した外部フォルダ（読み取り専用）
- 除外: パス（フォルダまたはファイル）の一覧。フォルダを除外すると配下すべてが対象外
- 形式: extract.TYPE_GROUPS のうち読み込むもの
- サイズ上限: これより大きいファイルは読まない

パスの表記
- Vault 内: ``顧客/A社.md`` のような Vault からの相対パス
- 外部フォルダ内: ``@<ソースID>/資料/見積.pdf``
"""
from __future__ import annotations

import json
import os
import re
import threading
from dataclasses import dataclass
from pathlib import Path

from .extract import EXT_GROUP, TYPE_GROUPS

VAULT_ID = "vault"
DEFAULT_SCOPE = {
    "sources": [],                       # [{"id": "s1", "path": "D:/共有/営業資料", "label": "営業資料"}]
    "exclude": [],                       # ["テンプレート/古い", "@s1/アーカイブ"]
    "types": list(TYPE_GROUPS),
    "max_mb": 50,
}
_lock = threading.Lock()


class ScopeError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class Source:
    id: str
    root: Path
    label: str
    editable: bool

    def prefix(self) -> str:
        return "" if self.id == VAULT_ID else f"@{self.id}/"


def _norm_ex(p: str) -> str:
    return p.replace("\\", "/").strip().strip("/")


def split_id(path: str) -> tuple[str, str]:
    """``@s1/a/b.pdf`` → ("s1", "a/b.pdf")、``a/b.md`` → ("vault", "a/b.md")"""
    if path.startswith("@"):
        sid, _, rel = path[1:].partition("/")
        return sid, rel
    return VAULT_ID, path


def is_under(path: str, prefix: str) -> bool:
    prefix = prefix.rstrip("/")
    if not prefix:
        return True
    return path == prefix or path.startswith(prefix + "/")


class Scope:
    def __init__(self, vault_root: Path, internal_dir: Path):
        self.vault_root = vault_root
        self.file = internal_dir / "scope.json"
        self.data = self._load()

    # ------------------------------------------------------------ 設定
    def _load(self) -> dict:
        data = json.loads(json.dumps(DEFAULT_SCOPE))
        if self.file.exists():
            try:
                saved = json.loads(self.file.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                saved = {}
            if isinstance(saved, dict):
                for key, default in DEFAULT_SCOPE.items():
                    if isinstance(saved.get(key), type(default)):
                        data[key] = saved[key]
        data["types"] = [t for t in data["types"] if t in TYPE_GROUPS]
        return data

    def save(self, update: dict) -> dict:
        data = json.loads(json.dumps(self.data))
        if isinstance(update.get("types"), list):
            data["types"] = [t for t in update["types"] if t in TYPE_GROUPS]
        if "max_mb" in update:
            try:
                data["max_mb"] = max(1, min(2048, int(float(update["max_mb"]))))
            except (TypeError, ValueError):
                pass
        if isinstance(update.get("exclude"), list):
            data["exclude"] = sorted({_norm_ex(p) for p in update["exclude"]
                                      if isinstance(p, str) and _norm_ex(p)})
        if isinstance(update.get("sources"), list):
            data["sources"] = self._validate_sources(update["sources"])
            valid = {s["id"] for s in data["sources"]}
            data["exclude"] = [e for e in data["exclude"]
                               if not e.startswith("@") or split_id(e)[0] in valid]
        with _lock:
            self.file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        self.data = data
        return data

    def _validate_sources(self, items: list) -> list[dict]:
        out, used = [], set()
        vault = self.vault_root.resolve()
        for it in items:
            if not isinstance(it, dict) or not isinstance(it.get("path"), str):
                continue
            p = Path(it["path"].strip()).expanduser()
            if not p.is_dir():
                raise ScopeError(f"フォルダが見つかりません: {it['path']}")
            p = p.resolve()
            if p == vault or vault in p.parents or p in vault.parents:
                raise ScopeError("Vault と重なるフォルダは追加できません（Vault 内のフォルダは最初から対象です）")
            for o in out:
                op = Path(o["path"])
                if p == op or op in p.parents or p in op.parents:
                    raise ScopeError(f"すでに追加したフォルダと重なっています: {p}")
            sid = str(it.get("id") or "")
            if not re.fullmatch(r"s\d{1,4}", sid) or sid in used:
                n = 1
                while f"s{n}" in used or any(x.get("id") == f"s{n}" for x in items if x is not it):
                    n += 1
                sid = f"s{n}"
            used.add(sid)
            label = str(it.get("label") or p.name or str(p)).strip()[:60]
            out.append({"id": sid, "path": str(p), "label": label})
        return out

    # ------------------------------------------------------------ ソース
    def sources(self) -> list[Source]:
        out = [Source(VAULT_ID, self.vault_root, "Vault", True)]
        for s in self.data["sources"]:
            out.append(Source(s["id"], Path(s["path"]), s["label"], False))
        return out

    def source(self, sid: str) -> Source:
        for s in self.sources():
            if s.id == sid:
                return s
        raise ScopeError(f"読み込み範囲にないフォルダです: @{sid}", 404)

    def abs_path(self, path: str) -> Path:
        """パス表記から実ファイルの場所を得る（ソースの外は拒否）。"""
        sid, rel = split_id(path)
        src = self.source(sid)
        rel = rel.replace("\\", "/").strip("/")
        if rel and any(part in ("..", "") or part.startswith(".") for part in rel.split("/")):
            raise ScopeError(f"使えないパスです: {path}")
        p = (src.root / rel).resolve() if rel else src.root.resolve()
        root = src.root.resolve()
        if p != root and root not in p.parents:
            raise ScopeError("読み込み範囲の外は扱えません")
        return p

    # ------------------------------------------------------------ 判定
    def is_excluded(self, path: str) -> bool:
        return any(is_under(path, e) for e in self.data["exclude"])

    def type_enabled(self, name: str) -> bool:
        return EXT_GROUP.get(Path(name).suffix.lower()) in self.data["types"]

    def max_bytes(self) -> int:
        return int(self.data["max_mb"]) * 1024 * 1024

    def file_state(self, path: str, size: int) -> str:
        """in: 読み込み対象 / excluded / type_off / too_big / unsupported"""
        if not EXT_GROUP.get(Path(path).suffix.lower()):
            return "unsupported"
        if self.is_excluded(path):
            return "excluded"
        if not self.type_enabled(path):
            return "type_off"
        if size > self.max_bytes():
            return "too_big"
        return "in"

    # ------------------------------------------------------------ 走査
    def walk(self, prefixes: list[str] | None = None):
        """読み込み対象のファイルを (パス, 実パス, mtime_ns, サイズ) で列挙する。

        除外したフォルダの中には入らない（大きな共有フォルダでも速い）。
        prefixes を渡すとその配下だけを列挙する（"" は Vault 全体、"@s1" は外部フォルダ全体）。
        """
        for src in self.sources():
            for start in self._starts(src, prefixes):
                pre = src.prefix()
                if start and self.is_excluded(pre + start):
                    continue
                base = src.root / start if start else src.root
                if base.is_file():
                    yield from self._file_entry(src, base)
                    continue
                if not base.is_dir():
                    continue
                for dirpath, dirnames, filenames in os.walk(base):
                    d = Path(dirpath)
                    rel_dir = d.relative_to(src.root).as_posix()
                    rel_dir = "" if rel_dir == "." else rel_dir
                    keep = []
                    for name in sorted(dirnames):
                        child = pre + (f"{rel_dir}/{name}" if rel_dir else name)
                        if not name.startswith(".") and not self.is_excluded(child):
                            keep.append(name)
                    dirnames[:] = keep
                    for name in sorted(filenames):
                        if not name.startswith((".", "~$")):
                            yield from self._file_entry(src, d / name)

    @staticmethod
    def _starts(src: Source, prefixes: list[str] | None) -> list[str]:
        """このソースの中で走査を始める相対フォルダ（"" はソース全体）。"""
        if prefixes is None:
            return [""]
        starts = []
        for p in prefixes:
            sid, rel = split_id(p) if p.startswith("@") else (VAULT_ID, p)
            if sid == src.id:
                starts.append(rel.strip("/"))
        starts = sorted(set(starts), key=len)
        out = []
        for s in starts:
            if not any(is_under(s, o) for o in out):
                out.append(s)
        return out

    def _file_entry(self, src: Source, p: Path):
        rel = p.relative_to(src.root).as_posix()
        path = src.prefix() + rel
        if not EXT_GROUP.get(p.suffix.lower()):
            return
        try:
            st = p.stat()
        except OSError:
            return
        if self.file_state(path, st.st_size) == "in":
            yield path, p, st.st_mtime_ns, st.st_size

    def list_dir(self, path: str) -> dict:
        """範囲ダイアログ用: 1 階層分のフォルダとファイル（未対応形式は件数だけ）。"""
        target = self.abs_path(path) if path else self.vault_root
        if not target.is_dir():
            raise ScopeError(f"フォルダではありません: {path}", 404)
        sid, rel = split_id(path) if path else (VAULT_ID, "")
        src = self.source(sid)
        pre = src.prefix()
        folders, files, other = [], [], 0
        try:
            entries = sorted(os.scandir(target), key=lambda e: e.name)
        except OSError as e:
            raise ScopeError(f"フォルダを開けません: {e}") from e
        for e in entries:
            if e.name.startswith(".") or e.name.startswith("~$"):
                continue
            child_rel = f"{rel.strip('/')}/{e.name}" if rel.strip("/") else e.name
            child = pre + child_rel
            try:
                if e.is_dir():
                    folders.append({"path": child, "name": e.name, "excluded": self.is_excluded(child)})
                elif e.is_file():
                    if not EXT_GROUP.get(Path(e.name).suffix.lower()):
                        other += 1
                        continue
                    st = e.stat()
                    files.append({"path": child, "name": e.name, "size": st.st_size,
                                  "group": EXT_GROUP[Path(e.name).suffix.lower()],
                                  "state": self.file_state(child, st.st_size)})
            except OSError:
                continue
        return {"path": path, "folders": folders, "files": files, "unsupported": other}


def browse(path: str) -> dict:
    """外部フォルダを追加するためのフォルダ選択（サーバ側の一覧）。"""
    if not path:
        if os.name == "nt":
            import string
            drives = [f"{d}:\\" for d in string.ascii_uppercase if os.path.exists(f"{d}:\\")]
            return {"path": "", "parent": None, "dirs": [{"name": d, "path": d} for d in drives]}
        path = str(Path.home())
    p = Path(path).expanduser()
    if not p.is_dir():
        raise ScopeError(f"フォルダが見つかりません: {path}", 404)
    p = p.resolve()
    dirs = []
    try:
        for e in sorted(os.scandir(p), key=lambda e: e.name.lower()):
            if e.name.startswith("."):
                continue
            try:
                if e.is_dir():
                    dirs.append({"name": e.name, "path": str(Path(e.path))})
            except OSError:
                continue
    except OSError as e:
        raise ScopeError(f"フォルダを開けません: {e}") from e
    parent = str(p.parent) if p.parent != p else ("" if os.name == "nt" else None)
    return {"path": str(p), "parent": parent, "dirs": dirs[:500]}
