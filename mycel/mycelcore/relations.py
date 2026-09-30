"""資料同士（資料とノートも可）の「つながり」。

資料（PDF・Word など）は中に [[リンク]] を書けないので、つながりは本文の外に持つ。
``<vault>/.mycel/relations.json`` に保存する利用者のデータで、インデックス（キャッシュ）とは別。
向きは持つが、画面では双方向のつながりとして扱う。

    {"src": "資料/見積書.docx", "dst": "@s1/提案/B社.pptx", "label": "改訂前", "origin": "user", "created": 1.7e9}
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from .scope import is_under

_lock = threading.RLock()


def _moved(path: str, old: str, new: str) -> str | None:
    """path が old（ファイルまたはフォルダ）の配下なら、移動後のパスを返す。"""
    if path == old:
        return new
    if is_under(path, old):
        return new + path[len(old):]
    return None


class Relations:
    def __init__(self, internal_dir: Path):
        self.file = internal_dir / "relations.json"
        self.items: list[dict] = self._load()

    def _load(self) -> list[dict]:
        try:
            data = json.loads(self.file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        return [r for r in data if isinstance(r, dict) and isinstance(r.get("src"), str)
                and isinstance(r.get("dst"), str)] if isinstance(data, list) else []

    def _save(self) -> None:
        tmp = self.file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.items, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.file)

    @staticmethod
    def _same(r: dict, a: str, b: str) -> bool:
        return {r["src"], r["dst"]} == {a, b}

    # ------------------------------------------------------------ 追加・削除
    def add(self, src: str, dst: str, label: str = "", origin: str = "user") -> dict:
        if src == dst:
            raise ValueError("同じファイル同士はつなげません")
        label = (label or "").strip()[:40]
        with _lock:
            for r in self.items:
                if self._same(r, src, dst):
                    if label:
                        r["label"] = label
                    if origin == "user":
                        r["origin"] = "user"          # AI の提案を利用者が確かめた
                    self._save()
                    return r
            r = {"src": src, "dst": dst, "label": label, "origin": origin, "created": time.time()}
            self.items.append(r)
            self._save()
            return r

    def remove(self, a: str, b: str) -> bool:
        with _lock:
            n = len(self.items)
            self.items = [r for r in self.items if not self._same(r, a, b)]
            if len(self.items) != n:
                self._save()
            return len(self.items) != n

    def drop(self, path: str) -> int:
        """ファイル（またはフォルダ配下）を消したとき、そのつながりも消す。"""
        with _lock:
            n = len(self.items)
            self.items = [r for r in self.items
                          if _moved(r["src"], path, "") is None and _moved(r["dst"], path, "") is None]
            if len(self.items) != n:
                self._save()
            return n - len(self.items)

    def rename(self, old: str, new: str) -> int:
        """ファイル・フォルダの移動に合わせてパスを書き換える。"""
        changed = 0
        with _lock:
            for r in self.items:
                for k in ("src", "dst"):
                    m = _moved(r[k], old, new)
                    if m is not None:
                        r[k] = m
                        changed += 1
            if changed:
                self._save()
        return changed

    # ------------------------------------------------------------ 参照
    def for_path(self, path: str) -> list[dict]:
        out = []
        for r in self.items:
            if path in (r["src"], r["dst"]):
                out.append({"path": r["dst"] if r["src"] == path else r["src"], "label": r.get("label", ""),
                            "origin": r.get("origin", "user"), "outgoing": r["src"] == path,
                            "created": r.get("created")})
        return out

    def pairs(self) -> list[tuple[str, str]]:
        return [(r["src"], r["dst"]) for r in self.items]

    def count(self, path: str) -> int:
        return sum(1 for r in self.items if path in (r["src"], r["dst"]))

    def exists(self, a: str, b: str) -> bool:
        return any(self._same(r, a, b) for r in self.items)
