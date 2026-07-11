"""HTMLサニタイズ。

- 抽出時: script/style/iframe 等を除去してテキスト化(JSは実行しない)
- 表示時: 証拠の抜粋等はフロントエンドでもテキストノードとして挿入するが、
  サーバ側でも二重にサニタイズする(多層防御)

不正・未閉鎖のHTML(閉じない <script>/<style> 等)でも本文を落とさないよう、
寛容にツリーを構築する BeautifulSoup をパーサとして用いる。
"""

from __future__ import annotations

import html as _html
import re

from bs4 import BeautifulSoup, Comment, NavigableString, Tag

# 中身ごと除去するタグ
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
    "meta",
    "link",
    "base",
}

# 表示時に許可するタグ(属性はすべて除去)
_ALLOWED_DISPLAY_TAGS = {
    "b",
    "strong",
    "i",
    "em",
    "u",
    "br",
    "p",
    "ul",
    "ol",
    "li",
    "table",
    "tr",
    "td",
    "th",
}

# テキスト抽出時に改行を挿入するブロック要素
_BLOCK_TAGS = {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "table"}


def _clean_soup(html: str) -> BeautifulSoup:
    soup = BeautifulSoup(html, "html.parser")
    # 危険タグは中身ごと除去(未閉鎖でも bs4 が木を閉じるため取り残しが本文を汚さない)
    for tag in soup.find_all(_DANGEROUS_TAGS):
        tag.decompose()
    # コメント(条件付きコメント等)も除去
    for comment in soup.find_all(string=lambda s: isinstance(s, Comment)):
        comment.extract()
    return soup


def strip_html_to_text(html: str) -> str:
    """HTMLからスクリプト等を除去したプレーンテキストを得る(JS非実行)。"""
    soup = _clean_soup(html)
    parts: list[str] = []
    for element in soup.descendants:
        if isinstance(element, Comment):
            continue
        if isinstance(element, NavigableString):
            parts.append(str(element))
        elif isinstance(element, Tag) and element.name in _BLOCK_TAGS:
            parts.append("\n")
    raw = "".join(parts)
    raw = re.sub(r"[ \t\r\f\v]+", " ", raw)
    return re.sub(r"\n\s*\n+", "\n", raw).strip()


def sanitize_html(html: str) -> str:
    """表示用HTMLサニタイズ: 許可タグ以外・全属性・script等を除去する。

    許可タグは属性を全て落として温存し、非許可タグは中身のテキストのみ残す。
    テキストは HTML エスケープする。
    """
    soup = _clean_soup(html)

    def render(node: object) -> str:
        if isinstance(node, Comment):
            return ""
        if isinstance(node, NavigableString):
            return _html.escape(str(node))
        if isinstance(node, Tag):
            inner = "".join(render(c) for c in node.children)
            if node.name in _ALLOWED_DISPLAY_TAGS:
                return f"<{node.name}>{inner}</{node.name}>"  # 属性は一切許可しない
            return inner  # 非許可タグは剥がして中身だけ残す
        return ""

    return "".join(render(child) for child in soup.children)
