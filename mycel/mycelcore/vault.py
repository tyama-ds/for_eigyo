"""Vault（ノートを置くフォルダ）の読み書き。

- ファイルが唯一の正本。インデックスはいつでも作り直せるキャッシュ
- パスは Vault からの相対パス（``/`` 区切り、``.md`` 付き）で扱う
- ``..`` や ``.`` で始まるフォルダ（``.mycel`` など）へのアクセスは拒否
- 保存は一時ファイル＋置き換えで行い、途中で落ちても壊れない
- 版（version）は内容のハッシュ。保存時に ``base_version`` を渡すと、
  ほかの場所（別タブ・別アプリ・将来の共有相手）が先に書き換えていた場合に
  ``ConflictError`` になる（楽観ロック）
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import time
from pathlib import Path

INTERNAL_DIR = ".mycel"
_BAD_CHARS_RE = re.compile(r'[<>:"|?*\x00-\x1f]')


class VaultError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class ConflictError(VaultError):
    def __init__(self, current_text: str, current_version: str):
        super().__init__("ほかの場所でこのノートが変更されています", 409)
        self.current_text = current_text
        self.current_version = current_version


def version_of(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()[:16]


def title_of(rel: str) -> str:
    return rel.rsplit("/", 1)[-1][:-3] if rel.endswith(".md") else rel.rsplit("/", 1)[-1]


def folder_of(rel: str) -> str:
    return rel.rsplit("/", 1)[0] if "/" in rel else ""


def normalize_rel(rel: str) -> str:
    """UI から来たパスを正規化・検証する（``.md`` は自動で補う）。"""
    if not isinstance(rel, str):
        raise VaultError("パスが不正です")
    rel = rel.replace("\\", "/").strip().strip("/")
    if not rel:
        raise VaultError("ノート名が空です")
    if not rel.lower().endswith(".md"):
        rel += ".md"
    parts = rel.split("/")
    for part in parts:
        name = part.strip()
        if not name or name in (".", "..") or name.startswith("."):
            raise VaultError(f"使えないパスです: {rel}")
        if _BAD_CHARS_RE.search(part):
            raise VaultError(f'ノート名に使えない文字が含まれています（< > : " | ? *）: {part}')
        if name != part:
            raise VaultError(f"名前の前後に空白は使えません: {part!r}")
    if len(rel) > 240:
        raise VaultError("パスが長すぎます")
    return "/".join(parts)


class Vault:
    def __init__(self, root: Path | str):
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / INTERNAL_DIR).mkdir(exist_ok=True)

    @property
    def internal(self) -> Path:
        return self.root / INTERNAL_DIR

    # ------------------------------------------------------------ パス
    def path(self, rel: str) -> Path:
        rel = normalize_rel(rel)
        p = (self.root / rel).resolve()
        if p != self.root and self.root not in p.parents:
            raise VaultError("Vault の外は扱えません")
        return p

    def rel(self, p: Path) -> str:
        return p.resolve().relative_to(self.root).as_posix()

    def exists(self, rel: str) -> bool:
        return self.path(rel).is_file()

    # ------------------------------------------------------------ 一覧
    def list_files(self) -> list[tuple[str, int, int]]:
        """(相対パス, mtime_ns, サイズ) の一覧。ドットで始まるフォルダは除外。"""
        out = []
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
            for name in filenames:
                if name.startswith(".") or not name.lower().endswith(".md"):
                    continue
                p = Path(dirpath) / name
                try:
                    st = p.stat()
                except OSError:
                    continue
                out.append((p.relative_to(self.root).as_posix(), st.st_mtime_ns, st.st_size))
        return out

    def list_folders(self) -> list[str]:
        out = []
        for dirpath, dirnames, _ in os.walk(self.root):
            dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
            for d in dirnames:
                out.append((Path(dirpath) / d).relative_to(self.root).as_posix())
        return out

    # ------------------------------------------------------------ 読み書き
    def read(self, rel: str) -> tuple[str, str]:
        p = self.path(rel)
        if not p.is_file():
            raise VaultError(f"ノートが見つかりません: {rel}", 404)
        data = p.read_bytes()
        return data.decode("utf-8", errors="replace"), version_of(data)

    def write(self, rel: str, text: str, base_version: str | None = None) -> str:
        p = self.path(rel)
        if base_version is not None and p.is_file():
            cur_text, cur_ver = self.read(rel)
            if cur_ver != base_version:
                raise ConflictError(cur_text, cur_ver)
        data = text.encode("utf-8")
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(f".{p.name}.{os.getpid()}.tmp")
        tmp.write_bytes(data)
        os.replace(tmp, p)
        return version_of(data)

    def create(self, rel: str, text: str = "") -> str:
        p = self.path(rel)
        if p.exists():
            raise VaultError(f"同じ名前のノートがあります: {normalize_rel(rel)}", 409)
        return self.write(rel, text)

    def move(self, old: str, new: str) -> None:
        src, dst = self.path(old), self.path(new)
        if not src.is_file():
            raise VaultError(f"ノートが見つかりません: {old}", 404)
        if dst.exists() and dst != src:
            # 大文字小文字だけの変更（Windows）は許す
            if not (os.path.normcase(str(dst)) == os.path.normcase(str(src))):
                raise VaultError(f"同じ名前のノートがあります: {normalize_rel(new)}", 409)
        dst.parent.mkdir(parents=True, exist_ok=True)
        os.replace(src, dst)
        self._prune_empty(src.parent)

    def delete(self, rel: str) -> str:
        """削除はゴミ箱（.mycel/trash）へ移動する。戻り値はゴミ箱内の場所。"""
        src = self.path(rel)
        if not src.is_file():
            raise VaultError(f"ノートが見つかりません: {rel}", 404)
        trash = self.internal / "trash" / time.strftime("%Y%m%d-%H%M%S")
        dst = trash / normalize_rel(rel)
        n = 2
        while dst.exists():                               # 同じ秒に同名を消した場合
            dst = trash / normalize_rel(f"{rel[:-3] if rel.endswith('.md') else rel} ({n})")
            n += 1
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        self._prune_empty(src.parent)
        return dst.relative_to(self.root).as_posix()

    def _prune_empty(self, d: Path) -> None:
        while d != self.root and self.root in d.parents:
            try:
                d.rmdir()
            except OSError:
                return
            d = d.parent
