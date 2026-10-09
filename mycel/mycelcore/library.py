"""文献管理モード: 論文・レポート・書籍などを「文献」として登録し、文献だけを対象に検索・質問・分解表示する。

- 文献は書誌情報（題名・著者・年・掲載誌・DOI・要旨・キーワード・タグ・読了状態・評価）＋本文ファイル（PDF など、
  読み込み範囲にある資料かノート）＋読書ノート（任意）の組。保存先は ``<vault>/.mycel/library.json``
- 検索は文献のファイルだけを対象にした意味検索（Embed モデルがあれば埋め込み、無ければ文字バイグラム BM25）。
  文献ごとに一致した段落を返す
- 質問（RAG）も文献だけを根拠にする
- 分解グラフ: トピック（タグ・著者・年）→ 文献 → 章・ページ → 段落 と、クリックで大きい要素から小さい要素へ開ける。
  どの階層でも、内容の近い要素同士を点線で結ぶ（別の文献の章・段落ともつながる）
- BibTeX / RIS の読み込みと書き出し、引用文の生成、ローカル LLM による書誌情報の補完と構造化要約
"""
from __future__ import annotations

import csv
import io
import json
import re
import threading
import time
import unicodedata
import uuid
from collections import Counter, defaultdict

from . import links as L
from .config import chat_configured, embed_configured
from .llm import LLMClient, LLMError
from .vault import VaultError, normalize_rel

TYPES = {"article": "論文（雑誌）", "inproceedings": "論文（会議）", "book": "書籍", "incollection": "書籍の章",
         "thesis": "学位論文", "report": "報告書", "web": "Web ページ", "patent": "特許", "misc": "その他"}
STATUSES = {"unread": "未読", "reading": "読書中", "read": "読了"}
FIELDS = ("type", "title", "authors", "year", "venue", "volume", "issue", "pages", "publisher", "doi", "url",
          "abstract", "keywords", "tags", "status", "rating", "file", "note", "lang")
LIST_FIELDS = ("authors", "keywords", "tags")
SUMMARY_KEYS = ("one_line", "purpose", "method", "results", "limitations")
SUMMARY_LABELS = {"one_line": "一言で", "purpose": "目的・課題", "method": "手法・対象", "results": "結果・主張",
                  "limitations": "限界・課題"}
GROUPS = {"tag": "タグ", "author": "著者", "year": "年", "type": "種類", "status": "読了状態", "none": "分類なし"}
# 文献同士のつながりの種類。directed=True は a → b の向きを持つ（a が b を引用している、など）
LINK_TYPES = {
    "cites": {"label": "引用している", "directed": True, "inverse": "引用されている"},
    "extends": {"label": "発展させている", "directed": True, "inverse": "元になった"},
    "supports": {"label": "支持している", "directed": True, "inverse": "支持されている"},
    "refutes": {"label": "反論している", "directed": True, "inverse": "反論されている"},
    "compares": {"label": "比較対象", "directed": False, "inverse": "比較対象"},
    "same_topic": {"label": "同じテーマ", "directed": False, "inverse": "同じテーマ"},
    "related": {"label": "関連", "directed": False, "inverse": "関連"},
}
_lock = threading.RLock()
_DOI_RE = re.compile(r"\b(10\.\d{4,9}/[^\s\"<>）)\]]+)", re.I)
_YEAR_RE = re.compile(r"(?<!\d)((?:19|20)\d{2})(?!\d)")


def _norm(s) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", str(s or ""))).strip()


def _list(v, comma: bool = True) -> list[str]:
    """文字列なら区切って配列にする。著者は「Family, Given」を壊さないよう半角カンマでは切らない。"""
    if isinstance(v, str):
        v = re.split(r"\s+and\s+|[;；、，\n]" + ("|," if comma else ""), v) if v else []
    return [_norm(x) for x in (v or []) if _norm(x)]


def _title_key(s: str) -> str:
    return re.sub(r"[^0-9a-z一-龥ぁ-んァ-ヶー]", "", _norm(s).lower())


def _ascii_word(s: str) -> str:
    s = unicodedata.normalize("NFKD", s)
    return re.sub(r"[^a-z0-9]", "", s.lower())


# ---------------------------------------------------------------- BibTeX / RIS
def _unbrace(v: str) -> str:
    v = re.sub(r"\\['\"`^~=.uvHcdb]\{?(\w)\}?", r"\1", v)       # \'{e} → e（簡易）
    v = v.replace("{", "").replace("}", "").replace("--", "–").replace("\\&", "&").replace("~", " ")
    return _norm(v)


def parse_bibtex(text: str) -> list[dict]:
    out = []
    i = 0
    while True:
        m = re.search(r"@(\w+)\s*\{", text[i:])
        if not m:
            break
        etype = m.group(1).lower()
        start = i + m.end()
        depth, j = 1, start
        while j < len(text) and depth:
            depth += {"{": 1, "}": -1}.get(text[j], 0)
            j += 1
        body = text[start:j - 1]
        i = j
        if etype in ("comment", "preamble", "string"):
            continue
        key, _, rest = body.partition(",")
        fields: dict[str, str] = {}
        pos = 0
        while pos < len(rest):
            fm = re.compile(r"\s*(\w[\w\-]*)\s*=\s*").match(rest, pos)
            if not fm:
                break
            name = fm.group(1).lower()
            pos = fm.end()
            if pos < len(rest) and rest[pos] == "{":
                d, q = 1, pos + 1
                while q < len(rest) and d:
                    d += {"{": 1, "}": -1}.get(rest[q], 0)
                    q += 1
                val, pos = rest[pos + 1:q - 1], q
            elif pos < len(rest) and rest[pos] == '"':
                q = pos + 1
                while q < len(rest) and (rest[q] != '"' or rest[q - 1] == "\\"):
                    q += 1
                val, pos = rest[pos + 1:q], q + 1
            else:
                q = rest.find(",", pos)
                q = len(rest) if q == -1 else q
                val, pos = rest[pos:q], q
            fields[name] = _unbrace(val)
            cm = re.compile(r"\s*,?").match(rest, pos)
            pos = cm.end() if cm else pos
        ref = {"type": etype if etype in TYPES else {"phdthesis": "thesis", "mastersthesis": "thesis",
                                                     "techreport": "report", "online": "web", "conference": "inproceedings",
                                                     "inbook": "incollection"}.get(etype, "misc"),
               "key": _norm(key), "title": fields.get("title", ""), "authors": _list(fields.get("author", ""), comma=False),
               "year": fields.get("year", "")[:4], "venue": fields.get("journal") or fields.get("booktitle") or fields.get("school") or fields.get("institution") or fields.get("publisher", ""),
               "volume": fields.get("volume", ""), "issue": fields.get("number", ""), "pages": fields.get("pages", ""),
               "publisher": fields.get("publisher", ""), "doi": fields.get("doi", ""), "url": fields.get("url", ""),
               "abstract": fields.get("abstract", ""), "keywords": _list(fields.get("keywords", ""))}
        if ref["title"]:
            out.append(ref)
    return out


_RIS_TYPES = {"JOUR": "article", "CONF": "inproceedings", "CPAPER": "inproceedings", "BOOK": "book", "CHAP": "incollection",
              "THES": "thesis", "RPRT": "report", "ELEC": "web", "PAT": "patent"}


