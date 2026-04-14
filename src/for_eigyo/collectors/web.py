"""Web スクレイパー（robots.txt 遵守）"""

from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import urlparse, urljoin
from urllib.robotparser import RobotFileParser

import httpx
from selectolax.parser import HTMLParser
from tenacity import retry, stop_after_attempt, wait_exponential

from for_eigyo.collectors.base import BaseCollector
from for_eigyo.storage.models import SearchResult

logger = logging.getLogger(__name__)

USER_AGENT = "ForEigyoBot/0.1 (+https://github.com/tyama-ds/for_eigyo)"


class WebCollector(BaseCollector):
    """軽量 Web スクレイパー"""

    name = "web"

    def __init__(self, timeout: float = 20.0, respect_robots: bool = True):
        self.timeout = timeout
        self.respect_robots = respect_robots
        self._robots_cache: dict[str, RobotFileParser] = {}

    def _can_fetch(self, url: str) -> bool:
        """robots.txt を確認"""
        if not self.respect_robots:
            return True

        parsed = urlparse(url)
        base = f"{parsed.scheme}://{parsed.netloc}"

        if base not in self._robots_cache:
            rp = RobotFileParser()
            robots_url = urljoin(base, "/robots.txt")
            try:
                rp.set_url(robots_url)
                rp.read()
            except Exception:
                logger.debug("Could not fetch robots.txt for %s", base)
                return True  # robots.txt 取得失敗時はアクセス許可
            self._robots_cache[base] = rp

        return self._robots_cache[base].can_fetch(USER_AGENT, url)

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, max=10))
    def fetch_page(self, url: str) -> str | None:
        """ページ HTML を取得"""
        if not self._can_fetch(url):
            logger.info("Blocked by robots.txt: %s", url)
            return None

        with httpx.Client(
            timeout=self.timeout,
            headers={"User-Agent": USER_AGENT},
            follow_redirects=True,
        ) as client:
            resp = client.get(url)
            resp.raise_for_status()
            return resp.text

    def extract_text(self, html: str) -> str:
        """HTML からテキストを抽出"""
        tree = HTMLParser(html)
        # script / style を除去
        for tag in tree.css("script, style, noscript"):
            tag.decompose()
        text = tree.body.text(separator="\n") if tree.body else ""
        # 空行の連続を整理
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    def extract_metadata(self, html: str, url: str) -> dict[str, str]:
        """meta タグ・OGP・タイトル等を抽出"""
        tree = HTMLParser(html)
        meta: dict[str, str] = {"url": url}

        title_node = tree.css_first("title")
        if title_node:
            meta["title"] = title_node.text().strip()

        for tag in tree.css("meta"):
            name = tag.attributes.get("name", tag.attributes.get("property", ""))
            content = tag.attributes.get("content", "")
            if name and content:
                meta[name] = content

        return meta

    def extract_links(self, html: str, base_url: str) -> list[dict[str, str]]:
        """ページ内のリンクを抽出"""
        tree = HTMLParser(html)
        links: list[dict[str, str]] = []
        for a in tree.css("a[href]"):
            href = a.attributes.get("href", "")
            if href and not href.startswith(("#", "javascript:", "mailto:")):
                absolute = urljoin(base_url, href)
                links.append({"url": absolute, "text": a.text().strip()})
        return links

    def search(self, query: str, **kwargs: Any) -> list[SearchResult]:
        """URL を直接指定してページを取得（queryはURL）"""
        url = query
        results: list[SearchResult] = []
        try:
            html = self.fetch_page(url)
            if html:
                meta = self.extract_metadata(html, url)
                text = self.extract_text(html)
                results.append(
                    SearchResult(
                        query=url,
                        title=meta.get("title", url),
                        url=url,
                        snippet=text[:500],
                        source="web",
                        raw_data={"metadata": meta, "text": text[:5000]},
                    )
                )
        except Exception:
            logger.exception("Web fetch failed: %s", url)

        return results

    def scrape_company_page(self, url: str) -> dict[str, Any]:
        """企業ページから情報を抽出"""
        result: dict[str, Any] = {"url": url}
        try:
            html = self.fetch_page(url)
            if not html:
                return result
            result["metadata"] = self.extract_metadata(html, url)
            result["text"] = self.extract_text(html)
            result["links"] = self.extract_links(html, url)
        except Exception:
            logger.exception("Company page scrape failed: %s", url)
        return result
