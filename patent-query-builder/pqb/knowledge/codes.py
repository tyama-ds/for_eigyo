"""分類コード（FI／Fターム／IPC）の正規化・階層・辞書照合。企画書 §9.6・§10.4。

粒度（粗 → 細）
  FI  : subclass → main_group → subgroup → expansion（展開記号）→ volume（分冊識別記号）
  FT  : theme → theme_view → full
  IPC : subclass → main_group → subgroup

書式は仮（Q5 の分類表データで確定）。ここでは
  FI  "C22C38/00,301@A"  IPC "C22C38/00"  FT "4K037AA01" を想定する。
"""
from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field

from ..core.document import SCHEMES

LEVELS = {
    "FI": ["subclass", "main_group", "subgroup", "expansion", "volume"],
    "IPC": ["subclass", "main_group", "subgroup"],
    "FT": ["theme", "theme_view", "full"],
}

_FI_RE = re.compile(r"^([A-H]\d{2}[A-Z])(?:(\d{1,4})/(\d{2,6})(?:,(\d{3}))?(?:@([A-Z]))?)?$")
_IPC_RE = re.compile(r"^([A-H]\d{2}[A-Z])(?:(\d{1,4})/(\d{2,6}))?$")
_FT_RE = re.compile(r"^(\d[A-Z]\d{3})(?:([A-Z]{2})(?:(\d{2}))?)?$")


def normalize(scheme: str, code: str) -> str:
    """空白除去・大文字化・全角→半角。"""
    import unicodedata
    text = unicodedata.normalize("NFKC", str(code or "")).upper()
    text = re.sub(r"\s+", "", text)
    text = text.replace("／", "/").replace("，", ",")
    return text


def levels(scheme: str, code: str) -> list[tuple[str, str]]:
    """粗い順に (粒度名, その粒度でのコード) を返す。書式不明なら [("raw", code)]。"""
    code = normalize(scheme, code)
    scheme = scheme.upper()
    if scheme in ("FI", "IPC"):
        m = (_FI_RE if scheme == "FI" else _IPC_RE).match(code)
        if not m:
            return [("raw", code)] if code else []
        sub, grp, sg = m.group(1), m.group(2), m.group(3)
        out = [("subclass", sub)]
        if grp:
            out.append(("main_group", f"{sub}{grp}/00"))
            if sg and sg != "00":
                out.append(("subgroup", f"{sub}{grp}/{sg}"))
            if scheme == "FI":
                exp = m.group(4)
                vol = m.group(5)
                base = f"{sub}{grp}/{sg}"
                if exp:
                    out.append(("expansion", f"{base},{exp}"))
                if vol:
                    out.append(("volume", f"{base}{',' + exp if exp else ''}@{vol}"))
        return out
    if scheme == "FT":
        m = _FT_RE.match(code)
        if not m:
            return [("raw", code)] if code else []
        theme, view, num = m.group(1), m.group(2), m.group(3)
        out = [("theme", theme)]
        if view:
            out.append(("theme_view", theme + view))
            if num:
                out.append(("full", theme + view + num))
        return out
    return [("raw", code)] if code else []


def level_of(scheme: str, code: str) -> str:
    lv = levels(scheme, code)
    return lv[-1][0] if lv else "raw"


def code_at_level(scheme: str, code: str, level: str) -> str | None:
    for name, value in levels(scheme, code):
        if name == level:
            return value
    return None


def coarser(scheme: str, code: str) -> str | None:
    """一段粗いコード。最粗なら None。"""
    lv = levels(scheme, code)
    return lv[-2][1] if len(lv) >= 2 else None


def is_valid_format(scheme: str, code: str) -> bool:
    lv = levels(scheme, code)
    return bool(lv) and lv[0][0] != "raw"


def matches(scheme: str, query_code: str, doc_code: str) -> bool:
    """検索式のコード（任意の粒度）が文献のコードに階層的に一致するか。"""
    q_levels = levels(scheme, query_code)
    if not q_levels:
        return False
    q_level, q_value = q_levels[-1]
    if q_level == "raw":
        return normalize(scheme, doc_code).startswith(q_value)
    d_value = code_at_level(scheme, doc_code, q_level)
    return d_value == q_value


@dataclass
class CodeEntry:
    scheme: str
    code: str
    title: str = ""
    parent: str = ""
    level: str = ""
    origin: str = "official"   # official | observed


@dataclass
class CodeDictionary:
    """分類表辞書。公式データが無い場合は観測コードだけで動く縮退モード（§15）。"""
    entries: dict = field(default_factory=dict)  # (scheme, code) -> CodeEntry

    # -------------------------------------------------------------- 読み込み
    def load_csv_text(self, text: str, origin: str = "official") -> int:
        """列: scheme, code, title, parent, level（ヘッダ必須。parent/level は省略可）。"""
        reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
        n = 0
        for row in reader:
            scheme = normalize("", row.get("scheme") or row.get("体系") or "")
            code = normalize(scheme, row.get("code") or row.get("コード") or "")
            if scheme not in SCHEMES or not code:
                continue
            title = (row.get("title") or row.get("タイトル") or "").strip()
            parent = normalize(scheme, row.get("parent") or row.get("親") or "") or (coarser(scheme, code) or "")
            level = (row.get("level") or row.get("粒度") or "").strip() or level_of(scheme, code)
            self.entries[(scheme, code)] = CodeEntry(scheme, code, title, parent, level, origin)
            n += 1
        return n

    def load_rows(self, rows) -> None:
        for r in rows:
            scheme, code = r["scheme"], normalize(r["scheme"], r["code"])
            self.entries[(scheme, code)] = CodeEntry(
                scheme, code, r.get("title") or "", r.get("parent") or "",
                r.get("level") or level_of(scheme, code), r.get("origin") or "official")

    def observe(self, scheme: str, code: str, title: str = "") -> None:
        """既知文献や CSV に現れたコードを「観測」として登録（全粒度）。"""
        for level, value in levels(scheme, code):
            if level == "raw":
                continue
            key = (scheme, value)
            if key not in self.entries:
                self.entries[key] = CodeEntry(scheme, value, title, coarser(scheme, value) or "",
                                              level, "observed")

    # -------------------------------------------------------------- 参照
    @property
    def degraded(self) -> bool:
        return not any(e.origin == "official" for e in self.entries.values())

    def known(self, scheme: str, code: str) -> bool:
        """辞書照合。公式データがあれば公式エントリのみを既知とし、無ければ観測コードで縮退運転。"""
        e = self.entries.get((scheme, normalize(scheme, code)))
        if e is None:
            return False
        return e.origin == "official" or self.degraded

    def lookup(self, scheme: str, code: str) -> CodeEntry | None:
        return self.entries.get((scheme, normalize(scheme, code)))

    def title(self, scheme: str, code: str) -> str:
        e = self.lookup(scheme, code)
        return e.title if e else ""

    def parents(self, scheme: str, code: str) -> list[str]:
        return [v for _, v in levels(scheme, code)[:-1]]

    def children(self, scheme: str, code: str) -> list[str]:
        code = normalize(scheme, code)
        return sorted(e.code for e in self.entries.values()
                      if e.scheme == scheme and e.parent == code)

    def rows(self) -> list[dict]:
        return [{"scheme": e.scheme, "code": e.code, "title": e.title, "parent": e.parent,
                 "level": e.level, "origin": e.origin}
                for e in sorted(self.entries.values(), key=lambda e: (e.scheme, e.code))]

    def __len__(self) -> int:
        return len(self.entries)