def parse_ris(text: str) -> list[dict]:
    out, cur = [], None
    for line in text.splitlines():
        m = re.match(r"^([A-Z][A-Z0-9])\s{1,2}-\s?(.*)$", line.strip("\ufeff"))
        if not m:
            continue
        tag, val = m.group(1), _norm(m.group(2))
        if tag == "TY":
            cur = {"type": _RIS_TYPES.get(val, "misc"), "authors": [], "keywords": [], "title": "", "year": "", "venue": "",
                   "volume": "", "issue": "", "pages": "", "publisher": "", "doi": "", "url": "", "abstract": ""}
            continue
        if cur is None:
            continue
        if tag == "ER":
            if cur["title"]:
                out.append(cur)
            cur = None
        elif tag in ("AU", "A1", "A2"):
            cur["authors"].append(val)
        elif tag in ("TI", "T1") and not cur["title"]:
            cur["title"] = val
        elif tag in ("PY", "Y1") and not cur["year"]:
            cur["year"] = (re.search(r"\d{4}", val) or [""])[0]
        elif tag in ("JO", "JF", "T2", "BT") and not cur["venue"]:
            cur["venue"] = val
        elif tag == "VL":
            cur["volume"] = val
        elif tag == "IS":
            cur["issue"] = val
        elif tag == "SP":
            cur["pages"] = val + (("-" + cur["pages"]) if cur["pages"] else "")
        elif tag == "EP":
            cur["pages"] = (cur["pages"] + "-" if cur["pages"] and "-" not in cur["pages"] else "") + val
        elif tag == "PB":
            cur["publisher"] = val
        elif tag == "DO":
            cur["doi"] = val
        elif tag == "UR" and not cur["url"]:
            cur["url"] = val
        elif tag in ("AB", "N2") and not cur["abstract"]:
            cur["abstract"] = val
        elif tag == "KW":
            cur["keywords"].append(val)
    if cur and cur["title"]:
        out.append(cur)
    return out


def to_bibtex(ref: dict) -> str:
    etype = {"thesis": "phdthesis", "report": "techreport", "web": "misc", "patent": "misc"}.get(ref["type"], ref["type"])
    f = [("title", ref["title"]), ("author", " and ".join(ref["authors"])), ("year", ref["year"])]
    venue_field = {"article": "journal", "inproceedings": "booktitle", "incollection": "booktitle", "thesis": "school",
                   "report": "institution"}.get(ref["type"], "howpublished" if ref["type"] == "web" else "publisher")
    f += [(venue_field, ref["venue"]), ("volume", ref["volume"]), ("number", ref["issue"]), ("pages", ref["pages"]),
          ("publisher", ref["publisher"] if venue_field != "publisher" else ""), ("doi", ref["doi"]), ("url", ref["url"]),
          ("abstract", ref["abstract"]), ("keywords", ", ".join(ref["keywords"]))]
    body = ",\n".join(f"  {k} = {{{v}}}" for k, v in f if v)
    return f"@{etype}{{{ref['key']},\n{body}\n}}"


def to_ris(ref: dict) -> str:
    ty = {v: k for k, v in _RIS_TYPES.items()}.get(ref["type"], "GEN")
    lines = [f"TY  - {ty}"] + [f"AU  - {a}" for a in ref["authors"]] + [f"TI  - {ref['title']}"]
    for tag, key in (("PY", "year"), ("T2", "venue"), ("VL", "volume"), ("IS", "issue"), ("PB", "publisher"),
                     ("DO", "doi"), ("UR", "url"), ("AB", "abstract")):
        if ref.get(key):
            lines.append(f"{tag}  - {ref[key]}")
    if ref.get("pages"):
        sp, _, ep = ref["pages"].replace("–", "-").partition("-")
        lines.append(f"SP  - {sp}")
        if ep:
            lines.append(f"EP  - {ep}")
    lines += [f"KW  - {k}" for k in ref["keywords"]] + ["ER  - "]
    return "\n".join(lines)


def citation(ref: dict, style: str = "apa") -> str:
    a = ref["authors"]
    if style == "ieee":
        names = ", ".join(a[:3]) + (" et al." if len(a) > 3 else "")
        parts = [names, f"“{ref['title']},”" if ref["title"] else "", ref["venue"],
                 f"vol. {ref['volume']}" if ref["volume"] else "", f"no. {ref['issue']}" if ref["issue"] else "",
                 f"pp. {ref['pages']}" if ref["pages"] else "", ref["year"]]
        s = ", ".join(p for p in parts if p) + "."
    else:
        if not a:
            names = ""
        elif len(a) <= 3:
            names = "、".join(a) if any(re.search(r"[一-龥ぁ-んァ-ヶ]", x) for x in a) else (", ".join(a[:-1]) + (" & " if len(a) > 1 else "") + a[-1])
        else:
            names = a[0] + (" ほか" if re.search(r"[一-龥ぁ-んァ-ヶ]", a[0]) else " et al.")
        s = f"{names} ({ref['year'] or 'n.d.'}). {ref['title']}."
        if ref["venue"]:
            s += f" {ref['venue']}"
            if ref["volume"]:
                s += f", {ref['volume']}" + (f"({ref['issue']})" if ref["issue"] else "")
            if ref["pages"]:
                s += f", {ref['pages']}"
            s += "."
    if ref["doi"]:
        s += f" https://doi.org/{ref['doi']}"
    elif ref["url"]:
        s += f" {ref['url']}"
    return re.sub(r"\s+", " ", s).strip()


