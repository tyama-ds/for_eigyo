"""HTMLサニタイズ。

- 抽出時: script/style/iframe 等を除去してテキスト化(JSは実行しない)
- 表示時: 証拠の抜粋等はフロントエンドでもテキストノードとして挿入するが、
  サーバ側でも二重にサニタイズする(多層防御)
"""

from __future__ import annotations

import re
from html.parser import HTMLParser

# 中身ごと除去するタグ(終了タグを持つコンテナ)
_DANGEROUS_TAGS = {
    "script",
    "style",
    "iframe",
    "frame",
    "frameset",
    "object",
    "embed",
    "applet",
    "form",
    "noscript",
}

# タグ自体を除去する void 要素(終了タグが無いため skip 深度を増やさない)
_DANGEROUS_VOID_TAGS = {"meta", "link", "base"}

_ALLOWED_DISPLAY_TAGS = {"b", "strong", "i", "em", "u", "br", "p", "ul", "ol", "li", "table", "tr", "td", "th"}


class _TextExtractor(HTMLParser):
    """危険タグの中身を捨ててテキストを抽出する。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _DANGEROUS_VOID_TAGS:
            return
        if tag in _DANGEROUS_TAGS:
            self._skip_depth += 1
        elif tag in ("p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "table"):
            self._chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _DANGEROUS_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0:
            self._chunks.append(data)

    def text(self) -> str:
        raw = "".join(self._chunks)
        raw = re.sub(r"[ \t\r\f\v]+", " ", raw)
        return re.sub(r"\n\s*\n+", "\n", raw).strip()


def strip_html_to_text(html: str) -> str:
    """HTMLからスクリプト等を除去したプレーンテキストを得る(JS非実行)。"""
    parser = _TextExtractor()
    parser.feed(html)
    parser.close()
    return parser.text()


class _DisplaySanitizer(HTMLParser):
    """表示用: 許可タグ(属性なし)以外を除去する。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self._out: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _DANGEROUS_VOID_TAGS:
            return
        if tag in _DANGEROUS_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth == 0 and tag in _ALLOWED_DISPLAY_TAGS:
            self._out.append(f"<{tag}>")  # 属性は一切許可しない(onclick等の除去)

    def handle_endtag(self, tag: str) -> None:
        if tag in _DANGEROUS_TAGS:
            if self._skip_depth > 0:
                self._skip_depth -= 1
            return
        if self._skip_depth == 0 and tag in _ALLOWED_DISPLAY_TAGS:
            self._out.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0:
            self._out.append(
                data.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            )

    def handle_entityref(self, name: str) -> None:
        if self._skip_depth == 0:
            self._out.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        if self._skip_depth == 0:
            self._out.append(f"&#{name};")

    def html(self) -> str:
        return "".join(self._out)


def sanitize_html(html: str) -> str:
    """表示用HTMLサニタイズ: 許可タグ以外・全属性・script等を除去する。"""
    parser = _DisplaySanitizer()
    parser.feed(html)
    parser.close()
    return parser.html()
