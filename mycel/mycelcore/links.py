"""Markdown からリンク・タグ・見出し・プロパティを取り出す。

- ウィキリンク: [[ノート名]] / [[ノート名|別名]] / [[ノート名#見出し]] / [[フォルダ/ノート名]]
- タグ: 本文中の #タグ と、プロパティ（フロントマター）の ``tags: a, b``
- コードブロック（```）とインラインコード（`...`）の中は解析しない
"""
from __future__ import annotations

import re

WIKILINK_RE = re.compile(r"\[\[([^\[\]\n]+?)\]\]")
TAG_RE = re.compile(r"(?<![\w#&/\\])#([^\s#\[\](){}<>,.、。!?！？:;\"'`|]+)")
HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
FENCE_RE = re.compile(r"^\s*(```|~~~)")
INLINE_CODE_RE = re.compile(r"`[^`\n]*`")


def parse_wikilink(inner: str) -> tuple[str, str, str]:
    """``[[...]]`` の中身を (対象, 見出し, 別名) に分ける。"""
    alias = ""
    if "|" in inner:
        inner, alias = inner.split("|", 1)
    heading = ""
    if "#" in inner:
        inner, heading = inner.split("#", 1)
    return inner.strip(), heading.strip(), alias.strip()


def split_frontmatter(text: str) -> tuple[dict, str, int]:
    """先頭の ``---`` ～ ``---`` を ``key: value`` の辞書として読む。

    戻り値は (プロパティ, 本文, 本文開始行番号)。
    """
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return {}, text, 0
    for end in range(1, min(len(lines), 200)):
        if lines[end].strip() == "---":
            props: dict = {}
            for line in lines[1:end]:
                if ":" in line:
                    key, val = line.split(":", 1)
                    key = key.strip()
                    if key:
                        props[key] = val.strip()
            return props, "\n".join(lines[end + 1:]), end + 1
    return {}, text, 0


def code_free_lines(text: str) -> list[tuple[int, str]]:
    """コードブロックとインラインコードを除いた (行番号, 行) の一覧。"""
    out: list[tuple[int, str]] = []
    in_fence = False
    for i, line in enumerate(text.split("\n")):
        if FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        out.append((i, INLINE_CODE_RE.sub("", line)))
    return out


def extract_links(text: str) -> list[str]:
    """リンク先ノート名の一覧（出現順・重複あり）。"""
    out: list[str] = []
    for _, line in code_free_lines(text):
        for m in WIKILINK_RE.finditer(line):
            target, _, _ = parse_wikilink(m.group(1))
            if target:
                out.append(target)
    return out


def extract_tags(text: str) -> list[str]:
    """タグの一覧（重複なし・出現順）。見出し行の ``# `` はタグにしない。"""
    props, _, _ = split_frontmatter(text)
    seen: dict[str, None] = {}
    raw = props.get("tags") or props.get("タグ") or ""
    for t in re.split(r"[,\s、]+", raw.strip("[]")):
        t = t.strip().lstrip("#")
        if t:
            seen.setdefault(t, None)
    for _, line in code_free_lines(text):
        line = WIKILINK_RE.sub(" ", line)
        line = re.sub(r"https?://\S+", " ", line)
        for m in TAG_RE.finditer(line):
            tag = m.group(1)
            if not tag.isdigit():
                seen.setdefault(tag, None)
    return list(seen)


def extract_headings(text: str) -> list[dict]:
    out = []
    for i, line in code_free_lines(text):
        m = HEADING_RE.match(line)
        if m:
            out.append({"level": len(m.group(1)), "text": m.group(2), "line": i})
    return out


def rewrite_links(text: str, old: str, new: str) -> tuple[str, int]:
    """``[[old]]`` 系のリンクを ``new`` に書き換える（見出し・別名は保持）。

    比較は大文字小文字を区別しない。コードブロック内は書き換えない。
    """
    old_key = old.strip().lower()
    count = 0
    lines = text.split("\n")
    in_fence = False

    def _sub(m: re.Match) -> str:
        nonlocal count
        target, heading, alias = parse_wikilink(m.group(1))
        if target.lower() != old_key:
            return m.group(0)
        count += 1
        out = new
        if heading:
            out += "#" + heading
        if alias:
            out += "|" + alias
        return f"[[{out}]]"

    for i, line in enumerate(lines):
        if FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if not in_fence and "[[" in line:
            lines[i] = WIKILINK_RE.sub(_sub, line)
    return "\n".join(lines), count


def line_containing(text: str, needle: str, limit: int = 160) -> str:
    """``needle`` を含む最初の行（前後を詰めた抜粋）。"""
    low = needle.lower()
    for line in text.split("\n"):
        idx = line.lower().find(low)
        if idx != -1:
            line = line.strip()
            idx = line.lower().find(low)
            if len(line) <= limit:
                return line
            start = max(0, idx - limit // 3)
            return ("…" if start else "") + line[start:start + limit] + "…"
    return ""


def chunk_note(text: str, max_chars: int = 900) -> list[dict]:
    """AI 検索用に見出し単位で分割する（長い節はさらに段落で分ける）。"""
    _, body, _ = split_frontmatter(text)
    chunks: list[dict] = []
    heading = ""
    buf: list[str] = []

    def flush():
        joined = "\n".join(buf).strip()
        if not joined:
            return
        if len(joined) <= max_chars:
            chunks.append({"heading": heading, "text": joined})
            return
        part = ""
        for para in re.split(r"\n\s*\n", joined):
            if part and len(part) + len(para) > max_chars:
                chunks.append({"heading": heading, "text": part.strip()})
                part = ""
            part += para + "\n\n"
            while len(part) > max_chars * 1.5:
                chunks.append({"heading": heading, "text": part[:max_chars].strip()})
                part = part[max_chars:]
        if part.strip():
            chunks.append({"heading": heading, "text": part.strip()})

    in_fence = False
    for line in body.split("\n"):
        if FENCE_RE.match(line):
            in_fence = not in_fence
        m = None if in_fence else HEADING_RE.match(line)
        if m:
            flush()
            buf = [line]
            heading = m.group(2)
        else:
            buf.append(line)
    flush()
    return chunks