# ---------------------------------------------------------------- 本体
class Library:
    def __init__(self, app):
        self.app = app
        self.file = app.vault.internal / "library.json"
        self.refs: list[dict] = self._load()
        self.links: list[dict] = self._load_links()
        self.rev = 0
        self._sim_cache: dict = {}

    # ---- 保存
    def _load(self) -> list[dict]:
        try:
            data = json.loads(self.file.read_text(encoding="utf-8"))
            refs = data.get("refs") if isinstance(data, dict) else data
            return [self._clean(r) for r in refs if isinstance(r, dict) and r.get("id")] if isinstance(refs, list) else []
        except (OSError, ValueError):
            return []

    def _load_links(self) -> list[dict]:
        try:
            data = json.loads(self.file.read_text(encoding="utf-8"))
            links = data.get("links", []) if isinstance(data, dict) else []
        except (OSError, ValueError):
            return []
        ids = {r["id"] for r in self.refs}
        return [l for l in links if isinstance(l, dict) and l.get("a") in ids and l.get("b") in ids
                and l.get("type") in LINK_TYPES and l["a"] != l["b"]]

    def _save(self) -> None:
        with _lock:
            tmp = self.file.with_suffix(".tmp")
            tmp.write_text(json.dumps({"refs": self.refs, "links": self.links, "updated": time.time()}, ensure_ascii=False, indent=1),
                           encoding="utf-8")
            tmp.replace(self.file)
            self.rev += 1
            self._sim_cache = {}

    def _clean(self, r: dict) -> dict:
        out = {"id": str(r.get("id") or uuid.uuid4().hex[:8]), "key": _norm(r.get("key", ""))[:60]}
        for f in FIELDS:
            v = r.get(f, "")
            if f in LIST_FIELDS:
                out[f] = _list(v, comma=f != "authors")
            elif f == "rating":
                try:
                    out[f] = max(0, min(5, int(v or 0)))
                except (TypeError, ValueError):
                    out[f] = 0
            elif f == "status":
                out[f] = v if v in STATUSES else "unread"
            elif f == "type":
                out[f] = v if v in TYPES else "misc"
            elif f == "year":
                out[f] = (re.search(r"\d{4}", str(v or "")) or [""])[0]
            else:
                out[f] = _norm(v) if f not in ("abstract",) else str(v or "").strip()[:20000]
        out["doi"] = re.sub(r"^https?://(dx\.)?doi\.org/", "", out["doi"], flags=re.I).strip()
        s = r.get("summary") if isinstance(r.get("summary"), dict) else {}
        out["summary"] = {k: str(s.get(k) or "").strip() for k in SUMMARY_KEYS} if s else {}
        ins = r.get("insights") if isinstance(r.get("insights"), dict) else {}
        out["insights"] = {"takeaways": [str(x)[:300] for x in ins.get("takeaways") or [] if str(x).strip()][:8],
                           "connections": [{"title": str(c.get("title") or "")[:120], "point": str(c.get("point") or "")[:300],
                                            "path": str(c.get("path") or "")} for c in ins.get("connections") or [] if isinstance(c, dict)][:8],
                           "questions": [str(x)[:300] for x in ins.get("questions") or [] if str(x).strip()][:6],
                           "actions": [str(x)[:300] for x in ins.get("actions") or [] if str(x).strip()][:6],
                           "at": float(ins.get("at") or 0)} if ins else {}
        out["added"] = float(r.get("added") or time.time())
        out["updated"] = float(r.get("updated") or out["added"])
        out["source"] = str(r.get("source") or "manual")
        return out

    # ---- 参照
    def get(self, rid: str) -> dict:
        for r in self.refs:
            if r["id"] == rid:
                return r
        raise VaultError("文献が見つかりません", 404)

    def by_path(self, path: str) -> dict | None:
        for r in self.refs:
            if path and path in (r["file"], r["note"]):
                return r
        return None

    def paths(self) -> set[str]:
        """検索・質問の対象になるファイル（本文ファイルと読書ノート）のうち、読み込み済みのもの。"""
        out = set()
        for r in self.refs:
            for p in (r["file"], r["note"]):
                if p and self.app.index.get(p):
                    out.add(p)
        return out

    def _row(self, r: dict) -> dict:
        it = self.app.index.get(r["file"]) if r["file"] else None
        return {**r, "citation": citation(r), "file_ok": bool(it and it["status"] == "ok"),
                "file_grp": it["grp"] if it else "", "file_kind": it["kind"] if it else "",
                "file_missing": bool(r["file"] and not it), "note_ok": bool(r["note"] and self.app.index.get(r["note"])),
                "has_summary": bool(r["summary"].get("purpose") or r["summary"].get("one_line") if r["summary"] else False)}

    def list(self, q: str = "", tag: str = "", year: str = "", status: str = "", author: str = "",
             rtype: str = "", sort: str = "added") -> dict:
        q = _norm(q).lower()
        rows = []
        for r in self.refs:
            if tag and tag not in r["tags"] and tag not in r["keywords"]:
                continue
            if year and r["year"] != year:
                continue
            if status and r["status"] != status:
                continue
            if rtype and r["type"] != rtype:
                continue
            if author and not any(author.lower() in a.lower() for a in r["authors"]):
                continue
            if q and q not in " ".join([r["title"], " ".join(r["authors"]), r["venue"], r["abstract"], r["key"],
                                        " ".join(r["keywords"]), " ".join(r["tags"]), r["doi"]]).lower():
                continue
            rows.append(self._row(r))
        keyf = {"added": lambda r: -r["added"], "updated": lambda r: -r["updated"], "year": lambda r: (-(int(r["year"] or 0)), r["title"]),
                "title": lambda r: r["title"].lower(), "rating": lambda r: (-r["rating"], -r["added"]),
                "author": lambda r: (r["authors"][0].lower() if r["authors"] else "~")}.get(sort, lambda r: -r["added"])
        rows.sort(key=keyf)
        return {"refs": rows, "facets": self.facets(), "count": len(self.refs)}

    def facets(self) -> dict:
        tags = Counter(t for r in self.refs for t in r["tags"])
        kws = Counter(t for r in self.refs for t in r["keywords"])
        years = Counter(r["year"] for r in self.refs if r["year"])
        authors = Counter(a for r in self.refs for a in r["authors"])
        status = Counter(r["status"] for r in self.refs)
        types = Counter(r["type"] for r in self.refs)
        return {"tags": tags.most_common(60), "keywords": kws.most_common(60), "years": sorted(years.items(), reverse=True),
                "authors": authors.most_common(40), "status": status, "types": types}

    # ---- 追加・変更・削除
    def _citekey(self, r: dict, exclude_id: str = "") -> str:
        first = r["authors"][0] if r["authors"] else ""
        surname = first.split(",")[0].strip() if "," in first else (first.split()[-1] if re.search(r"[A-Za-z]", first) and first.split() else first[:2])
        base = _ascii_word(surname) or re.sub(r"\s", "", surname)[:4] or "ref"
        word = next((_ascii_word(w) for w in re.split(r"\W+", r["title"]) if len(_ascii_word(w)) > 3
                     and _ascii_word(w) not in ("the", "and", "for", "with", "from", "that", "this", "into", "using", "based", "toward", "towards")), "")
        key = f"{base}{r['year']}{word[:12]}"
        taken = {x["key"] for x in self.refs if x["id"] != exclude_id}
        cand, n = key, 0
        while cand in taken:
            n += 1
            cand = key + "abcdefghijklmnopqrstuvwxyz"[(n - 1) % 26] * (1 + (n - 1) // 26)
        return cand

    def _dup_of(self, r: dict, exclude_id: str = "") -> dict | None:
        tk = _title_key(r.get("title", ""))
        for x in self.refs:
            if x["id"] == exclude_id:
                continue
            if r.get("doi") and x["doi"] and x["doi"].lower() == r["doi"].lower():
                return x
            if r.get("file") and x["file"] == r["file"]:
                return x
            if tk and len(tk) > 8 and _title_key(x["title"]) == tk and (not r.get("year") or not x["year"] or r["year"] == x["year"]):
                return x
        return None

    def add(self, data: dict, source: str = "manual", skip_dup: bool = False) -> dict:
        r = self._clean({**data, "id": uuid.uuid4().hex[:8]})
        if not r["title"]:
            raise VaultError("題名を入力してください")
        for p in (r["file"], r["note"]):
            if p:
                self.app._path(p)
        dup = self._dup_of(r)
        if dup:
            if skip_dup:
                return {**dup, "_dup": True}
            raise VaultError(f"同じ文献が登録済みです: {dup['title']}", 409)
        r["key"] = self._citekey(r) if not r["key"] or any(x["key"] == r["key"] for x in self.refs) else r["key"]
        r["source"] = source
        self.refs.append(r)
        self._save()
        return r

    def update(self, rid: str, data: dict) -> dict:
        r = self.get(rid)
        merged = {**r, **{k: v for k, v in data.items() if k in FIELDS or k in ("key", "summary", "insights")}}
        new = self._clean({**merged, "id": rid, "added": r["added"], "source": r["source"]})
        if not new["title"]:
            raise VaultError("題名を入力してください")
        for p in (new["file"], new["note"]):
            if p:
                self.app._path(p)
        if not new["key"]:
            new["key"] = self._citekey(new, rid)
        elif any(x["key"] == new["key"] and x["id"] != rid for x in self.refs):
            raise VaultError(f"引用キーが重複しています: {new['key']}", 409)
        new["updated"] = time.time()
        self.refs[self.refs.index(r)] = new
        self._save()
        return self._row(new)

    def remove(self, ids: list[str]) -> int:
        n = len(self.refs)
        self.refs = [r for r in self.refs if r["id"] not in set(ids)]
        self.links = [l for l in self.links if l["a"] not in set(ids) and l["b"] not in set(ids)]
        if len(self.refs) != n:
            self._save()
        return n - len(self.refs)

    # ---- 文献同士のつながり
    def _same_link(self, l: dict, a: str, b: str, ltype: str) -> bool:
        if l["type"] != ltype:
            return False
        return (l["a"], l["b"]) == (a, b) or (not LINK_TYPES[ltype]["directed"] and (l["a"], l["b"]) == (b, a))

    def link(self, a: str, b: str, ltype: str = "related", note: str = "", origin: str = "user") -> dict:
        self.get(a), self.get(b)
        if a == b:
            raise VaultError("同じ文献同士はつなげません")
        if ltype not in LINK_TYPES:
            raise VaultError("つながりの種類が不正です")
        for l in self.links:
            if self._same_link(l, a, b, ltype):
                if note:
                    l["note"] = note[:200]
                if origin == "user":
                    l["origin"] = "user"
                self._save()
                return l
        l = {"a": a, "b": b, "type": ltype, "note": (note or "")[:200], "origin": origin if origin in ("user", "ai", "auto") else "user",
             "created": time.time()}
        self.links.append(l)
        self._save()
        return l

    def unlink(self, a: str, b: str, ltype: str = "") -> int:
        n = len(self.links)
        self.links = [l for l in self.links if not ({l["a"], l["b"]} == {a, b} and (not ltype or l["type"] == ltype))]
        if len(self.links) != n:
            self._save()
        return n - len(self.links)

    def links_of(self, rid: str) -> list[dict]:
        """この文献から見たつながり（向きを揃えて、相手の情報と説明文を付ける）。"""
        out = []
        for l in self.links:
            if rid not in (l["a"], l["b"]):
                continue
            outgoing = l["a"] == rid
            other_id = l["b"] if outgoing else l["a"]
            try:
                other = self.get(other_id)
            except VaultError:
                continue
            t = LINK_TYPES[l["type"]]
            out.append({"id": other_id, "title": other["title"], "year": other["year"], "authors": other["authors"][:2],
                        "type": l["type"], "label": t["label"] if outgoing or not t["directed"] else t["inverse"],
                        "outgoing": outgoing, "directed": t["directed"], "note": l.get("note", ""), "origin": l.get("origin", "user")})
        out.sort(key=lambda x: (x["type"], x["title"]))
        return out

    def detect_citations(self, ids: list[str] | None = None) -> dict:
        """本文（参考文献欄など）に他の登録文献の DOI・題名が出ていれば「引用している」とみなす（自動）。"""
        targets = [r for r in self.refs if ids is None or r["id"] in set(ids)]
        others = [(o, o["doi"].lower(), _title_key(o["title"])) for o in self.refs if o["title"]]
        added, found = 0, []
        for r in targets:
            text = self._text_of(r, 3_000_000)
            if not text:
                continue
            low = text.lower()
            tail = low[int(len(low) * 0.6):]                        # 参考文献は後ろに多いので、後半は題名の一致も許す
            for o, doi, tk in others:
                if o["id"] == r["id"]:
                    continue
                hit = bool(doi and len(doi) > 8 and doi in low)
                if not hit and len(tk) >= 12:
                    hit = tk in _title_key(tail) if len(tail) < 400_000 else tk in _title_key(tail[:400_000])
                if hit and not any(self._same_link(l, r["id"], o["id"], "cites") for l in self.links):
                    self.links.append({"a": r["id"], "b": o["id"], "type": "cites", "note": "本文中に DOI・題名が出ている",
                                       "origin": "auto", "created": time.time()})
                    added += 1
                    found.append({"from": r["title"], "to": o["title"]})
        if added:
            self._save()
        return {"added": added, "pairs": found}

    def suggest_links(self, rid: str, k: int = 5) -> list[dict]:
        """つながりの候補。内容の近い文献を集め、LLM があれば種類と理由を判断させる。"""
        r = self.get(rid)
        linked = {l["b"] if l["a"] == rid else l["a"] for l in self.links if rid in (l["a"], l["b"])}
        cands = [c for c in self.related(rid, k=k + len(linked) + 2) if c["id"] not in linked][:k + 2]
        if not cands:
            return []
        cfg = self.app.config()
        if not chat_configured(cfg):
            return [{"id": c["id"], "title": c["title"], "year": c["year"], "type": "same_topic" if any("タグ" in x for x in c["reasons"]) else "related",
                     "reason": " ・ ".join(c["reasons"]), "ai": False} for c in cands[:k]]
        me = (f"[0] {r['title']}（{r['year']}）\n"
              + ((r["summary"] or {}).get("one_line") or r["abstract"][:400] or self._text_of(r, 400)))
        lst = "\n".join(f"[{i}] {c['title']}（{c['year']}）\n"
                        + ((c["summary"] or {}).get("one_line") or c["abstract"][:300] or c["passage"])
                        for i, c in enumerate(cands, 1))
        raw = LLMClient(cfg).chat(
            "[TASK:liblinks]\n文献 [0] と候補の文献の関係を判断してください。本当に関係のあるものだけを選び、"
            "種類を次から選んでください: cites（[0] が候補を引用している）/ extends（[0] が候補を発展させている）/ "
            "supports（[0] が候補を支持している）/ refutes（[0] が候補に反論している）/ compares（比較対象）/ same_topic（同じテーマ）/ related（関連）。"
            '\n出力は JSON 配列だけ: [{"n": 候補番号, "type": "種類", "reason": "関係の説明（30 字程度）"}]\n'
            f"# 対象の文献\n{me}\n# 候補\n{lst}", temperature=0.0)
        data = _parse_json_list(raw)
        out = []
        for it in data:
            try:
                c = cands[int(it.get("n")) - 1]
            except (TypeError, ValueError, IndexError, AttributeError):
                continue
            t = str(it.get("type") or "related")
            out.append({"id": c["id"], "title": c["title"], "year": c["year"], "type": t if t in LINK_TYPES else "related",
                        "reason": str(it.get("reason") or "").strip()[:80], "ai": True})
        return out[:k]

    def rename_path(self, old: str, new: str) -> None:
        changed = False
        for r in self.refs:
            for f in ("file", "note"):
                if r[f] == old or (r[f].startswith(old + "/")):
                    r[f] = new + r[f][len(old):]
                    changed = True
        if changed:
            self._save()

    def import_text(self, text: str, tags: list[str] | None = None) -> dict:
        text = (text or "").strip()
        refs = parse_bibtex(text) if "@" in text and re.search(r"@\w+\s*\{", text) else parse_ris(text)
        if not refs:
            raise VaultError("BibTeX か RIS の形式として読めませんでした")
        added, dups = [], []
        for r in refs:
            r["tags"] = tags or []
            got = self.add(r, source="import", skip_dup=True)
            (dups if got.get("_dup") else added).append(got)
        return {"added": [self._row(a) for a in added], "duplicates": [d["title"] for d in dups], "total": len(refs)}

    def export(self, ids: list[str] | None, fmt: str) -> str:
        refs = [r for r in self.refs if ids is None or r["id"] in set(ids)]
        if fmt == "ris":
            return "\n\n".join(to_ris(r) for r in refs) + "\n"
        if fmt == "csv":
            buf = io.StringIO()
            w = csv.writer(buf)
            w.writerow(["key", "type", "title", "authors", "year", "venue", "volume", "issue", "pages", "doi", "url",
                        "tags", "keywords", "status", "rating", "file", "note", "one_line"])
            for r in refs:
                w.writerow([r["key"], r["type"], r["title"], "; ".join(r["authors"]), r["year"], r["venue"], r["volume"],
                            r["issue"], r["pages"], r["doi"], r["url"], "; ".join(r["tags"]), "; ".join(r["keywords"]),
                            r["status"], r["rating"], r["file"], r["note"], (r["summary"] or {}).get("one_line", "")])
            return buf.getvalue()
        if fmt == "md":
            return "\n".join(f"- {citation(r)}" for r in refs) + "\n"
        return "\n\n".join(to_bibtex(r) for r in refs) + "\n"

    # ---- ファイルから登録
    def register_files(self, paths: list[str], use_ai: bool = False, tags: list[str] | None = None) -> dict:
        added, dups, errors = [], [], []
        for p in paths:
            try:
                path = self.app._path(p)
                it = self.app.index.get(path)
                if not it or it["status"] != "ok":
                    raise VaultError("読み込まれていないファイルです（「更新」で読み込んでください）")
                if self.by_path(path):
                    dups.append(it["title"])
                    continue
                meta = self.guess_metadata(it["title"], it["text"] or "")
                meta.update({"file": path, "tags": tags or []})
                r = self.add(meta, source="file", skip_dup=True)
                if r.get("_dup"):
                    dups.append(it["title"])
                    continue
                if use_ai and chat_configured(self.app.config()):
                    try:
                        r = self.ai_metadata(r["id"], overwrite=True)     # 推定しただけの書誌情報は AI の結果で置き換える
                    except LLMError as e:
                        errors.append({"path": path, "error": f"AI の補完に失敗: {e}"})
                added.append(self._row(r))
            except VaultError as e:
                errors.append({"path": p, "error": str(e)})
        return {"added": added, "duplicates": dups, "errors": errors}

    @staticmethod
    def guess_metadata(filename: str, text: str) -> dict:
        """LLM を使わない書誌情報の推定（題名・DOI・年・arXiv）。"""
        head = text[:4000]
        title = ""
        for ln in head.split("\n"):
            s = re.sub(r"^#+\s*", "", ln).strip()
            if 6 <= len(s) <= 200 and not re.match(r"^(p\.\d+|スライド|slide|page)\b", s, re.I) and not _DOI_RE.search(s):
                title = s
                break
        stem = re.sub(r"\.[^.]+$", "", filename)
        if not title or re.fullmatch(r"[\d\W_]+", title):
            title = stem
        doi = (_DOI_RE.search(head) or [None, ""])[1].rstrip(".,;")
        year = ""
        ym = re.search(r"(?:©|\(c\)|copyright|received|accepted|published|発行|年)\D{0,20}((?:19|20)\d{2})", head, re.I)
        if ym:
            year = ym.group(1)
        else:
            ys = _YEAR_RE.findall(head)
            if ys:
                year = Counter(ys).most_common(1)[0][0]
        fy = _YEAR_RE.search(stem)
        if fy:
            year = fy.group(1)
        arxiv = re.search(r"arXiv:\s*(\d{4}\.\d{4,5})", head, re.I)
        out = {"title": title[:300], "doi": doi, "year": year, "type": "misc"}
        if arxiv:
            out["url"] = f"https://arxiv.org/abs/{arxiv.group(1)}"
            out["type"] = "article"
        if re.search(r"abstract|要旨|概要", head, re.I):
            out["type"] = "article"
        return out

    # ---- AI
    def _text_of(self, r: dict, limit: int) -> str:
        parts = []
        for p in (r["file"], r["note"]):
            it = self.app.index.get(p) if p else None
            if it and it["text"]:
                parts.append(it["text"])
        text = "\n\n".join(parts).strip()
        if not text and r["abstract"]:
            text = r["abstract"]
        return text[:limit]

    def ai_metadata(self, rid: str, overwrite: bool = False) -> dict:
        r = self.get(rid)
        cfg = self.app.config()
        if not chat_configured(cfg):
            raise LLMError("LLM が未設定です")
        text = self._text_of(r, int(cfg.get("ingest_chunk_chars") or 3000) * 2)
        if not text:
            raise LLMError("本文がありません（本文ファイルを結びつけてください）")
        raw = LLMClient(cfg).chat(
            "[TASK:libmeta]\n次の文書の書誌情報を JSON だけで出力してください。\n"
            '形式: {"title": "正式な題名", "authors": ["著者（姓 名 または Family, Given）", "…"], "year": "発行年 (YYYY)",'
            ' "venue": "掲載誌・会議名・出版社", "volume": "", "issue": "", "pages": "", "doi": "", "url": "",'
            ' "type": "article/inproceedings/book/incollection/thesis/report/web/patent/misc", "lang": "ja/en",'
            ' "abstract": "要旨（原文にあればそのまま、無ければ 3 文で）", "keywords": ["キーワード", "…"]}\n'
            "不明な項目は空にし、文書に書かれていないことは書かないでください。\n\n# 文書の冒頭\n" + text, temperature=0.0)
        data = _parse_json(raw)
        if not isinstance(data, dict):
            raise LLMError("AI の応答を書誌情報として読めませんでした")
        upd = {}
        for k in ("title", "authors", "year", "venue", "volume", "issue", "pages", "doi", "url", "type", "lang", "abstract", "keywords"):
            v = data.get(k)
            if v in (None, "", []):
                continue
            cur = r.get(k)
            if overwrite or not cur or (k == "title" and r["source"] == "file" and _title_key(cur) == _title_key(re.sub(r"\.[^.]+$", "", r["file"].rsplit("/", 1)[-1]))):
                upd[k] = v
        if upd:
            if "title" in upd or "authors" in upd or "year" in upd:
                upd["key"] = ""                 # 引用キーを作り直す
            return self.update(rid, upd) and self.get(rid)
        return r

    def ai_summary(self, rid: str) -> dict:
        r = self.get(rid)
        cfg = self.app.config()
        if not chat_configured(cfg):
            raise LLMError("LLM が未設定です")
        size = int(cfg.get("ingest_chunk_chars") or 3000)
        text = self._text_of(r, size * 6)
        if not text:
            raise LLMError("本文がありません")
        from .ingest import _split
        parts = _split(text, size)
        if len(parts) > 1:
            notes = []
            client = LLMClient(cfg)
            for i, part in enumerate(parts[:6], 1):
                notes.append(f"## 部分 {i}\n" + client.chat(
                    f"[TASK:ingest_map]\n次は文献「{r['title']}」の一部（{i}/{min(len(parts), 6)}）です。"
                    "この部分の要点（目的・手法・結果・数値・限界に関わること）を箇条書き 3〜8 行で書いてください。\n\n# 文書の一部\n" + part,
                    temperature=0.1).strip())
            material = "\n\n".join(notes)
        else:
            material = text
        raw = LLMClient(cfg).chat(
            "[TASK:libsummary]\n次の文献を構造化して要約し、JSON だけで出力してください。\n"
            '形式: {"one_line": "1 文での要約", "purpose": "目的・解こうとした課題", "method": "手法・対象・データ",'
            ' "results": "主な結果・主張（数値があれば含める）", "limitations": "限界・残された課題",'
            ' "keywords": ["キーワード", "…"]}\n文献に書かれていないことは書かないでください。\n\n'
            f"# 文献「{r['title']}」\n{material}", temperature=0.1)
        data = _parse_json(raw)
        if not isinstance(data, dict):
            raise LLMError("AI の応答を要約として読めませんでした")
        summary = {k: str(data.get(k) or "").strip()[:3000] for k in SUMMARY_KEYS}
        upd = {"summary": summary}
        kws = _list(data.get("keywords"))
        if kws and not r["keywords"]:
            upd["keywords"] = kws[:10]
        self.update(rid, upd)
        return self._row(self.get(rid))

    # ---- 検索（文献だけ）
    def search(self, query: str, k: int = 20, tag: str = "", year: str = "", status: str = "", author: str = "") -> dict:
        query = _norm(query)
        if not query:
            return {"results": [], "mode": "none"}
        filt = {r["id"] for r in self.list(tag=tag, year=year, status=status, author=author)["refs"]}
        path_ref = {}
        for r in self.refs:
            if r["id"] in filt:
                for p in (r["file"], r["note"]):
                    if p:
                        path_ref[p] = r
        paths = {p for p in path_ref if self.app.index.get(p)}
        hits = self.app.ai.retrieve(query, k=80, paths=paths, pool=120) if paths else []
        agg: dict[str, dict] = {}
        for h in hits:
            r = path_ref.get(h["path"])
            if not r:
                continue
            a = agg.setdefault(r["id"], {"ref": r, "score": 0.0, "passages": [], "modes": set()})
            a["score"] += h["score"]
            a["modes"].add(h["mode"])
            if len(a["passages"]) < 3:
                body = re.sub(r"^#{1,6}\s+[^\n]*\n?", "", h["text"]).strip() or h["text"]     # 見出し行は別に出すので外す
                a["passages"].append({"heading": h["heading"], "text": body[:300], "path": h["path"], "mode": h["mode"]})
        # 書誌情報だけに一致する文献（本文が無いものなど）
        ql = query.lower()
        for r in self.refs:
            if r["id"] in filt and r["id"] not in agg and ql in " ".join([r["title"], " ".join(r["authors"]), r["abstract"], " ".join(r["keywords"]), r["venue"]]).lower():
                agg[r["id"]] = {"ref": r, "score": 0.004, "passages": [{"heading": "書誌情報", "text": r["abstract"][:300] or r["title"], "path": "", "mode": "keyword"}], "modes": {"keyword"}}
        for a in agg.values():                                     # 題名・要旨に出る語は上げる
            r = a["ref"]
            if ql in r["title"].lower():
                a["score"] *= 1.5
            elif ql in r["abstract"].lower() or any(ql in kw.lower() for kw in r["keywords"]):
                a["score"] *= 1.2
        ranked = sorted(agg.values(), key=lambda a: -a["score"])[:k]
        any_vec = any("vector" in a["modes"] or "both" in a["modes"] for a in ranked)
        return {"results": [{**self._row(a["ref"]), "score": round(a["score"], 5), "passages": a["passages"],
                             "mode": "both" if a["modes"] & {"both"} or a["modes"] >= {"keyword", "vector"} else next(iter(a["modes"]))} for a in ranked],
                "mode": "hybrid" if any_vec else "keyword", "embed": embed_configured(self.app.config())}

    def related(self, rid: str, k: int = 6) -> list[dict]:
        r = self.get(rid)
        own = {p for p in (r["file"], r["note"]) if p}
        paths = self.paths() - own
        out: dict[str, dict] = {}
        if paths:
            q = (r["title"] + "\n" + (r["abstract"] or self._text_of(r, 1500)))[:1500]
            for h in self.app.ai.retrieve(q, k=30, paths=paths, pool=80):
                o = self.by_path(h["path"])
                if o and o["id"] != rid:
                    e = out.setdefault(o["id"], {"ref": o, "score": 0.0, "reasons": set(), "passage": h["text"][:160]})
                    e["score"] += h["score"]
                    e["reasons"].add("内容が近い")
        for o in self.refs:
            if o["id"] == rid:
                continue
            shared_a = set(o["authors"]) & set(r["authors"])
            shared_t = (set(o["tags"]) | set(o["keywords"])) & (set(r["tags"]) | set(r["keywords"]))
            if shared_a or shared_t:
                e = out.setdefault(o["id"], {"ref": o, "score": 0.0, "reasons": set(), "passage": ""})
                e["score"] += 0.01 * len(shared_a) + 0.004 * len(shared_t)
                if shared_a:
                    e["reasons"].add("同じ著者: " + "、".join(sorted(shared_a)[:2]))
                if shared_t:
                    e["reasons"].add("同じタグ・キーワード: " + "、".join(sorted(shared_t)[:3]))
        for l in self.links_of(rid):
            o = self.get(l["id"])
            e = out.setdefault(o["id"], {"ref": o, "score": 0.0, "reasons": set(), "passage": ""})
            e["score"] += 0.02
            e["reasons"].add("つながり: " + l["label"])
        rows = sorted(out.values(), key=lambda e: -e["score"])[:k]
        return [{**self._row(e["ref"]), "score": round(e["score"], 5), "reasons": sorted(e["reasons"]), "passage": e["passage"]} for e in rows]

    def related_in_vault(self, rid: str, k: int = 6) -> list[dict]:
        """登録していないノート・資料も含めて、内容の近いものを Vault 全体から探す（報告書の関連資料・気づきの材料）。"""
        r = self.get(rid)
        own = {p for p in (r["file"], r["note"]) if p}
        q = (r["title"] + "\n" + (r["abstract"] or self._text_of(r, 1500)))[:1500]
        registered = self.paths()
        out: dict[str, dict] = {}
        for h in self.app.ai.retrieve(q, k=40, exclude=own, pool=80):
            if h["path"] in registered or h["path"] in own:
                continue
            e = out.setdefault(h["path"], {"path": h["path"], "title": h["title"], "kind": h.get("kind", "note"),
                                           "score": 0.0, "passage": h["text"][:160].replace("\n", " ")})
            e["score"] += h["score"]
        return sorted(out.values(), key=lambda e: -e["score"])[:k]

    def insights(self, rid: str) -> dict:
        """AI の気づき: 要点、関連する文書との共通点・相違点、残る疑問、次の一手。結果は文献に保存する。"""
        r = self.get(rid)
        cfg = self.app.config()
        if not chat_configured(cfg):
            raise LLMError("LLM が未設定です")
        size = int(cfg.get("ingest_chunk_chars") or 3000)
        text = self._text_of(r, size * 3)
        if not text:
            raise LLMError("本文がありません（本文ファイルを結びつけて「更新」してください）")
        rel = self.related(rid, k=4)
        rel_vault = self.related_in_vault(rid, k=4)
        others = []
        for o in rel:
            others.append({"title": o["title"], "path": o["file"] or o["note"] or "", "text": self._text_of(o, 1200) or o["abstract"],
                           "why": "、".join(o["reasons"])})
        for o in rel_vault:
            it = self.app.index.get(o["path"])
            others.append({"title": o["title"], "path": o["path"], "text": (it["text"][:1200] if it else o["passage"]), "why": "内容が近い（未登録）"})
        others_txt = "\n\n".join(f"## [{i}] {o['title']}（{o['why']}）\n{o['text']}" for i, o in enumerate(others, 1)) or "（関連する文書はまだありません）"
        raw = LLMClient(cfg).chat(
            "[TASK:insights]\n次の「対象の文書」を読み、関連する文書と見比べて、読む人の役に立つ気づきを JSON だけで出力してください。\n"
            '形式: {"takeaways": ["対象の文書の要点（数値・結論・決定事項）", "…"],'
            ' "connections": [{"n": 関連文書の番号, "point": "対象の文書との共通点・相違点・補い合う点を 1〜2 文"}],'
            ' "questions": ["読んで残る疑問・確認すべき点"], "actions": ["次にやるとよいこと（確認・比較・連絡など）"]}\n'
            "文書に書かれていないことは書かず、推測は「推定」と書いてください。\n\n"
            f"# 対象の文書「{r['title']}」\n{text}\n\n# 関連する文書\n{others_txt}", temperature=0.2)
        data = _parse_json(raw)
        if not isinstance(data, dict):
            raise LLMError("AI の応答を気づきとして読めませんでした")
        conns = []
        for c in data.get("connections") or []:
            if not isinstance(c, dict):
                continue
            try:
                o = others[int(c.get("n")) - 1]
            except (TypeError, ValueError, IndexError):
                continue
            conns.append({"title": o["title"], "path": o["path"], "point": str(c.get("point") or "").strip()})
        ins = {"takeaways": data.get("takeaways") or [], "connections": conns, "questions": data.get("questions") or [],
               "actions": data.get("actions") or [], "at": time.time()}
        self.update(rid, {"insights": ins})
        return self.get(rid)["insights"]

    def ask_doc(self, rid: str, question: str, history=None) -> dict:
        """この文書だけを根拠に質問する。"""
        r = self.get(rid)
        paths = {p for p in (r["file"], r["note"]) if p and self.app.index.get(p)}
        if not paths:
            return {"answer": "", "sources": [], "llm": False, "message": "本文ファイルがありません（結びつけて「更新」してください）"}
        system = ("あなたは報告書・文献を読み解く助手です。与えられた 1 つの文書の抜粋だけを根拠に、日本語で簡潔かつ正確に答えてください。"
                  "根拠にした箇所は [[名前]] の形で示し、書かれていないことは「この文書には記載がありません」と答えてください。")
        res = self.app.ai.ask(question, history=history, paths=paths, k=8, system_extra=system)
        for s in res["sources"]:
            s["ref_id"], s["citation"], s["title"] = r["id"], citation(r), r["title"]
        return res

    def ask(self, question: str, history=None, tag: str = "", year: str = "", status: str = "") -> dict:
        ids = {r["id"] for r in self.list(tag=tag, year=year, status=status)["refs"]}
        paths = {p for r in self.refs if r["id"] in ids for p in (r["file"], r["note"]) if p and self.app.index.get(p)}
        if not paths:
            return {"answer": "", "sources": [], "llm": False, "message": "検索できる文献がありません（本文ファイルを結びつけて「更新」してください）"}
        system = ("あなたは研究文献を読み解く助手です。与えられた文献の抜粋だけを根拠に、日本語で簡潔かつ正確に答えてください。"
                  "根拠にした文献は [[名前]] の形で本文中に示し、数値や主張は出典の文献ごとに区別してください。"
                  "文献間で結果が食い違う場合はその旨を書き、書かれていないことは「文献には記載がありません」と答えてください。")
        res = self.app.ai.ask(question, history=history, paths=paths, k=8, system_extra=system)
        for s in res["sources"]:
            ref = self.by_path(s["path"])
            if ref:
                s["ref_id"], s["citation"], s["title"] = ref["id"], citation(ref), ref["title"]
        return res

    def embed_status(self) -> dict:
        cfg = self.app.config()
        paths = self.paths()
        chunks = sum(len(self.app.index.chunks_of(p)) for p in paths)
        pending = len(self.app.index.chunks_without_embedding(cfg["embed_model"], None, paths)) if embed_configured(cfg) else 0
        return {"embed": embed_configured(cfg), "files": len(paths), "chunks": chunks, "pending": pending,
                "model": cfg.get("embed_model", "")}

    # ---- 分解（章・段落）
    def structure(self, rid: str) -> list[dict]:
        r = self.get(rid)
        secs: list[dict] = []
        for p in (r["file"], r["note"]):
            if not p:
                continue
            cur = None
            for c in self.app.index.chunks_of(p):
                h = c["heading"]
                if not h:                                    # 見出しの無い先頭部分は、短ければ 1 行目（題名）を見出しにする
                    first = c["text"].strip().split("\n")[0].strip()
                    h = first if 0 < len(first) <= 80 else "本文"
                if cur is None or cur["heading"] != h:
                    cur = {"n": len(secs), "heading": h, "path": p, "chunks": []}
                    secs.append(cur)
                cur["chunks"].append({"id": c["id"], "ord": c["ord"], "text": c["text"], "len": c["len"]})
        return secs

    # ---- 分解グラフ
    def _groups(self, group_by: str) -> dict[str, list[dict]]:
        g: dict[str, list[dict]] = defaultdict(list)
        for r in self.refs:
            vals = {"tag": r["tags"] or r["keywords"][:3], "author": r["authors"][:3], "year": [r["year"]] if r["year"] else [],
                    "type": [TYPES.get(r["type"], r["type"])], "status": [STATUSES[r["status"]]], "none": []}.get(group_by, [])
            for v in (vals or ["（未分類）"]):
                g[v].append(r)
        return g

    def graph(self, group_by: str = "tag", expanded: list[str] | None = None, center: str | None = None) -> dict:
        """階層グラフ。expanded に入っているノードは子を展開する。
        ノード id: g:<値> / r:<文献id> / s:<文献id>:<章番号> / c:<チャンクid>"""
        expanded = set(expanded or [])
        group_by = group_by if group_by in GROUPS else "tag"
        nodes: dict[str, dict] = {}
        edges: list[list] = []
        visible_refs: dict[str, dict] = {}
        groups = self._groups(group_by)
        if group_by == "none":
            for r in self.refs:
                visible_refs[r["id"]] = r
        else:
            for name, refs in sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0])):
                gid = f"g:{name}"
                nodes[gid] = {"id": gid, "title": name, "kind": "group", "level": 0, "count": len(refs),
                              "expandable": True, "expanded": gid in expanded}
                if gid in expanded or center and any(center == f"r:{r['id']}" for r in refs):
                    for r in refs:
                        visible_refs[r["id"]] = r
                        edges.append([gid, f"r:{r['id']}", "tree"])
        sections: dict[tuple, dict] = {}            # (rid, n) → section
        passages: dict[int, tuple] = {}             # chunk id → (rid, n)
        chunk_owner: dict[int, tuple] = {}
        for rid, r in visible_refs.items():
            nid = f"r:{rid}"
            has_text = bool(self.app.index.get(r["file"]) if r["file"] else self.app.index.get(r["note"]) if r["note"] else False)
            nodes[nid] = {"id": nid, "title": r["title"], "kind": "ref", "level": 1, "year": r["year"], "status": r["status"],
                          "authors": r["authors"][:2], "path": r["file"] or r["note"], "expandable": has_text,
                          "expanded": nid in expanded, "rating": r["rating"], "grp": "ref"}
            if nid in expanded and has_text:
                for sec in self.structure(rid):
                    sid = f"s:{rid}:{sec['n']}"
                    sections[(rid, sec["n"])] = sec
                    nodes[sid] = {"id": sid, "title": sec["heading"], "kind": "section", "level": 2, "path": sec["path"],
                                  "count": len(sec["chunks"]), "expandable": len(sec["chunks"]) > 0, "expanded": sid in expanded,
                                  "ref": nid}
                    edges.append([nid, sid, "tree"])
                    for c in sec["chunks"]:
                        chunk_owner[c["id"]] = (rid, sec["n"])
                        if sid in expanded:
                            cid = f"c:{c['id']}"
                            passages[c["id"]] = (rid, sec["n"])
                            body = re.sub(r"^#{1,6}\s+[^\n]*\n?", "", c["text"]).strip() or c["text"]
                            nodes[cid] = {"id": cid, "title": body[:60].replace("\n", " "), "kind": "passage", "level": 3,
                                          "path": sec["path"], "text": c["text"][:400], "heading": sec["heading"], "expandable": False,
                                          "ref": nid, "section": sid}
                            edges.append([sid, cid, "tree"])
        # 内容の近さによる横のつながり
        lib_paths = self.paths()
        path_ref = {p: r for r in self.refs for p in (r["file"], r["note"]) if p}
        sim_edges: set[tuple[str, str]] = set()

        def target_for(chunk_id: int, path: str) -> str | None:
            """近いチャンクを、いま見えている最も細かいノードに結びつける。"""
            if chunk_id in passages:
                return f"c:{chunk_id}"
            own = chunk_owner.get(chunk_id)
            if own:
                return f"s:{own[0]}:{own[1]}"
            o = path_ref.get(path)
            return f"r:{o['id']}" if o and o["id"] in visible_refs else None

        def add_sim(a: str, b: str):
            if a != b and (b, a) not in sim_edges and (a, b) not in sim_edges:
                sim_edges.add((a, b))

        if len(visible_refs) > 1:
            for rid, sims in self._ref_similarity().items():
                if rid in visible_refs:
                    for other, score in sims:
                        if other in visible_refs:
                            add_sim(f"r:{rid}", f"r:{other}")
        chunk_path = {c["id"]: c["path"] for c in self.app.index.chunk_refs()} if sections else {}
        for (rid, n), sec in sections.items():
            sid = f"s:{rid}:{n}"
            q = (sec["heading"] + "\n" + " ".join(c["text"] for c in sec["chunks"]))[:700]
            own_paths = {p for p in (visible_refs[rid]["file"], visible_refs[rid]["note"]) if p}
            for cid, _ in self.app.index.search_chunks(q, 6, paths=lib_paths - own_paths):
                t = target_for(cid, chunk_path.get(cid, ""))
                if t and t != sid:
                    add_sim(sid, t)
                    break
        for chunk_id, (rid, n) in passages.items():
            c = next(x for x in sections[(rid, n)]["chunks"] if x["id"] == chunk_id)
            own_paths = {p for p in (visible_refs[rid]["file"], visible_refs[rid]["note"]) if p}
            hits = self.app.index.search_chunks(c["text"][:500], 4, paths=lib_paths - own_paths)
            for cid, _ in hits[:2]:
                t = target_for(cid, chunk_path.get(cid, ""))
                if t:
                    add_sim(f"c:{chunk_id}", t)
        linked_pairs = set()
        for l in self.links:
            if l["a"] in visible_refs and l["b"] in visible_refs:
                t = LINK_TYPES[l["type"]]
                edges.append([f"r:{l['a']}", f"r:{l['b']}", "link", t["label"], t["directed"]])
                linked_pairs.add(frozenset((f"r:{l['a']}", f"r:{l['b']}")))
        edges += [[a, b, "sim"] for a, b in sorted(sim_edges) if a in nodes and b in nodes and frozenset((a, b)) not in linked_pairs]
        return {"nodes": list(nodes.values()), "edges": [e for e in edges if e[0] in nodes and e[1] in nodes],
                "group_by": group_by, "groups": GROUPS, "link_types": {k: v["label"] for k, v in LINK_TYPES.items()}}

    def _ref_similarity(self) -> dict[str, list[tuple[str, float]]]:
        key = (self.app.index.rev, self.rev)
        if self._sim_cache.get("key") == key:
            return self._sim_cache["sims"]
        path_ref = {p: r for r in self.refs for p in (r["file"], r["note"]) if p}
        paths = self.paths()
        sims: dict[str, list[tuple[str, float]]] = {}
        chunk_path = {c["id"]: c["path"] for c in self.app.index.chunk_refs()}
        for r in self.refs:
            own = {p for p in (r["file"], r["note"]) if p}
            if not (own & paths):
                continue
            q = (r["title"] + "\n" + (r["abstract"] or self._text_of(r, 800)))[:1000]
            best: dict[str, float] = {}
            base = 0.0
            for cid, sc in self.app.index.search_chunks(q, 40, paths=paths):
                o = path_ref.get(chunk_path.get(cid, ""))
                if not o:
                    continue
                if o["id"] == r["id"]:
                    base = max(base, sc)
                elif sc > best.get(o["id"], 0):
                    best[o["id"]] = sc
            base = base or max(best.values(), default=1.0)
            ranked = sorted(((oid, sc / base) for oid, sc in best.items() if sc / base >= 0.15), key=lambda x: -x[1])[:3]
            sims[r["id"]] = ranked
        self._sim_cache = {"key": key, "sims": sims}
        return sims

    # ---- 読書ノート
    def create_note(self, rid: str) -> dict:
        r = self.get(rid)
        if r["note"] and self.app.vault.exists(r["note"]):
            return {"path": r["note"], "created": False}
        folder = (self.app.config().get("library_folder") or "文献").strip().strip("/")
        name = re.sub(r'[<>:"|?*\\/\x00-\x1f]', " ", r["title"])[:60].strip() or r["key"]
        rel = normalize_rel(f"{folder}/{name}")
        n = 2
        while self.app.vault.exists(rel):
            rel = normalize_rel(f"{folder}/{name} ({n})")
            n += 1
        s = r["summary"] or {}
        fm = ["---", "種別: 文献", f"引用キー: {r['key']}", f"著者: {'、'.join(r['authors'])}", f"年: {r['year']}",
              f"掲載: {r['venue']}"]
        if r["doi"]:
            fm.append(f"DOI: {r['doi']}")
        if r["file"]:
            fm.append(f"元の資料: [[{r['file']}]]")
        fm += [f"状態: {STATUSES[r['status']]}", "---"]
        body = [f"# {r['title']}", "", f"> {citation(r)}", ""]
        if s.get("one_line"):
            body += [f"**一言で**: {s['one_line']}", ""]
        for k in ("purpose", "method", "results", "limitations"):
            body += [f"## {SUMMARY_LABELS[k]}", s.get(k, ""), ""]
        body += ["## メモ", "", "## 引用したい箇所", "", "#文献" + "".join(f" #{t}" for t in r["tags"])]
        res = self.app.create(rel, text="\n".join(fm + body) + "\n")
        self.update(rid, {"note": res["path"]})
        return {"path": res["path"], "created": True}


def _parse_json_list(raw: str) -> list:
    raw = (raw or "").strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", raw, re.DOTALL)
    if m:
        raw = m.group(1)
    s, e = raw.find("["), raw.rfind("]")
    if s == -1 or e <= s:
        return []
    try:
        data = json.loads(raw[s:e + 1])
    except ValueError:
        return []
    return data if isinstance(data, list) else []


def _parse_json(raw: str):
    raw = (raw or "").strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", raw, re.DOTALL)
    if m:
        raw = m.group(1)
    s, e = raw.find("{"), raw.rfind("}")
    if s == -1 or e <= s:
        return None
    try:
        return json.loads(raw[s:e + 1])
    except ValueError:
        return None
