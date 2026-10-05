#!/usr/bin/env python3
"""Prism — ニュースポータル.

多数の RSS / Atom フィードを1画面に束ねて分光するニュース収集ポータル。

    python news-portal/server.py            # http://127.0.0.1:8780
    python news-portal/server.py --port 9300 --open
    python news-portal/server.py --demo     # ネットワークを使わずデモ記事で起動

- 標準ライブラリのみ（pip install 不要）。127.0.0.1 にのみ bind し外部公開しない
- フィードの登録は同じフォルダの feeds.json（UI の「情報源」からも編集可）
- 取得はスレッドプールで並列化し、TTL付きメモリキャッシュに保持する
- フィードに1件も到達できないときはオフラインのデモ記事で UI を満たす
- フィード本文は必ずプレーンテキスト化して返し、UI 側は textContent で描画（XSS対策）
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import html
import io
import ipaddress
import json
import os
import re
import secrets
import socket
import sqlite3
import ssl
import sys
import threading
import time
import unicodedata
import webbrowser
import zipfile
import zlib
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

import docgen   # 標準ライブラリだけの Word/Excel/PowerPoint/PDF 生成（同じフォルダ）

BASE = Path(__file__).resolve().parent
FEEDS_FILE = BASE / "feeds.json"
UI_FILE = BASE / "index.html"
ARCHIVE_DB = BASE / "archive.sqlite3"   # 過去記事の自動保存先（SQLite・条件検索用の索引つき）
ARCHIVE_FILE = BASE / "archive.jsonl"   # 旧形式。存在すれば初回起動時に SQLite へ取り込む

DEFAULT_PORT = 8780
HOST = "127.0.0.1"
CACHE_TTL = 600          # 秒。これより新しいキャッシュはそのまま配信する
FETCH_TIMEOUT = 8        # 秒。1フィードあたりの取得タイムアウト
MAX_PER_SOURCE = 40      # 1フィードから取り込む最大記事数
MAX_TOTAL = 600          # 全体の上限
MIN_PER_SOURCE = 8       # 上限あふれ時も各情報源に保証する最低枠（低頻度フィードの全滅防止）
MAX_SUMMARY = 320        # 要約の最大文字数
MAX_BODY = 256 * 1024    # POST ボディの上限（バイト）
MAX_FEED_BYTES = 6 * 1024 * 1024  # 1フィードの取得上限（バイト）
# 一部サイト（特に Google/Bing ニュース）はボット風UAに同意ページ/403を返すため、
# 一般的なブラウザ相当のUAを使う。
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")

AI_TIMEOUT = 60           # 生成AI呼び出しのタイムアウト（秒・クラウド）
AI_TIMEOUT_LOCAL = 300    # ローカルLLMは推論(思考)モデルが長考するため長め（秒）
AI_MAX_TOKENS = 1500      # AI応答の最大トークン
MAX_PAGE_TEXT = 6000      # 記事ページを本文コンテキストに含める最大文字数
MAX_PAGE_TEXT_LOCAL = 3500  # ローカルLLMは文脈窓が小さく、長い本文で出力が
                            # 思考の途中に切れて漏れやすいため短めにする
MIN_PAGE_TEXT = 200       # これ未満しか取れなければ「本文取得できず」とみなす
                          # （JS描画のSPAやブロックページは平文化するとほぼ空になる）
SELENIUM_TIMEOUT = 25     # ヘッドレスブラウザでのページ読込タイムアウト（秒）
ARCHIVE_MAX = 20000       # 過去ログ(archive.jsonl)の最大保持件数
AI_PROVIDERS = ("anthropic", "openai", "local")  # openai/local = OpenAI互換（base_url指定）
# local = ローカルLLM（Ollama / LM Studio / llama.cpp 等）。APIキー任意・プロキシ非経由
LOCAL_DEFAULT_BASE = "http://localhost:11434/v1"  # Ollama 既定
SETTINGS_FILE = BASE / "settings.json"  # AI API設定（APIキー等・.gitignore対象）

# カテゴリの正準リスト（UI の色・並びと対応）。「専門」は専門誌・学術系。
CATEGORIES = ["総合", "テクノロジー", "ビジネス", "科学", "専門", "世界", "スポーツ", "エンタメ"]

# 初期フィード（feeds.json が無いとき書き出される）
DEFAULT_SOURCES = [
    ("NHK 主要ニュース",     "https://www.nhk.or.jp/rss/news/cat0.xml",                    "総合"),
    ("NHK 経済",             "https://www.nhk.or.jp/rss/news/cat5.xml",                    "ビジネス"),
    ("NHK 国際",             "https://www.nhk.or.jp/rss/news/cat6.xml",                    "世界"),
    ("NHK 科学・文化",       "https://www.nhk.or.jp/rss/news/cat3.xml",                    "科学"),
    ("NHK スポーツ",         "https://www.nhk.or.jp/rss/news/cat7.xml",                    "スポーツ"),
    ("Yahoo!ニュース 主要",  "https://news.yahoo.co.jp/rss/topics/top-picks.xml",          "総合"),
    ("ITmedia NEWS",         "https://rss.itmedia.co.jp/rss/2.0/news_bursts.xml",          "テクノロジー"),
    ("GIGAZINE",             "https://gigazine.net/news/rss_2.0/",                         "テクノロジー"),
    ("Publickey",            "https://www.publickey1.jp/atom.xml",                         "テクノロジー"),
    ("はてブ 人気エントリー", "https://b.hatena.ne.jp/hotentry.rss",                        "総合"),
    ("TechCrunch",           "https://techcrunch.com/feed/",                               "テクノロジー"),
    ("The Verge",            "https://www.theverge.com/rss/index.xml",                     "テクノロジー"),
    ("Hacker News",          "https://hnrss.org/frontpage",                                "テクノロジー"),
    ("BBC World",            "https://feeds.bbci.co.uk/news/world/rss.xml",                "世界"),
    ("The Guardian World",   "https://www.theguardian.com/world/rss",                      "世界"),
    ("BBC Entertainment",    "https://feeds.bbci.co.uk/news/entertainment_and_arts/rss.xml", "エンタメ"),
    # 専門誌・学術系
    ("Nature",               "https://www.nature.com/nature.rss",                          "専門"),
    ("Science (AAAS)",       "https://www.science.org/rss/news_current.xml",               "専門"),
    ("IEEE Spectrum",        "https://spectrum.ieee.org/feeds/feed.rss",                   "専門"),
    ("MIT Technology Review","https://www.technologyreview.com/feed/",                      "専門"),
    ("ScienceDaily",         "https://www.sciencedaily.com/rss/all.xml",                   "専門"),
    ("Ars Technica",         "https://feeds.arstechnica.com/arstechnica/index",            "専門"),
    ("Harvard Business Review", "https://feeds.hbr.org/harvardbusiness",                   "専門"),
    ("MONOist（ものづくり）", "https://rss.itmedia.co.jp/rss/2.0/monoist.xml",              "専門"),
    ("EE Times Japan",       "https://rss.itmedia.co.jp/rss/2.0/eetimes.xml",              "専門"),
    ("arXiv cs.AI",          "https://rss.arxiv.org/rss/cs.AI",                            "専門"),
    ("arXiv 材料科学 (cond-mat.mtrl-sci)", "https://rss.arxiv.org/rss/cond-mat.mtrl-sci",      "専門"),
    ("arXiv 応用物理 (physics.app-ph)",    "https://rss.arxiv.org/rss/physics.app-ph",         "専門"),
    ("arXiv 機械学習 (cs.LG)",             "https://rss.arxiv.org/rss/cs.LG",                  "専門"),
    ("arXiv 制御・システム (eess.SY)",     "https://rss.arxiv.org/rss/eess.SY",                "専門"),
    # 鉄鋼・素材（ご要望: 日刊工業新聞系 / ISIJ 鉄と鋼 / Science系 Materials）
    ("Nature Materials",     "https://www.nature.com/nmat.rss",                            "専門"),
    ("ScienceDaily 材料科学", "https://www.sciencedaily.com/rss/matter_energy/materials_science.xml", "専門"),
    ("鉄と鋼（ISIJ・J-STAGE）", "https://api.jstage.jst.go.jp/searchapi/do?service=3&cdjournal=tetsutohagane&count=30", "専門"),
    ("ISIJ International（J-STAGE）", "https://api.jstage.jst.go.jp/searchapi/do?service=3&cdjournal=isijinternational&count=30", "専門"),
    ("ニュースイッチ（日刊工業新聞）", "https://newswitch.jp/rss",                          "専門"),
    # 産業・専門紙（「新聞」系メディア）。多くは自前RSS非提供のため Google ニュースRSSで
    # 各紙ドメインに絞って取得する（有効なRSSを返し、当該紙の記事に限定される）。
    ("電気新聞",             "https://news.google.com/rss/search?q=site:denkishimbun.com&hl=ja&gl=JP&ceid=JP:ja",   "専門"),
    ("日刊鉄鋼新聞",         "https://news.google.com/rss/search?q=site:japanmetaldaily.com&hl=ja&gl=JP&ceid=JP:ja", "専門"),
    ("日刊産業新聞（鉄鋼・非鉄）", "https://news.google.com/rss/search?q=site:japanmetal.com&hl=ja&gl=JP&ceid=JP:ja",     "専門"),
    ("電波新聞（電波新聞デジタル）", "https://news.google.com/rss/search?q=site:dempa-digital.com&hl=ja&gl=JP&ceid=JP:ja", "専門"),
    ("日刊工業新聞（本紙）", "https://news.google.com/rss/search?q=site:nikkan.co.jp&hl=ja&gl=JP&ceid=JP:ja",       "専門"),
    ("化学工業日報",         "https://news.google.com/rss/search?q=site:chemicaldaily.com&hl=ja&gl=JP&ceid=JP:ja",  "専門"),
    ("環境新聞",             "https://news.google.com/rss/search?q=site:kankyo-news.co.jp&hl=ja&gl=JP&ceid=JP:ja",  "専門"),
    ("日刊建設工業新聞",     "https://news.google.com/rss/search?q=site:decn.co.jp&hl=ja&gl=JP&ceid=JP:ja",         "専門"),
    # 需要産業（自動車・造船海事・建設）と物流・繊維の専門紙
    ("日刊自動車新聞",       "https://news.google.com/rss/search?q=site:netdenjd.com&hl=ja&gl=JP&ceid=JP:ja",       "専門"),
    ("日本海事新聞",         "https://news.google.com/rss/search?q=site:jmd.co.jp&hl=ja&gl=JP&ceid=JP:ja",           "専門"),
    ("建設通信新聞",         "https://news.google.com/rss/search?q=site:kensetsunews.com&hl=ja&gl=JP&ceid=JP:ja",   "専門"),
    ("日本物流新聞",         "https://news.google.com/rss/search?q=site:nb-shinbun.co.jp&hl=ja&gl=JP&ceid=JP:ja",   "専門"),
    ("物流ニッポン",         "https://news.google.com/rss/search?q=site:logistics.jp&hl=ja&gl=JP&ceid=JP:ja",       "専門"),
    ("繊研新聞",             "https://news.google.com/rss/search?q=site:senken.co.jp&hl=ja&gl=JP&ceid=JP:ja",       "専門"),
    # 経済一般（日経系・自前RSS非提供のため Google ニュース RSS）
    ("日本経済新聞（日経系）", "https://news.google.com/rss/search?q=site:nikkei.com&hl=ja&gl=JP&ceid=JP:ja",         "ビジネス"),
]

_cache_lock = threading.Lock()      # _cache の読み書きを保護
_refresh_lock = threading.Lock()    # 取得(refresh)を直列化しスタンピードを防ぐ
_sources_lock = threading.Lock()    # feeds.json の read-modify-write を保護
_settings_lock = threading.Lock()   # settings.json の read-modify-write を保護
_archive_lock = threading.Lock()    # archive.sqlite3 への書き込み/初期化を直列化
_archive_ready = False              # テーブル作成・旧JSONL取り込み済みフラグ
_cache: dict = {"articles": [], "errors": {}, "offline": None, "updated": None, "ts": 0.0}
DEMO = False                        # --demo 起動時 True（常にデモ記事を返す）


# ------------------------------------------------------------------ feeds.json

def _slug(name: str) -> str:
    base = "".join(c if c.isalnum() else "-" for c in name.lower()).strip("-")
    return base or "src"


def _ensure_ids(sources: list[dict]) -> list[dict]:
    seen: set[str] = set()
    for s in sources:
        sid = s.get("id") or _slug(s.get("name", "src"))
        uniq, n = sid, 2
        while uniq in seen:
            uniq, n = f"{sid}-{n}", n + 1
        s["id"] = uniq
        seen.add(uniq)
        s.setdefault("enabled", True)
        s.setdefault("category", "総合")
    return sources


def _defaults() -> list[dict]:
    return _ensure_ids([{"name": n, "url": u, "category": c, "enabled": True}
                        for (n, u, c) in DEFAULT_SOURCES])


def _load_locked() -> list[dict]:
    """feeds.json を読む（_sources_lock 保持前提）。壊れていても動き続ける。"""
    if not FEEDS_FILE.exists():
        sources = _defaults()
        _save_locked(sources)
        return sources
    try:
        data = json.loads(FEEDS_FILE.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return []
    if not isinstance(data, dict) or not isinstance(data.get("sources"), list):
        return []
    # dict 以外や url 欠落の要素は捨てる（手編集ミスで全体を壊さない）
    clean = [s for s in data["sources"] if isinstance(s, dict) and s.get("url")]
    return _ensure_ids(clean)


def _save_locked(sources: list[dict]) -> None:
    """原子的に書き出す（_sources_lock 保持前提）。書き込み中の破損を防ぐ。"""
    tmp = FEEDS_FILE.parent / (FEEDS_FILE.name + ".tmp")
    tmp.write_text(
        json.dumps({"sources": sources}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(tmp, FEEDS_FILE)


def load_sources() -> list[dict]:
    with _sources_lock:
        return _load_locked()


def add_source(name: str, url: str, category: str) -> list[dict]:
    with _sources_lock:
        sources = _load_locked()
        sources.append({"name": name, "url": url, "category": category, "enabled": True})
        _ensure_ids(sources)
        _save_locked(sources)
        return sources


def toggle_source(sid: str) -> tuple[list[dict], bool]:
    with _sources_lock:
        sources = _load_locked()
        hit = False
        for s in sources:
            if s.get("id") == sid:
                s["enabled"] = not s.get("enabled", True)
                hit = True
        if hit:
            _save_locked(sources)
        return sources, hit


def set_enabled(ids: list[str], enabled: bool) -> tuple[list[dict], int]:
    """指定 id 群の enabled を一括で設定（トグルではなく指定値）。変更件数を返す。"""
    want = set(ids)
    with _sources_lock:
        sources = _load_locked()
        n = 0
        for s in sources:
            if s.get("id") in want and bool(s.get("enabled", True)) != enabled:
                s["enabled"] = enabled
                n += 1
        if n:
            _save_locked(sources)
        return sources, n


def delete_source(sid: str) -> tuple[list[dict], bool]:
    with _sources_lock:
        sources = _load_locked()
        remain = [s for s in sources if s.get("id") != sid]
        changed = len(remain) != len(sources)
        if changed:
            _save_locked(remain)
        return remain, changed


# ------------------------------------------------------------------ 解析ユーティリティ

def _local(tag: str) -> str:
    """名前空間を落としたローカルタグ名（小文字）。"""
    return tag.rsplit("}", 1)[-1].lower()


def _find_local(parent, names: tuple[str, ...]):
    for el in list(parent):
        if _local(el.tag) in names:
            return el
    return None


def _findall_local(parent, names: tuple[str, ...]) -> list:
    return [el for el in list(parent) if _local(el.tag) in names]


def _text(el) -> str:
    if el is None:
        return ""
    return (el.text or "").strip()


_TAG_RE = re.compile(r"(?s)<[^>]+>")
_SCRIPT_RE = re.compile(r"(?is)<(script|style)\b.*?</\1>")
_WS_RE = re.compile(r"\s+")
_IMG_RE = re.compile(r"""(?i)<img[^>]+src\s*=\s*["']?([^"'>\s]+)""")
_IMGEXT_RE = re.compile(r"(?i)\.(?:jpg|jpeg|png|webp|gif|avif)(?:[?#]|$)")


def strip_html(s: str) -> str:
    """HTML をプレーンテキスト化する（描画は textContent なので二重に安全）。

    実体参照を戻したあとに再度タグ除去することで、二重エンコードされた
    ``&lt;script&gt;`` のような文字列がタグに復活しても確実に落とす。
    """
    if not s:
        return ""
    s = _SCRIPT_RE.sub(" ", s)
    s = html.unescape(s)
    s = _SCRIPT_RE.sub(" ", s)
    s = _TAG_RE.sub(" ", s)
    return _WS_RE.sub(" ", s).strip()


def safe_url(u: str | None) -> str | None:
    if not u:
        return None
    u = u.strip()
    p = urlparse(u)
    if p.scheme in ("http", "https") and p.netloc:
        return u
    return None


def parse_date(s: str) -> datetime | None:
    if not s:
        return None
    s = s.strip()
    try:  # RFC 822 (RSS の pubDate)
        dt = parsedate_to_datetime(s)
        if dt is not None:
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError, IndexError):
        pass
    try:  # ISO 8601 (Atom の updated/published)
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _extract_link(item) -> str | None:
    links = _findall_local(item, ("link",))
    # Atom: rel=alternate の href を最優先
    for el in links:
        href = el.get("href")
        if href and (el.get("rel") or "alternate").lower() == "alternate":
            return href.strip()
    for el in links:  # 次点: href を持つ任意の link / text 形式の link
        if el.get("href"):
            return el.get("href").strip()
        if _text(el):
            return _text(el)
    g = _find_local(item, ("guid", "id"))
    txt = _text(g)
    if txt.startswith("http"):
        return txt
    return None


def _extract_thumb(item, summary_html: str) -> str | None:
    best = None
    for el in item.iter():
        ln = _local(el.tag)
        url = el.get("url")
        if ln == "thumbnail" and url:
            return url
        if ln == "content" and url:
            typ = (el.get("type") or "") + " " + (el.get("medium") or "")
            if "image" in typ.lower() or _IMGEXT_RE.search(url):
                best = best or url
        if ln == "enclosure" and url:
            typ = (el.get("type") or "").lower()
            if typ.startswith("image") or _IMGEXT_RE.search(url):
                best = best or url
    if best:
        return best
    m = _IMG_RE.search(summary_html or "")
    return m.group(1) if m else None


def parse_feed(raw: bytes, src: dict) -> list[dict]:
    root = ET.fromstring(raw)  # ET.ParseError は呼び出し側で捕捉
    items = [e for e in root.iter() if _local(e.tag) in ("item", "entry")]
    out: list[dict] = []
    for it in items:
        title = strip_html(_text(_find_local(it, ("title",))))
        link = safe_url(_extract_link(it))
        # J-STAGE WebAPI 互換: 標準の title/link ではなく
        # <article_title><ja>…</ja></article_title> / <article_link> を使う
        if not title:
            at = _find_local(it, ("article_title",))
            if at is not None:
                title = strip_html(_text(_find_local(at, ("ja", "en")))
                                   or "".join(at.itertext()).strip())
        if not link:
            al = _find_local(it, ("article_link",))
            if al is not None:
                link = safe_url(_text(_find_local(al, ("ja", "en")))
                                or "".join(al.itertext()).strip())
        if not title or not link:
            continue
        raw_summary = _text(_find_local(
            it, ("description", "summary", "encoded", "content", "subtitle")))
        summary = strip_html(raw_summary)
        if len(summary) > MAX_SUMMARY:
            summary = summary[:MAX_SUMMARY].rstrip() + "…"
        cat_el = _find_local(it, ("category",))
        category = src.get("category") or "総合"
        dt = parse_date(_text(_find_local(
            it, ("pubdate", "published", "updated", "date", "issued"))))
        aid = hashlib.md5(link.encode("utf-8")).hexdigest()[:12]
        out.append({
            "id": aid,
            "source": src.get("name", ""),
            "source_id": src.get("id", ""),
            "category": category,
            "title": title,
            "link": link,
            "summary": summary,
            "thumbnail": safe_url(_extract_thumb(it, raw_summary)),
            "published": dt.isoformat() if dt else None,
            "published_ts": dt.timestamp() if dt else None,
        })
    return out


# ------------------------------------------------------------------ 取得

FEED_ACCEPT = "application/rss+xml, application/atom+xml, application/xml, text/xml;q=0.9, */*;q=0.8"


def _looks_like_feed(raw: bytes) -> bool:
    """本文が RSS/Atom/RDF フィードらしいか（同意ページ・ブロックページのHTMLを弾く）。"""
    head = raw[:2048].lstrip().lower()
    if head.startswith(b"<!doctype html") or head.startswith(b"<html"):
        return False
    return (b"<rss" in head or b"<feed" in head or b"<rdf" in head
            or b"<channel" in head or b"rss version" in head)


def _alt_aggregator(url: str) -> str:
    """Google ニュース RSS ⇄ Bing ニュース RSS の相互フォールバックURLを返す（無ければ空）。
    社内プロキシが一方のドメインをブロックしていても、もう一方で取得を試みる。"""
    try:
        p = urlparse(url)
        host, path = (p.hostname or "").lower(), p.path
        q = (parse_qs(p.query).get("q") or [""])[0]
        if not q:
            return ""
        if "news.google.com" in host and "/rss/search" in path:
            return "https://www.bing.com/news/search?q=" + quote(q) + "&format=RSS&setlang=ja"
        if "bing.com" in host and "/news/search" in path:
            return "https://news.google.com/rss/search?q=" + quote(q) + "&hl=ja&gl=JP&ceid=JP:ja"
    except (ValueError, UnicodeError):
        pass
    return ""


def _http_get(url: str, accept: str = FEED_ACCEPT, block_internal: bool = True,
              cfg: dict | None = None) -> dict:
    """1回のHTTP GET。ブラウザ相当のヘッダを送り、Google系には同意回避Cookieを付ける。
    例外は握って構造化した結果を返す（診断・フォールバックで使う）。
    cfg を渡すと保存済み設定の代わりにそのプロキシ設定で取得（接続テスト用）。"""
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": accept,
        "Accept-Encoding": "gzip, identity",
        "Accept-Language": "ja,en;q=0.8",
    }
    host = (urlparse(url).hostname or "").lower()
    if host.endswith("google.com"):
        headers["Cookie"] = "CONSENT=YES+cb; SOCS=CAISHAgBEhJnd3NfMjAyMw"   # EU同意ページ回避
    req = urllib.request.Request(url, headers=headers)
    try:
        with _opener(block_internal=block_internal, cfg=cfg).open(req, timeout=FETCH_TIMEOUT) as r:
            raw = r.read(MAX_FEED_BYTES + 1)
            return {"ok": True, "status": getattr(r, "status", 200) or 200,
                    "final_url": r.geturl(), "ctype": (r.headers.get("Content-Type") or ""),
                    "enc": (r.headers.get("Content-Encoding") or "").lower(),
                    "raw": raw, "error": None}
    except urllib.error.HTTPError as e:
        body = b""
        try:
            body = e.read(2048)
        except Exception:
            pass
        ctype = (e.headers.get("Content-Type") if e.headers else "") or ""
        return {"ok": False, "status": e.code, "final_url": url, "ctype": ctype,
                "enc": "", "raw": body, "error": f"HTTP {e.code} {e.reason}"}
    except Exception as e:   # URLError(接続不可/プロキシ/DNS)・SSLエラー・タイムアウト等
        return {"ok": False, "status": None, "final_url": url, "ctype": "",
                "enc": "", "raw": b"", "error": f"{type(e).__name__}: {e}"}


def _decode_feed_bytes(res: dict) -> bytes:
    raw = res["raw"]
    if res.get("enc") == "gzip" or raw[:2] == b"\x1f\x8b":
        raw = _gunzip_capped(raw, MAX_FEED_BYTES)
    return raw


def _fetch_and_parse(url: str, src: dict):
    """1URLを取得→解析。戻り値 (articles, error)。非フィード応答は明確なエラーにする。"""
    res = _http_get(url)
    if not res["ok"]:
        return [], res["error"]
    if len(res["raw"]) > MAX_FEED_BYTES:
        return [], "feed too large"
    try:
        raw = _decode_feed_bytes(res)
    except Exception as e:
        return [], f"decompress error: {type(e).__name__}"
    if not _looks_like_feed(raw):
        ct = (res["ctype"].split(";")[0] or "?").strip()
        return [], f"非フィード応答 (HTTP {res['status']}, {ct}) — 同意/ブロックページの可能性"
    arts = parse_feed(raw, src)
    return arts[:MAX_PER_SOURCE], None


def _fallback_urls(url: str) -> list[str]:
    """主URLで取得できない/0件のときに順に試す代替URL（最大2つ）。

    - Google⇄Bing ニュース検索は相互フォールバック（従来どおり）。
      Google が対象ドメインを索引していない「正常だが0件」も Bing で救う。
    - arXiv (rss.arxiv.org) → 公式の export.arxiv.org API（Atom・別ホスト）。
    - Hacker News (hnrss.org) → 本家 news.ycombinator.com/rss。
    - その他の直接フィード → Google ニュース site:ドメイン 検索 → Bing 同検索。
      社内プロキシが配信元ドメインを遮断していても news.google.com が通れば
      同じ媒体の記事を取得できる（プロキシ環境での主要な救済経路）。
    - IPアドレス直指定やドットなしホスト（イントラ/テスト）は対象外。
    """
    try:
        p = urlparse(url)
        host = (p.hostname or "").lower()
        if not host or "." not in host:
            return []
        try:
            ipaddress.ip_address(host)
            return []                        # IP直指定はフォールバックしない
        except ValueError:
            pass
        alt = _alt_aggregator(url)           # Google⇄Bing のニュース検索URL
        if alt:
            return [alt]
        if host == "rss.arxiv.org" and p.path.startswith("/rss/"):
            cat = p.path[len("/rss/"):]
            return ["https://export.arxiv.org/api/query?search_query=cat:" + quote(cat)
                    + "&sortBy=submittedDate&sortOrder=descending&max_results=30"]
        if host == "hnrss.org":
            return ["https://news.ycombinator.com/rss"]
        if host.endswith("google.com") or host.endswith("bing.com"):
            return []                        # 検索URL以外の google/bing は対象外
        domain = host[4:] if host.startswith("www.") else host
        q = quote("site:" + domain)
        return ["https://news.google.com/rss/search?q=" + q + "&hl=ja&gl=JP&ceid=JP:ja",
                "https://www.bing.com/news/search?q=" + q + "&format=RSS&setlang=ja"]
    except (ValueError, UnicodeError):
        return []


def fetch_source(src: dict) -> tuple[str, list[dict], str | None]:
    sid, url = src.get("id", ""), src.get("url", "")
    arts, err = _fetch_and_parse(url, src)
    if arts:
        return sid, arts, None
    # 取得失敗/非フィード/0件 → 代替URLを順に試す（0件でも試す:
    # Google が索引していないドメインを Bing が持っているケース等があるため）
    primary_ok_but_empty = err is None
    for alt in _fallback_urls(url)[:2]:
        alt_arts, alt_err = _fetch_and_parse(alt, src)
        if alt_arts:
            return sid, alt_arts, None
        host = urlparse(alt).hostname or "?"
        err = f"{err or '記事0件'} / 代替({host}): {alt_err or '0件'}"
    if primary_ok_but_empty and err is not None and not _fallback_urls(url):
        err = None   # 正常な空フィードで代替も無い場合はエラー扱いにしない
    return sid, [], err


def diagnose_source(src: dict) -> dict:
    """情報源1件を実際に取得し、失敗理由を切り分けるための詳細を返す
    （プロキシ/同意ページ/403/TLS/解析 の判別に使う）。"""
    cfg = proxy_config()
    mode = ("直結(proxyオフ)" if not cfg["use_proxy"]
            else (f"明示proxy: {cfg['proxy_url']}" if cfg["proxy_url"] else "環境変数のproxy"))
    ca = cfg.get("ca_bundle") or os.environ.get("SSL_CERT_FILE") or ""

    def probe(u: str) -> dict:
        res = _http_get(u)
        raw = res["raw"]
        looks = False
        items = 0
        try:
            if res["ok"]:
                dec = _decode_feed_bytes(res)
                looks = _looks_like_feed(dec)
                if looks:
                    items = len(parse_feed(dec, src))
        except Exception:
            pass
        snippet = ""
        try:
            snippet = raw[:160].decode("utf-8", "replace").replace("\n", " ").strip()
        except Exception:
            pass
        return {"url": u, "ok": res["ok"], "status": res["status"],
                "final_url": res["final_url"], "content_type": res["ctype"].split(";")[0],
                "bytes": len(raw), "looks_like_feed": looks, "items": items,
                "error": res["error"], "snippet": snippet}

    url = src.get("url", "")
    out = {"id": src.get("id", ""), "name": src.get("name", ""), "url": url,
           "proxy_mode": mode, "ca_bundle": ca or "(未設定/システム既定)",
           "primary": probe(url)}
    alts = [probe(u) for u in _fallback_urls(url)[:2]]
    out["alternatives"] = alts
    out["alternative"] = alts[0] if alts else None   # 旧フィールド互換
    return out


PROXY_TEST_URL = "https://rss.arxiv.org/rss/cs.AI"   # 接続テストの既定ターゲット


def proxy_test(body: dict) -> dict:
    """設定画面の「接続テスト」。フォームの値（保存前）でプロキシ経由の取得を試す。
    設定ファイルには一切書き込まない。"""
    use_proxy = bool(body.get("use_proxy", True))
    purl = (body.get("proxy_url") or "").strip()
    if not use_proxy:
        purl = ""
    elif purl and not safe_url(purl):
        return {"ok": False, "error": "proxy_url は http/https の有効なURLにしてください",
                "status": None, "items": 0, "looks_like_feed": False,
                "elapsed_ms": 0, "url": "", "proxy_mode": ""}
    cfg = {"use_proxy": use_proxy, "proxy_url": purl,
           "ca_bundle": (body.get("ca_bundle") or "").strip()}
    url = safe_url(body.get("url")) or PROXY_TEST_URL
    mode = ("直結(proxyオフ)" if not use_proxy
            else (f"明示proxy: {purl}" if purl else "環境変数のproxy"))
    t0 = time.time()
    res = _http_get(url, cfg=cfg)
    elapsed = int((time.time() - t0) * 1000)
    looks, items = False, 0
    if res["ok"]:
        try:
            raw = _decode_feed_bytes(res)
            looks = _looks_like_feed(raw)
            if looks:
                items = len(parse_feed(raw, {"id": "test", "name": "test", "category": "総合"}))
        except Exception:
            pass
    return {"ok": bool(res["ok"] and looks), "http_ok": res["ok"], "status": res["status"],
            "error": res["error"], "looks_like_feed": looks, "items": items,
            "elapsed_ms": elapsed, "url": url, "proxy_mode": mode,
            "content_type": (res["ctype"].split(";")[0] if res.get("ctype") else "")}


def _merge_articles(articles: list[dict]) -> list[dict]:
    """link 単位で重複除去し、全体上限 MAX_TOTAL に収める。

    単純な「新着順トップN」だと高頻度フィード（ニュースアグリゲータ等）が枠を
    独占し、低頻度の情報源（arXiv=日次 / Nature=週刊 など）が取得成功しても
    1件も残らない。そこで各情報源の最新 MIN_PER_SOURCE 件をまず確保してから、
    残り枠を全体の新着順で埋める。最終表示順は新しい順（日付なしは末尾）。
    """
    seen: set[str] = set()
    uniq: list[dict] = []
    for a in articles:
        if a["id"] in seen:
            continue
        seen.add(a["id"])
        uniq.append(a)
    key = lambda a: a["published_ts"] or 0.0   # 日付なしは末尾へ（安定ソートでフィード順維持）
    if len(uniq) <= MAX_TOTAL:
        uniq.sort(key=key, reverse=True)
        return uniq
    groups: dict[str, list[dict]] = {}
    for a in uniq:
        groups.setdefault(a["source_id"], []).append(a)
    keep: list[dict] = []
    rest: list[dict] = []
    for g in groups.values():
        g.sort(key=key, reverse=True)
        keep.extend(g[:MIN_PER_SOURCE])
        rest.extend(g[MIN_PER_SOURCE:])
    if len(keep) > MAX_TOTAL:
        # 情報源が極端に多い場合は各ソースの新着からラウンドロビンで公平に採用
        by_src = [g[:MIN_PER_SOURCE] for g in groups.values()]
        keep, i = [], 0
        while len(keep) < MAX_TOTAL:
            added = False
            for g in by_src:
                if i < len(g):
                    keep.append(g[i])
                    added = True
                    if len(keep) >= MAX_TOTAL:
                        break
            if not added:
                break
            i += 1
        rest = []
    rest.sort(key=key, reverse=True)
    keep.extend(rest[:MAX_TOTAL - len(keep)])
    keep.sort(key=key, reverse=True)
    return keep


# ------------------------------------------------------------------ 過去ログ（自動アーカイブ・SQLite）

def _db():
    """archive.sqlite3 への接続（呼び出し側で close する）。"""
    conn = sqlite3.connect(ARCHIVE_DB, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _archive_key(a: dict) -> float:
    """新着順ソート用キー。日付が無い記事は保存時刻で代用する。"""
    return a.get("published_ts") or a.get("archived_at") or 0.0


def _archive_row(a: dict, now: float) -> tuple:
    at = a.get("archived_at") or now
    return (a.get("id"), a.get("source_id"), a.get("source"), a.get("category"),
            a.get("title"), a.get("link"), a.get("summary"), a.get("published"),
            a.get("published_ts"), at, a.get("published_ts") or at)


def _norm(s) -> str:
    """検索用の正規化: NFKC（全角/半角・互換文字の統一）＋小文字化＋空白の畳み込み。"""
    return _WS_RE.sub(" ", unicodedata.normalize("NFKC", str(s or ""))).strip().lower()


def _fts_text(title, summary, source) -> str:
    """索引に入れる文字列: タイトル・要約・媒体名を正規化して連結。"""
    return _norm(f"{title or ''} \n {summary or ''} \n {source or ''}")


_fts_ok = False   # SQLite の FTS5（trigram）が使えるか。_fts_setup() で判定する


def _fts_available(conn) -> bool:
    try:
        conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS _fts_probe USING fts5(x, tokenize='trigram')")
        conn.execute("DROP TABLE IF EXISTS _fts_probe")
        return True
    except sqlite3.OperationalError:
        return False


def _fts_rows(rows) -> list[tuple]:
    return [(r[0], _fts_text(r[1], r[2], r[3])) for r in rows]


def _fts_rebuild(conn) -> None:
    conn.execute("DELETE FROM articles_fts")
    rows = conn.execute("SELECT rowid, title, summary, source FROM articles").fetchall()
    for i in range(0, len(rows), 2000):
        conn.executemany("INSERT INTO articles_fts(rowid, text) VALUES (?,?)", _fts_rows(rows[i:i + 2000]))


def _fts_setup(conn) -> None:
    """正規化テキストの索引テーブル articles_fts を用意する。FTS5(trigram) が使えれば仮想テーブル、
    無ければ通常テーブル。どちらも rowid=articles.rowid と text 列を持ち、instr() の部分一致に使える。
    件数が articles と合わなければ（旧DB・FTS5 の有無が変わった）作り直す。"""
    global _fts_ok
    row = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'articles_fts'").fetchone()
    existing = (row[0] or "") if row else None
    want = _fts_available(conn)
    if existing is not None and (("fts5" in existing.lower()) != want):
        conn.execute("DROP TABLE articles_fts")
        existing = None
    if existing is None:
        if want:
            conn.execute("CREATE VIRTUAL TABLE articles_fts USING fts5(text, tokenize='trigram')")
        else:
            conn.execute("CREATE TABLE articles_fts(id INTEGER PRIMARY KEY, text TEXT)")
    _fts_ok = want
    n_a = conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
    n_f = conn.execute("SELECT COUNT(*) FROM articles_fts").fetchone()[0]
    if n_a != n_f:
        _fts_rebuild(conn)


def _archive_init_locked() -> None:
    """テーブル作成と、旧形式 archive.jsonl の1回限りの取り込み。_archive_lock 内で呼ぶ。"""
    global _archive_ready
    if _archive_ready:
        return
    conn = _db()
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("""CREATE TABLE IF NOT EXISTS articles(
            id TEXT PRIMARY KEY, source_id TEXT, source TEXT, category TEXT,
            title TEXT, link TEXT, summary TEXT, published TEXT,
            published_ts REAL, archived_at REAL, sort_ts REAL)""")
        conn.execute("CREATE INDEX IF NOT EXISTS ix_articles_sort ON articles(sort_ts DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS ix_articles_src ON articles(source_id, sort_ts DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS ix_articles_cat ON articles(category, sort_ts DESC)")
        conn.execute("""CREATE TABLE IF NOT EXISTS reports(
            id TEXT PRIMARY KEY, created_at REAL, title TEXT, question TEXT, template TEXT,
            filters_json TEXT, article_ids_json TEXT, markdown TEXT, sources_json TEXT,
            model TEXT, stats_json TEXT)""")   # リサーチのレポート（生成結果の保存）
        conn.execute("""CREATE TABLE IF NOT EXISTS themes(
            id TEXT PRIMARY KEY, name TEXT, filters_json TEXT, created_at REAL, updated_at REAL,
            last_seen_at REAL, last_brief_at REAL, last_brief_id TEXT)""")   # 保存した検索条件（テーマ）
        if "kind" not in {r[1] for r in conn.execute("PRAGMA table_info(themes)")}:
            conn.execute("ALTER TABLE themes ADD COLUMN kind TEXT DEFAULT 'theme'")   # theme / watch（企業・製品ウォッチ）
        conn.execute("""CREATE TABLE IF NOT EXISTS pages(
            article_id TEXT PRIMARY KEY, link TEXT, text TEXT, via TEXT, error TEXT,
            fetched_at REAL, chars INTEGER)""")   # 記事本文のキャッシュ（一括取得の結果）
        conn.execute("""CREATE TABLE IF NOT EXISTS exports(
            id TEXT PRIMARY KEY, created_at REAL, kind TEXT, title TEXT, filename TEXT, bytes INTEGER,
            report_id TEXT, instructions TEXT, meta_json TEXT)""")   # 生成した文書ファイル（Word/Excel/PowerPoint/PDF）
        if ARCHIVE_FILE.exists():   # 旧 JSONL → SQLite 取り込み（取り込み後は .imported に退避）
            rows = []
            try:
                with open(ARCHIVE_FILE, encoding="utf-8") as f:
                    for line in f:
                        try:
                            a = json.loads(line)
                        except ValueError:
                            continue
                        if isinstance(a, dict) and a.get("id"):
                            rows.append(_archive_row(a, time.time()))
            except OSError:
                rows = []
            if rows:
                conn.executemany("INSERT OR IGNORE INTO articles VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
            conn.commit()
            try:
                os.replace(ARCHIVE_FILE, ARCHIVE_FILE.with_name(ARCHIVE_FILE.name + ".imported"))
            except OSError:
                pass
        _fts_setup(conn)   # 全文索引（件数が合わなければ再構築）
        conn.commit()
    finally:
        conn.close()
    _archive_ready = True


def archive_add(articles: list[dict]) -> int:
    """取得した記事を過去ログ(SQLite)へ自動保存する。id で重複排除（INSERT OR IGNORE）。
    新規行は全文索引にも入れる。上限 ARCHIVE_MAX 超過時は古い順に削除（索引・本文キャッシュも掃除）。
    デモ記事は保存しない。戻り値は新規追加件数。"""
    now = time.time()
    fresh = [a for a in articles if a.get("id") and a.get("source_id") != "demo"]
    if not fresh:
        return 0
    with _archive_lock:
        _archive_init_locked()
        conn = _db()
        try:
            max_rowid = conn.execute("SELECT coalesce(MAX(rowid), 0) FROM articles").fetchone()[0]
            conn.executemany("INSERT OR IGNORE INTO articles VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                             [_archive_row(a, now) for a in fresh])
            new_rows = conn.execute("SELECT rowid, title, summary, source FROM articles WHERE rowid > ?",
                                    (max_rowid,)).fetchall()
            if new_rows:
                conn.executemany("INSERT INTO articles_fts(rowid, text) VALUES (?,?)", _fts_rows(new_rows))
            cnt = conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
            if cnt > ARCHIVE_MAX:
                conn.execute("DELETE FROM articles WHERE id NOT IN "
                             "(SELECT id FROM articles ORDER BY sort_ts DESC LIMIT ?)", (ARCHIVE_MAX,))
                conn.execute("DELETE FROM articles_fts WHERE rowid NOT IN (SELECT rowid FROM articles)")
                conn.execute("DELETE FROM pages WHERE article_id NOT IN (SELECT id FROM articles)")
            conn.commit()
            return len(new_rows)
        finally:
            conn.close()


def archive_stats() -> dict:
    """件数・期間に加え、フィルタUI用に情報源別/カテゴリ別の件数を返す。"""
    with _archive_lock:
        _archive_init_locked()
        conn = _db()
        try:
            n, lo, hi = conn.execute("SELECT COUNT(*), MIN(sort_ts), MAX(sort_ts) FROM articles").fetchone()
            srcs = [{"id": r[0] or "", "name": r[1] or "", "count": r[2]} for r in conn.execute(
                "SELECT source_id, MAX(source), COUNT(*) AS n FROM articles "
                "GROUP BY source_id ORDER BY n DESC, source_id")]
            cats = [{"name": r[0] or "", "count": r[1]} for r in conn.execute(
                "SELECT category, COUNT(*) AS n FROM articles GROUP BY category ORDER BY n DESC")]
        finally:
            conn.close()
    return {"count": n, "oldest": lo, "newest": hi, "sources": srcs, "categories": cats, "fts": _fts_ok}


# ---- 検索語の解釈（AND / OR / 除外 / 同義語辞書）

DEFAULT_SYNONYMS = [["高炉", "ブラストファーネス"], ["電炉", "電気炉"], ["EV", "電気自動車"],
                    ["脱炭素", "カーボンニュートラル"], ["CCUS", "CCS"]]
_SYN_SPLIT_RE = re.compile(r"[,、，|｜/／\t]+")
MAX_QUERY_GROUPS = 10


def synonym_groups() -> list[list[str]]:
    g = load_settings().get("synonyms")
    return g if isinstance(g, list) else [list(x) for x in DEFAULT_SYNONYMS]


def parse_synonyms_text(text: str) -> list[list[str]]:
    """「高炉, ブラストファーネス」のような 1行1グループ のテキストを同義語グループにする。"""
    groups: list[list[str]] = []
    for line in str(text or "").splitlines():
        words: list[str] = []
        for w in _SYN_SPLIT_RE.split(line):
            w = w.strip()[:40]
            if w and _norm(w) not in [_norm(x) for x in words]:
                words.append(w)
        if len(words) >= 2:
            groups.append(words[:20])
        if len(groups) >= 200:
            break
    return groups


def synonyms_text(groups: list[list[str]] | None = None) -> str:
    return "\n".join(", ".join(g) for g in (groups if groups is not None else synonym_groups()))


def save_synonyms(text: str) -> dict:
    groups = parse_synonyms_text(text)
    s = load_settings()
    s["synonyms"] = groups
    save_settings(s)
    return {"ok": True, "groups": groups, "text": synonyms_text(groups), "fts": _fts_ok}


def _synonym_map(groups: list | None = None) -> dict[str, list[str]]:
    """正規化した語 → その同義語グループ（原語のリスト）。"""
    m: dict[str, list[str]] = {}
    for g in (groups if groups is not None else synonym_groups()):
        if isinstance(g, list) and len(g) >= 2:
            for w in g:
                m.setdefault(_norm(w), [str(x) for x in g])
    return m


def _query_tokens(q_norm: str) -> list[str]:
    """空白で語に分ける。ただし "..." で囲んだ部分（"nippon steel" など）は空白を含めて1語として扱う。"""
    parts = re.split(r'("[^"]*")', q_norm)
    joined = "".join(p.replace(" ", "\x00") if p.startswith('"') else p for p in parts)
    return [t.replace("\x00", " ") for t in joined.split(" ") if t.strip()]


def quote_term(w: str) -> str:
    """空白を含む語を検索構文用に引用符で囲む（企業名の別表記などを | で並べるとき用）。"""
    w = str(w or "").strip().replace('"', "")
    return f'"{w}"' if " " in w else w


def parse_query(q: str, syn: dict | None = None) -> dict:
    """検索語を解釈する。スペース区切り=AND、語の中の | =OR、先頭の - =除外、"..." =空白を含む1語。
    同義語辞書で OR を自動展開。
    戻り値 {"groups": [[正規化語, ...], ...], "excludes": [正規化語, ...],
            "expanded": [{"term": 語, "to": [展開された原語, ...]}, ...]}"""
    syn = _synonym_map() if syn is None else syn
    groups: list[list[str]] = []
    excludes: list[str] = []
    expanded: list[dict] = []
    for tok in _query_tokens(_norm(q)):
        tok = tok.strip()
        if not tok or not tok.strip('-|"'):   # 記号だけの語（"-" "|" など）は無視
            continue
        if tok.startswith("-"):
            w = tok[1:].strip('"')
            if w:
                excludes.append(w)
            continue
        alts = [a.strip().strip('"') for a in tok.split("|")]
        out: list[str] = []
        for a in alts:
            if not a:
                continue
            out.append(a)
            g = syn.get(a)
            if g:
                adds = [w for w in g if _norm(w) != a]
                if adds:
                    expanded.append({"term": a, "to": adds})
                    out.extend(_norm(w) for w in adds)
        out = list(dict.fromkeys(out))[:20]
        if out:
            groups.append(out)
    return {"groups": groups[:MAX_QUERY_GROUPS], "excludes": excludes[:MAX_QUERY_GROUPS], "expanded": expanded}


def _fts_phrase(w: str) -> str:
    return '"' + w.replace('"', '""') + '"'


_SEARCH_FROM = "FROM articles a JOIN articles_fts ON articles_fts.rowid = a.rowid"


def _search_where(q: str, sources: list[str] | None, since_ts: float | None, until_ts: float | None,
                  category: str | None, archived_since: float | None = None,
                  parsed: dict | None = None) -> tuple[str, list, bool]:
    """archive_search / ヒストグラム / テーマの件数 で共用する WHERE 句（_SEARCH_FROM を前提）。
    3文字以上の語だけから成る OR グループは FTS5 の MATCH（trigram）、2文字以下を含むグループと
    除外語は正規化テキストへの instr() で評価する。戻り値 (where_sql, params, MATCH を使ったか)。"""
    p = parsed if parsed is not None else parse_query(q)
    where: list[str] = []
    params: list = []
    match_parts: list[str] = []
    for g in p["groups"]:
        if _fts_ok and all(len(w) >= 3 for w in g):
            match_parts.append("(" + " OR ".join(_fts_phrase(w) for w in g) + ")")
        else:
            where.append("(" + " OR ".join("instr(articles_fts.text, ?) > 0" for _ in g) + ")")
            params.extend(g)
    uses_match = bool(match_parts)
    if uses_match:
        where.insert(0, "articles_fts MATCH ?")
        params.insert(0, " AND ".join(match_parts))
    for w in p["excludes"]:
        where.append("instr(articles_fts.text, ?) = 0")
        params.append(w)
    srcs = [s for s in (sources or []) if isinstance(s, str) and s][:100]
    if srcs:
        where.append("a.source_id IN (%s)" % ",".join("?" * len(srcs)))
        params.extend(srcs)
    if category:
        where.append("a.category = ?")
        params.append(category)
    if since_ts is not None:
        where.append("a.sort_ts >= ?")
        params.append(float(since_ts))
    if until_ts is not None:
        where.append("a.sort_ts < ?")
        params.append(float(until_ts))
    if archived_since is not None:   # 新着差分: 前回確認以降に過去ログへ入った記事
        where.append("a.archived_at > ?")
        params.append(float(archived_since))
    return (" WHERE " + " AND ".join(where)) if where else "", params, uses_match


def archive_search(q: str, limit: int = 60, sources: list[str] | None = None,
                   since_ts: float | None = None, until_ts: float | None = None,
                   category: str | None = None, archived_since: float | None = None,
                   order: str = "new", parsed: dict | None = None) -> list[dict]:
    """過去ログを条件検索する。条件はすべて AND:
    - q: 検索語（parse_query の構文。全角/半角・大小文字は区別しない。同義語辞書で展開）
    - sources: source_id のリスト（いずれかに一致）
    - since_ts / until_ts: sort_ts（公開日時、無ければ保存日時）の範囲 [since, until)
    - category: カテゴリ名の完全一致
    - archived_since: 保存日時（archived_at）がこれより後の記事だけ（テーマの「新着のみ」）
    order="rel" かつ FTS の MATCH を使えた場合は関連順（bm25）、それ以外は新着順。
    各行に has_text（本文キャッシュあり）を付けて返す。"""
    where, params, uses_match = _search_where(q, sources, since_ts, until_ts, category, archived_since, parsed)
    try:
        n = max(1, min(int(limit), 300))
    except (TypeError, ValueError):
        n = 60
    order_sql = ("articles_fts.rank, a.sort_ts DESC" if (order == "rel" and uses_match) else "a.sort_ts DESC")
    sql = ("SELECT a.*, (p.article_id IS NOT NULL AND coalesce(p.chars,0) > 0) AS has_text "
           + _SEARCH_FROM + " LEFT JOIN pages p ON p.article_id = a.id"
           + where + " ORDER BY " + order_sql + " LIMIT ?")
    params.append(n)
    with _archive_lock:
        _archive_init_locked()
        conn = _db()
        try:
            rows = [dict(r) for r in conn.execute(sql, params)]
        finally:
            conn.close()
    for r in rows:
        r["has_text"] = bool(r.get("has_text"))
    return rows


def _filters_window(f: dict, now: float | None = None) -> tuple[float | None, float | None]:
    """検索条件の期間（from/to の日付範囲、無ければ直近 days 日）を [since, until) の epoch に。
    since_ts / until_ts（epoch）が直接あればそれを優先（比較ビューの「前の期間」など秒単位の範囲）。"""
    if isinstance(f.get("since_ts"), (int, float)) or isinstance(f.get("until_ts"), (int, float)):
        return (float(f["since_ts"]) if isinstance(f.get("since_ts"), (int, float)) else None,
                float(f["until_ts"]) if isinstance(f.get("until_ts"), (int, float)) else None)
    since = _parse_day(f.get("from")) if isinstance(f.get("from"), str) else None
    until = _parse_day(f.get("to")) if isinstance(f.get("to"), str) else None
    if until is not None:
        until += 86400   # to は当日を含む
    try:
        days = int(f.get("days") or 0)
    except (TypeError, ValueError):
        days = 0
    if since is None and days > 0:
        since = (now or time.time()) - days * 86400
    return since, until


# ---- 類似記事の束ね（別媒体の同じニュース等）

_BRACKET_RE = re.compile(r"[【\[（(［〔].*?[】\]）)］〕]")
_TITLE_SPLIT_RE = re.compile(r"\s[-|—–]\s|｜|\s\|\s")
_TITLE_STRIP_RE = re.compile(r"[\W_]+")


def _title_key(t: str) -> str:
    t = _BRACKET_RE.sub(" ", _norm(t))
    t = _TITLE_SPLIT_RE.split(t)[0]   # 「… - 媒体名」「…｜媒体名」の後置きを落とす
    return _TITLE_STRIP_RE.sub("", t)


def _bigrams(s: str) -> set:
    return {s[i:i + 2] for i in range(len(s) - 1)} if len(s) > 1 else ({s} if s else set())


def group_duplicates(rows: list[dict], window: float = 3 * 86400, threshold: float = 0.6) -> int:
    """タイトルが似ていて日付が近い記事を束ねる。先に出る方（新しい方）を代表にし、代表には dups=[id...]、
    他には dup_of=代表id を付ける。判定: 正規化タイトルが一致／片方を含む（10字以上）／文字バイグラムの
    Jaccard が threshold 以上（8字以上）。戻り値は 2件以上の束の数。"""
    heads: list[tuple[str, set, float, dict]] = []
    for r in rows:
        k = _title_key(r.get("title") or "")
        bg = _bigrams(k)
        ts = float(r.get("sort_ts") or 0)
        hit = None
        for hk, hbg, hts, hr in heads:
            if abs(hts - ts) > window or not k or not hk:
                continue
            if k == hk or (len(k) >= 10 and len(hk) >= 10 and (k in hk or hk in k)):
                hit = hr
                break
            if min(len(k), len(hk)) >= 8:
                inter = len(bg & hbg)
                if inter and inter / len(bg | hbg) >= threshold:
                    hit = hr
                    break
        if hit is not None:
            hit.setdefault("dups", []).append(r["id"])
            r["dup_of"] = hit["id"]
        else:
            heads.append((k, bg, ts, r))
    return sum(1 for _, _, _, r in heads if r.get("dups"))


# ---- 日付×情報源のヒストグラム

def archive_histogram(q: str, sources: list[str] | None, category: str | None,
                      parsed: dict | None = None, since_ts: float | None = None,
                      until_ts: float | None = None) -> dict:
    """検索条件に合う記事の、日付×情報源の件数分布。既定では期間を無視して全期間を集計し
    （期間の選択に使う）、since/until を与えればその範囲だけ（比較ビュー用）。
    期間の長さに応じて 日／週／月 単位にまとめる（区間は最大 ~120）。日付はローカル時刻。
    情報源は件数上位6つ＋その他（other）。"""
    where, params, _ = _search_where(q, sources, since_ts, until_ts, category, None, parsed)
    with _archive_lock:
        _archive_init_locked()
        conn = _db()
        try:
            rows = conn.execute("SELECT a.sort_ts, a.source_id, a.source " + _SEARCH_FROM + where,
                                params).fetchall()
        finally:
            conn.close()
    if not rows:
        return {"unit": "day", "total": 0, "sources": [], "buckets": []}
    tss = [float(r[0] or 0) for r in rows]
    lo, hi = min(tss), max(max(tss), time.time())
    span = (hi - lo) / 86400
    unit = "day" if span <= 120 else "week" if span <= 840 else "month"

    def bstart(t: float) -> date:
        d = datetime.fromtimestamp(t).date()
        if unit == "week":
            return d - timedelta(days=d.weekday())
        if unit == "month":
            return d.replace(day=1)
        return d

    def bnext(d: date) -> date:
        if unit == "week":
            return d + timedelta(days=7)
        if unit == "month":
            return (d.replace(day=28) + timedelta(days=4)).replace(day=1)
        return d + timedelta(days=1)

    src_total: dict[str, int] = {}
    names: dict[str, str] = {}
    for _, sid, sname in rows:
        sid = sid or ""
        src_total[sid] = src_total.get(sid, 0) + 1
        names.setdefault(sid, sname or sid)
    top = [s for s, _ in sorted(src_total.items(), key=lambda x: (-x[1], x[0]))[:6]]
    buckets: dict[date, dict[str, int]] = {}
    for t, sid, _ in zip(tss, (r[1] or "" for r in rows), rows):
        c = buckets.setdefault(bstart(t), {})
        k = sid if sid in top else "other"
        c[k] = c.get(k, 0) + 1
    out: list[dict] = []
    d, last = min(buckets), max(buckets)
    while d <= last and len(out) < 400:
        c = buckets.get(d, {})
        out.append({"from": d.isoformat(), "to": (bnext(d) - timedelta(days=1)).isoformat(),
                    "total": sum(c.values()), "src": c})
        d = bnext(d)
    return {"unit": unit, "total": len(rows),
            "sources": [{"id": s, "name": names.get(s, s), "count": src_total[s]} for s in top],
            "other": sum(v for s, v in src_total.items() if s not in top), "buckets": out}


def _parse_day(s: str | None) -> float | None:
    """YYYY-MM-DD を その日のローカル時刻 0時 の epoch に（画面の日付・ヒストグラムの区間と揃える）。
    不正なら None。"""
    if not s or not isinstance(s, str):
        return None
    try:
        return datetime.strptime(s.strip()[:10], "%Y-%m-%d").timestamp()
    except ValueError:
        return None


def refresh(sources: list[dict]) -> dict:
    enabled = [s for s in sources if s.get("enabled", True)]
    articles: list[dict] = []
    errors: dict[str, str] = {}
    if enabled:
        with ThreadPoolExecutor(max_workers=min(8, len(enabled))) as ex:
            for sid, arts, err in ex.map(fetch_source, enabled):
                if err:
                    errors[sid] = err
                articles.extend(arts)
    # 過去ログへ自動保存（表示上限 MAX_TOTAL とは独立に、取得できた全記事を対象）
    try:
        seen_ids: set[str] = set()
        all_uniq = [a for a in articles
                    if a.get("id") and not (a["id"] in seen_ids or seen_ids.add(a["id"]))]
        archive_add(all_uniq)
    except Exception:
        pass   # アーカイブ失敗で表示を壊さない
    uniq = _merge_articles(articles)

    offline = len(uniq) == 0
    if offline:
        uniq = demo_articles()
    return {
        "articles": uniq,
        "errors": errors,
        "offline": offline,
        "updated": datetime.now(timezone.utc).isoformat(),
        "ts": time.time(),
    }


def get_feed(force: bool = False) -> dict:
    if DEMO:  # --demo 起動時は常にキャッシュ済みデモを返す（force でも再取得しない）
        with _cache_lock:
            return dict(_cache)
    with _cache_lock:
        fresh = bool(_cache["ts"]) and (time.time() - _cache["ts"] < CACHE_TTL)
        if fresh and not force:
            return dict(_cache)
    # ネットワーク取得は _cache_lock の外で行い、読み取り側をブロックしない。
    # _refresh_lock で直列化してスタンピード（同時多重取得）を防ぐ。
    with _refresh_lock:
        with _cache_lock:
            fresh = bool(_cache["ts"]) and (time.time() - _cache["ts"] < CACHE_TTL)
            if fresh and not force:
                return dict(_cache)
        data = refresh(load_sources())
        with _cache_lock:
            _cache.update(data)
            return dict(_cache)


# ------------------------------------------------------------------ 設定 / 生成AI

def load_settings() -> dict:
    with _settings_lock:
        if not SETTINGS_FILE.exists():
            return {}
        try:
            data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return {}
        return data if isinstance(data, dict) else {}


def save_settings(data: dict) -> None:
    with _settings_lock:
        tmp = SETTINGS_FILE.parent / (SETTINGS_FILE.name + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                       encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)   # APIキーを含むため所有者のみ読み書き可に
        except OSError:
            pass
        os.replace(tmp, SETTINGS_FILE)


def proxy_config() -> dict:
    """情報取得・クラウドAI に使うプロキシ設定（llmlab と同じ流儀）。

    - use_proxy=False           → 直結（環境変数のプロキシも無視）
    - use_proxy=True + proxy_url → その URL を使用
    - use_proxy=True + 空        → 環境変数 HTTP(S)_PROXY を使用（既定）
    """
    x = load_settings().get("proxy")
    p = x if isinstance(x, dict) else {}   # 壊れた settings.json でも崩れない
    return {"use_proxy": bool(p.get("use_proxy", True)),
            "proxy_url": (p.get("proxy_url") or "").strip(),
            "ca_bundle": (p.get("ca_bundle") or "").strip()}   # 社内プロキシのCA証明書(任意)


def browser_config() -> dict:
    """本文取得のヘッドレスブラウザ設定（設定画面から入力・いずれも任意）。

    - binary: ブラウザ実行ファイルのパス（空=インストール済み Chrome/Edge を自動検出）
    - driver: WebDriver（chromedriver / msedgedriver）のパス
      （空= Selenium Manager が自動解決）
    環境変数 PRISM_BROWSER_BINARY / PRISM_CHROMEDRIVER は設定が空のときの
    フォールバックとして機能する。
    """
    raw = browser_settings_raw()
    return {"binary": raw["binary"] or (os.environ.get("PRISM_BROWSER_BINARY") or "").strip(),
            "driver": raw["driver"] or (os.environ.get("PRISM_CHROMEDRIVER") or "").strip()}


def browser_settings_raw() -> dict:
    """settings.json に保存されたままのブラウザ設定（設定画面のフォーム表示用）。"""
    x = load_settings().get("browser")
    b = x if isinstance(x, dict) else {}
    return {"binary": (b.get("binary") or "").strip(),
            "driver": (b.get("driver") or "").strip()}


def _host_is_internal(url: str) -> bool:
    """URL のホストがループバック/リンクローカル/プライベート等に解決されるか（SSRF対策）。"""
    try:
        host = urlparse(url).hostname
        if not host:
            return False
        for info in socket.getaddrinfo(host, None):
            ip = ipaddress.ip_address(info[4][0])
            if (ip.is_loopback or ip.is_link_local or ip.is_private
                    or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
                return True
        return False
    except (socket.gaierror, ValueError, OSError, UnicodeError):
        return False


class _NoInternalRedirect(urllib.request.HTTPRedirectHandler):
    """内部アドレスや http/https 以外へのリダイレクトを追わない（SSRF対策）。"""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urlparse(newurl).scheme not in ("http", "https") or _host_is_internal(newurl):
            return None
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _gunzip_capped(raw: bytes, cap: int) -> bytes:
    """gzip/zlib を上限付きで展開する（出力を cap+1 までしか確保しない = gzip爆弾対策）。"""
    out = zlib.decompressobj(47).decompress(raw, cap + 1)   # 47: gzip/zlib 自動判定
    if len(out) > cap:
        raise ValueError("decompressed data exceeds cap")
    return out


def _ssl_context(cfg: dict | None = None):
    """HTTPS 検証用の SSL コンテキスト。社内プロキシがTLSを傍受(MITM)する環境では
    その CA を settings の ca_bundle か環境変数 SSL_CERT_FILE で指定できる。
    検証は常に有効（無効化はしない）。指定が無ければ既定(システムCA/環境変数)を使う。"""
    ca = (cfg or proxy_config()).get("ca_bundle") or os.environ.get("SSL_CERT_FILE") or ""
    try:
        if ca and os.path.exists(ca):
            return ssl.create_default_context(cafile=ca)
    except (ssl.SSLError, OSError):
        pass
    return None   # None → urllib 既定（システムCA、環境変数も反映）


def _opener(force_direct: bool = False, block_internal: bool = False,
            cfg: dict | None = None):
    """proxy_config に従って urllib の opener を作る（llmlab の3モードに対応）。

    force_direct=True でローカルLLM等は常に直結。block_internal=True で
    内部アドレスへのリダイレクトを遮断する（情報取得の SSRF 対策）。
    cfg を渡すと保存済み設定の代わりにその値を使う（接続テスト用・保存しない）。
    """
    cfg = cfg or proxy_config()
    extra = [_NoInternalRedirect()] if block_internal else []
    ctx = _ssl_context(cfg)
    if ctx is not None:
        extra.append(urllib.request.HTTPSHandler(context=ctx))   # 社内CAを信頼
    if force_direct or not cfg["use_proxy"]:
        extra.append(urllib.request.ProxyHandler({}))                         # 直結
    elif cfg["proxy_url"]:
        p = cfg["proxy_url"]
        extra.append(urllib.request.ProxyHandler({"http": p, "https": p}))    # 明示URL
    # それ以外は環境変数のプロキシ（build_opener が既定の ProxyHandler を付与）
    return urllib.request.build_opener(*extra)


def ai_config() -> dict:
    x = load_settings().get("ai")
    ai = x if isinstance(x, dict) else {}   # 壊れた settings.json でも崩れない
    provider = ai.get("provider") if ai.get("provider") in AI_PROVIDERS else "anthropic"
    return {
        "provider": provider,
        "base_url": (ai.get("base_url") or "").strip(),
        "model": (ai.get("model") or "").strip(),
        "api_key": ai.get("api_key") or "",
    }


def ai_status() -> dict:
    """APIキーを含めない安全な設定ビュー。"""
    cfg = ai_config()
    return {
        "provider": cfg["provider"],
        "base_url": cfg["base_url"],
        "model": cfg["model"],
        "has_key": bool(cfg["api_key"]),
        "providers": list(AI_PROVIDERS),
    }


def _fetch_page_urllib(u: str) -> tuple[str, str]:
    """urllib での本文取得。戻り値 (text, error)。error が空でなければ失敗/不十分。"""
    try:
        req = urllib.request.Request(u, headers={
            "User-Agent": USER_AGENT, "Accept": "text/html,*/*;q=0.8",
            "Accept-Encoding": "gzip, identity", "Accept-Language": "ja,en;q=0.8",
        })
        with _opener(block_internal=True).open(req, timeout=FETCH_TIMEOUT) as r:
            raw = r.read(MAX_FEED_BYTES + 1)
            enc = (r.headers.get("Content-Encoding") or "").lower()
        if len(raw) > MAX_FEED_BYTES:
            return "", "ページが大きすぎる"
        if enc == "gzip" or raw[:2] == b"\x1f\x8b":
            raw = _gunzip_capped(raw, MAX_FEED_BYTES)
        text = strip_html(raw.decode("utf-8", "replace"))[:MAX_PAGE_TEXT]
        if len(text) < MIN_PAGE_TEXT:
            return text, "本文テキストがほぼ空（JS描画/ブロックページの可能性）"
        return text, ""
    except Exception as e:
        return "", f"{type(e).__name__}: {str(e)[:120]}"


def _fetch_page_selenium(url: str) -> tuple[str, str]:
    """Selenium（ヘッドレスブラウザ）での本文取得。戻り値 (text, error)。

    実ブラウザはシステムのプロキシ設定（PAC/自動構成・SSO認証）をそのまま使える
    ため、urllib が社内プロキシで遮断・JS描画で空になるページの代替経路になる。
    selenium 未インストール環境では理由を返してスキップ（依存は任意のまま）。
    PRISM_BROWSER_BINARY / PRISM_CHROMEDRIVER でバイナリを明示指定できる。
    """
    try:
        from selenium import webdriver
        from selenium.webdriver.chrome.service import Service as ChromeService
        from selenium.webdriver.edge.service import Service as EdgeService
    except ImportError:
        return "", "selenium未インストール（pip install selenium で有効化できます）"
    cfg = proxy_config()
    bcfg = browser_config()   # 設定画面のパス（空なら環境変数→自動検出）
    if bcfg["driver"] and not os.path.exists(bcfg["driver"]):
        return "", f"設定のWebDriverが見つかりません: {bcfg['driver']}"
    if bcfg["binary"] and not os.path.exists(bcfg["binary"]):
        return "", f"設定のブラウザ実行ファイルが見つかりません: {bcfg['binary']}"

    def _opts(cls, with_binary: bool):
        o = cls()
        o.add_argument("--headless=new")
        o.add_argument("--disable-gpu")
        o.add_argument("--no-sandbox")
        o.add_argument("--window-size=1280,900")
        o.page_load_strategy = "eager"   # DOMContentLoaded まで（本文抽出には十分で速い）
        if with_binary and bcfg["binary"]:
            o.binary_location = bcfg["binary"]
        if not cfg["use_proxy"]:
            o.add_argument("--no-proxy-server")
        elif cfg["proxy_url"]:
            o.add_argument("--proxy-server=" + cfg["proxy_url"])
        # use_proxy=True + URL空 → ブラウザ既定（システム設定/PAC/SSO）に任せる
        return o

    drv = None
    try:
        drvpath = bcfg["driver"]
        try:
            if drvpath:
                drv = webdriver.Chrome(options=_opts(webdriver.ChromeOptions, True),
                                       service=ChromeService(executable_path=drvpath))
            else:
                drv = webdriver.Chrome(options=_opts(webdriver.ChromeOptions, True))
        except Exception as e:
            chrome_err = f"Chrome: {type(e).__name__}"
            # Edge にフォールバック。binary_location はChrome用パスの可能性が
            # あるため付けない。driver指定があれば msedgedriver として試す
            try:
                if drvpath:
                    drv = webdriver.Edge(options=_opts(webdriver.EdgeOptions, False),
                                         service=EdgeService(executable_path=drvpath))
                else:
                    drv = webdriver.Edge(options=_opts(webdriver.EdgeOptions, False))
            except Exception as e2:
                return "", f"ブラウザ起動失敗（{chrome_err} / Edge: {type(e2).__name__}）"
        drv.set_page_load_timeout(SELENIUM_TIMEOUT)
        drv.get(url)
        final = drv.current_url or url
        if _host_is_internal(final):   # SSRF: 内部へのリダイレクトは破棄
            return "", "内部アドレスへのリダイレクトのため破棄"
        # ブラウザのエラーページ/証明書警告を本文として採用しない
        # （chrome-error:// への遷移、Chromium の neterror/ssl インタースティシャルDOM）
        is_err_page = final.startswith("chrome-error://") or bool(drv.execute_script(
            "return !!(document.querySelector('#main-frame-error')"
            "||document.querySelector('#interstitial-wrapper')"
            "||(document.body&&/\\b(ssl|neterror|interstitial)\\b/.test(document.body.className))"
            "||(document.querySelector('#main-message')&&document.querySelector('#error-code')))"))
        if is_err_page:
            return "", "ブラウザがエラーページを表示（接続不可/ブロック/証明書エラー等）"
        text = (drv.execute_script(
            "return document.body ? document.body.innerText : ''") or "").strip()
        text = re.sub(r"[ \t]+\n", "\n", re.sub(r"\n{3,}", "\n\n", text))
        if len(text) < MIN_PAGE_TEXT:
            return text, "ブラウザでも本文テキストがほぼ空"
        return text[:MAX_PAGE_TEXT], ""
    except Exception as e:
        return "", f"{type(e).__name__}: {str(e)[:120]}"
    finally:
        if drv is not None:
            try:
                drv.quit()
            except Exception:
                pass


def fetch_page_text(url: str) -> dict:
    """記事ページ本文をプレーンテキストで取得する。proxy設定に従う。

    1) urllib で取得 → 2) 失敗/ほぼ空なら Selenium（ヘッドレスブラウザ）へ
    フォールダウン → 3) どちらも駄目なら text="" と失敗理由を返す
    （呼び出し側が「取れなかった」ことをUIに明示できる）。
    記事リンクはフィード提供者（=第三者）由来のため、内部アドレスへの取得は
    SSRF 対策として拒否し、リダイレクトも内部アドレスを追わない。
    戻り値: {"text": 本文, "via": "urllib"|"selenium"|"", "error": 失敗理由}
    """
    u = safe_url(url)
    if not u or _host_is_internal(u):
        return {"text": "", "via": "", "error": "URLが不正か内部アドレス"}
    text, err = _fetch_page_urllib(u)
    if text and not err:
        return {"text": text, "via": "urllib", "error": ""}
    s_text, s_err = _fetch_page_selenium(u)
    if s_text and not s_err:
        return {"text": s_text, "via": "selenium", "error": ""}
    best = s_text or text   # 断片でも無いよりまし（ただし「不十分」だったことは伝える）
    if best:
        return {"text": best, "via": ("selenium" if s_text else "urllib"),
                "error": f"本文が不完全な可能性（直接取得: {err or 'OK'} / ブラウザ: {s_err or 'OK'}）"}
    return {"text": "", "via": "",
            "error": f"直接取得: {err} / ブラウザ: {s_err}"}


def _http_json(url: str, body: dict, headers: dict, no_proxy: bool = False,
               timeout: float = AI_TIMEOUT) -> dict:
    """JSON を POST して JSON を返す（proxy・CA は urllib が処理）。

    no_proxy=True のときはプロキシを経由しない（ローカルLLM向け）。
    """
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={**headers, "Content-Type": "application/json"})
    try:
        with _opener(force_direct=no_proxy).open(req, timeout=timeout) as r:
            resp = r.read(4 * 1024 * 1024)
        return json.loads(resp.decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        detail = e.read(1500).decode("utf-8", "replace")
        raise RuntimeError(f"HTTP {e.code}: {detail[:500]}")
    except (urllib.error.URLError, socket.timeout, TimeoutError, ValueError, OSError) as e:
        raise RuntimeError(f"接続エラー: {e}")


# 推論マーカー: <think>/<thinking>（DeepSeek-R1, QwQ, Qwen3系）、[THINK]（Magistral等）、
# <|begin_of_thought|>（OpenThinker系）
_THINK_PAIR_RE = re.compile(
    r"(?:<think(?:ing)?>(.*?)</think(?:ing)?>"
    r"|\[THINK\](.*?)\[/THINK\]"
    r"|<\|begin_of_thought\|>(.*?)<\|end_of_thought\|>)\s*",
    re.DOTALL | re.IGNORECASE)
_THINK_OPEN_RE = re.compile(r"<think(?:ing)?>|\[THINK\]|<\|begin_of_thought\|>",
                            re.IGNORECASE)
_THINK_CLOSE_RE = re.compile(r"(?:</think(?:ing)?>|\[/THINK\]|<\|end_of_thought\|>)\s*",
                             re.IGNORECASE)
_TRUNCATED_NOTE = ("（モデルが思考の途中で応答を終えたため、最終解答がありません。"
                   "下の「推論過程を表示」で途中経過を確認するか、もう一度質問してください）")
# タグを使わず「*Output Generation*」「Final Answer」等の見出し行で最終解答を区切る
# モデル向けのヒューリスティック（見出しの後ろが解答、前が思考）
_SCAFFOLD_RE = re.compile(
    r"(?im)^[ \t]*(?:#{1,3}[ \t]*)?(?:\*{1,2}|_{1,2})?"
    r"(?:(?:Output Generation|Final (?:Answer|Response|Output)|最終(?:解答|回答|出力))"
    r"(?:\*{1,2}|_{1,2})?[ \t]*(?:\(.*?\))?[ \t]*[::]?"
    r"|(?:Answer|Response|回答|解答)(?:\*{1,2}|_{1,2})?[ \t]*[::])[ \t]*$")


def _split_reasoning(text: str) -> tuple[str, str]:
    """推論系ローカルLLM（DeepSeek-R1 / QwQ / Qwen3 等）が本文に混ぜて出力する
    <think>…</think> 等の推論過程を分離する。対応ケース:
    - 通常の開閉ペア（複数ブロック・本文混在も可）
    - 開きタグ無しで閉じタグだけ（テンプレートが開きタグを食う LM Studio 等）
    - 閉じタグ無しで開きタグだけ（トークン上限で思考が打ち切られた Qwen3 の長考等）
    戻り値は (最終解答, 推論過程)。解答が無い場合は案内文を解答として返し、
    推論はそのまま折りたたみ側に渡す（生の思考を解答欄に出さない）。"""
    if not text:
        return text, ""
    if not (_THINK_OPEN_RE.search(text) or _THINK_CLOSE_RE.search(text)):
        # タグ無しで見出し形式の思考を書くモデル（*Output Generation* 等）:
        # 最後の見出し行より後ろを解答、前を思考として分離する
        ms = list(_SCAFFOLD_RE.finditer(text))
        if ms:
            after = text[ms[-1].end():].strip()
            before = text[:ms[-1].start()].strip()
            if after and before:
                return after, before
        return text, ""
    chunks: list[str] = []
    def _grab(m):
        chunks.append(next(g for g in m.groups() if g is not None).strip())
        return ""
    stripped = _THINK_PAIR_RE.sub(_grab, text)
    if _THINK_CLOSE_RE.search(stripped):   # 開きタグの無い残骸: 先頭〜閉じタグ が推論
        parts = _THINK_CLOSE_RE.split(stripped, maxsplit=1)
        chunks.insert(0, parts[0].strip())
        stripped = parts[1] if len(parts) > 1 else ""
    m = _THINK_OPEN_RE.search(stripped)    # 閉じられずに終わった思考（打ち切り）
    if m:
        chunks.append(stripped[m.end():].strip())
        stripped = stripped[:m.start()]
    answer = stripped.strip()
    reasoning = "\n\n".join(c for c in chunks if c)
    if not answer:
        return (_TRUNCATED_NOTE if reasoning else text.strip()), reasoning
    return answer, reasoning


def call_ai(cfg: dict, system: str, user_content: str, history: list[dict],
            max_tokens: int | None = None) -> str:
    """provider に応じて生成AIを呼び出し、本文テキストを返す。
    max_tokens はクラウド向けの応答上限（省略時 AI_MAX_TOKENS。ローカルは常に上限なし）。"""
    provider, key, model = cfg["provider"], cfg["api_key"], cfg["model"]
    base = cfg["base_url"].rstrip("/")
    # history は [{role, content(str)}]（user/assistant のみ想定）
    msgs = [{"role": m.get("role", "user"), "content": str(m.get("content", ""))}
            for m in history if m.get("role") in ("user", "assistant")]
    msgs.append({"role": "user", "content": user_content})

    if provider == "anthropic":
        url = (base or "https://api.anthropic.com") + "/v1/messages"
        body = {"model": model or "claude-opus-4-8",
                "max_tokens": max_tokens or AI_MAX_TOKENS, "messages": msgs}
        if system:
            body["system"] = system
        data = _http_json(url, body, {
            "x-api-key": key, "anthropic-version": "2023-06-01",
        })
        parts = [b.get("text", "") for b in data.get("content", [])
                 if isinstance(b, dict) and b.get("type") == "text"]
        return "".join(parts).strip() or "（空の応答が返りました）"

    # OpenAI互換（Chat Completions）— openai / local 共通
    if provider == "local":
        url = (base or LOCAL_DEFAULT_BASE) + "/chat/completions"
        # ローカル(=内部アドレス)のみプロキシ非経由。外部URLならプロキシ設定に従う
        default_model, no_proxy = "llama3.1", _host_is_internal(base or LOCAL_DEFAULT_BASE)
    else:
        url = (base or "https://api.openai.com/v1") + "/chat/completions"
        default_model, no_proxy = "gpt-4o-mini", False
    full = ([{"role": "system", "content": system}] if system else []) + msgs
    body = {"model": model or default_model, "messages": full}
    if provider == "local":
        # 推論(思考)モデルは max_tokens=1500 だと </think> の前に打ち切られて
        # 生の思考が漏れるため、上限を課さない（サーバー既定=EOSまで）。時間も長めに
        timeout = AI_TIMEOUT_LOCAL
    else:
        body["max_tokens"] = max_tokens or AI_MAX_TOKENS
        timeout = AI_TIMEOUT
    headers = {"Authorization": "Bearer " + key} if key else {}   # ローカルはキー任意
    data = _http_json(url, body, headers, no_proxy=no_proxy, timeout=timeout)
    choices = data.get("choices") or [{}]
    msg = choices[0].get("message") or {}
    content = (msg.get("content") or "").strip()
    # 推論を別フィールドで返す実装（DeepSeek API / Ollama 等）は <think> 形式に
    # 畳んでおき、呼び出し側の _split_reasoning で本文と一元的に分離する
    rc = (msg.get("reasoning_content") or msg.get("reasoning") or "").strip()
    if rc:
        content = f"<think>{rc}</think>\n{content}"
    return content or "（空の応答が返りました）"


def ai_chat(payload: dict) -> dict:
    """UI からの1問に答える。記事/一覧のコンテキストを組み立ててAIへ。"""
    cfg = ai_config()
    # ローカルLLM はAPIキー不要。クラウドはキー必須。
    if cfg["provider"] != "local" and not cfg["api_key"]:
        return {"ok": False, "need_setup": True,
                "error": "生成AI APIが未設定です。設定からAPIキーを登録してください。"}
    question = (payload.get("question") or "").strip()
    if not question:
        return {"ok": False, "error": "質問が空です。"}
    history = payload.get("history") if isinstance(payload.get("history"), list) else []
    history = history[-8:]  # 直近のみ
    ctx = payload.get("context") if isinstance(payload.get("context"), dict) else {}

    system = ("あなたはニュース閲覧を助けるアシスタントです。以下に与えられた記事情報や"
              "本文に基づいて、日本語で簡潔かつ正確に答えてください。推測が必要な場合は"
              "その旨を明示し、与えられた情報に無い事実を断定しないでください。")
    if cfg["provider"] == "local":
        # 推論系ローカルLLM（Qwen3 等）が思考・下書き・*見出し* 付きの検討を
        # 本文に垂れ流すのを抑止し、出す場合はタグで機械可読にさせる
        system += ("\n出力規律: 思考過程・分析・下書き・途中の検討（*Analysis* や "
                   "*Output Generation* のような見出しを含む）は出力せず、最終的な"
                   "回答本文だけを書いてください。思考過程を書く必要がある場合は、"
                   "必ず <think> と </think> で囲み、その後に回答本文を書いてください。")

    parts = []
    page_note = ""   # 本文取得の状態（selenium経由/失敗）をUIへ伝える注記
    if ctx.get("kind") == "list":
        parts.append("【現在表示中の記事一覧】")
        for i, a in enumerate((ctx.get("items") or [])[:25], 1):
            parts.append(f"{i}. [{a.get('category','')}] {a.get('title','')}（{a.get('source','')}）"
                         + (f" — {a.get('summary','')}" if a.get("summary") else ""))
    elif ctx.get("kind") == "research":   # リサーチ画面: 検索でヒットした記事群
        # 検索条件（情報源×期間×キーワード）を明示して、LLMが対象範囲を踏まえて答えられるようにする
        f = ctx.get("filters") if isinstance(ctx.get("filters"), dict) else {}
        conds = _filters_conds(f)
        items = (ctx.get("items") or [])[:30]
        if conds:
            parts.append("【検索条件】" + " ／ ".join(conds))
        hit = f.get("hit")
        parts.append("【検索でヒットした記事（過去ログ）】"
                     + (f"（該当 {hit} 件のうち {len(items)} 件を選択）"
                        if isinstance(hit, int) and hit >= len(items) else ""))
        pages: dict[str, dict] = {}
        if payload.get("fulltext"):   # 取得済み（キャッシュ）の本文抜粋を添える。会話では新規取得はしない
            ids = [str(a.get("id")) for a in items if a.get("id")][:FULLTEXT_CHAT_MAX]
            pages = {k: v for k, v in pages_cached(ids).items() if v.get("text")}
        per = FULLTEXT_CHAT_LOCAL if cfg["provider"] == "local" else FULLTEXT_CHAT_CLOUD
        for i, a in enumerate(items, 1):
            d = str(a.get("published") or "")[:10]
            parts.append(f"{i}. [{a.get('category','')}] {a.get('title','')}"
                         f"（{a.get('source','')}{' ' + d if d else ''}）"
                         + (f" — {a.get('summary','')}" if a.get("summary") else ""))
            pg = pages.get(str(a.get("id")))
            if pg:
                parts.append("   本文抜粋: " + re.sub(r"\s+", " ", pg["text"])[:per])
        if payload.get("fulltext"):
            page_note = (f"本文抜粋を {len(pages)} 件に添付（取得済みの記事のみ・各{per}字まで）" if pages else
                         "⚠ 取得済みの本文がありません（左の「本文を取得」で先に取得してください）。要約のみに基づく回答です")
    else:
        parts.append("【対象の記事】")
        for f in ("title", "source", "category", "published", "summary", "link"):
            if ctx.get(f):
                parts.append(f"{f}: {ctx.get(f)}")
        # 本文取得（記事の実URLがあり、要求されていれば）
        # urllib → Selenium(ヘッドレスブラウザ) の順に試し、両方失敗なら
        # 「取れなかった」ことを注記としてLLMと画面の両方へ明示する
        if payload.get("fetch_page") and safe_url(ctx.get("link")):
            pg = fetch_page_text(ctx.get("link"))
            if pg["text"]:
                page = pg["text"]
                if cfg["provider"] == "local":
                    # 小さな文脈窓で出力が思考の途中に切れて漏れるのを防ぐ
                    page = page[:MAX_PAGE_TEXT_LOCAL]
                parts.append("\n【記事ページ本文（抜粋）】\n" + page)
                if pg["via"] == "selenium":
                    page_note = "本文はヘッドレスブラウザ（selenium）経由で取得しました"
                if pg["error"]:
                    page_note = "⚠ " + pg["error"]
            else:
                parts.append("\n【注記】記事ページ本文は取得できなかった（"
                             + pg["error"] + "）。上の要約のみに基づいて回答すること。")
                page_note = ("⚠ 記事本文を取得できませんでした（" + pg["error"]
                             + "）。要約のみに基づく回答です")
    context_block = "\n".join(str(p) for p in parts if p)
    user_content = (context_block + "\n\n" if context_block else "") + "質問: " + question
    if cfg["provider"] == "local" and context_block:
        # 長い資料の直後は指示が薄まりやすいので、末尾でも出力規律を念押しする
        user_content += ("\n\n（注意: 上の資料の分析過程・思考・下書きは出力せず、"
                         "質問への最終回答のみを日本語で書いてください）")

    try:
        answer, reasoning = _split_reasoning(call_ai(cfg, system, user_content, history))
        return {"ok": True, "answer": answer, "reasoning": reasoning,
                "page_note": page_note}
    except RuntimeError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:   # 想定外も UI に見せる
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


# ------------------------------------------------------------------ リサーチ: レポート生成（map-reduce・出典番号つき）

REPORT_MAX_ARTICLES = 300       # 1レポートの対象記事上限
REPORT_CHUNK_CHARS_LOCAL = 3500 # 部分要約1回に渡す記事テキストの文字数（ローカルLLM）
REPORT_CHUNK_CHARS_CLOUD = 12000
REPORT_PARALLEL_LOCAL = 2       # 部分要約の並列数（ローカルサーバーは直列化されることが多い）
REPORT_PARALLEL_CLOUD = 4
REPORT_MAX_TOKENS = 4000        # クラウド向けの応答上限（ローカルは上限を課さない）
REPORT_TEMPLATES = {
    "overview": ("概況レポート", [
        "概況（3〜5文で全体像）",
        "主要トピック（3〜6項目。項目ごとに小見出し＋2〜4文）",
        "時系列の流れ（日付順の箇条書き）",
        "情報源ごとの視点の違い（あれば）",
        "示唆・次に注目すべき点",
    ]),
    "timeline": ("時系列レポート", [
        "期間の概況（2〜3文）",
        "時系列（日付順。1行1出来事「YYYY-MM-DD 媒体: 内容 [n]」）",
        "転換点・変化（あれば）",
        "今後の注目点",
    ]),
    "brief": ("要点ブリーフ", [
        "要点（5項目以内。各項目1〜2文＋出典番号）",
        "ひとこと所感（1〜2文）",
    ]),
    "compare": ("比較レポート", [
        "比較の概況（2〜3文。件数や時期の違いにも触れる）",
        "〔A〕の主な動き（3〜5項目）",
        "〔B〕の主な動き（3〜5項目）",
        "共通点（両方に見られる論点）",
        "相違点・温度差（片方だけの論点、扱いの違い）",
        "示唆・次に注目すべき点",
    ]),
}
_jobs: dict[str, dict] = {}       # レポート生成ジョブ（メモリ上。完了結果は SQLite の reports に保存）
_jobs_lock = threading.Lock()
_CITE_RE = re.compile(r"\[(\d{1,3})\]")


def _filters_conds(f: dict) -> list[str]:
    """リサーチの検索条件（情報源×期間×カテゴリ×キーワード）を日本語の条件文に。"""
    conds: list[str] = []
    if not isinstance(f, dict):
        return conds
    if f.get("theme"):
        conds.append(f"テーマ「{str(f['theme'])[:40]}」")
    if isinstance(f.get("compare"), dict):
        conds.append(f"比較: 〔A〕{str(f['compare'].get('a') or '')[:40]} ／ 〔B〕{str(f['compare'].get('b') or '')[:40]}")
    if f.get("q"):
        conds.append(f"キーワード「{str(f['q'])[:100]}」")
    srcn = ([str(x)[:40] for x in f.get("sources") if x][:20]
            if isinstance(f.get("sources"), list) else [])
    if srcn:
        conds.append("情報源: " + "・".join(srcn))
    if f.get("category"):
        conds.append(f"カテゴリ: {str(f['category'])[:20]}")
    if f.get("from") or f.get("to"):
        conds.append(f"期間: {str(f.get('from') or '')[:10]}〜{str(f.get('to') or '')[:10]}")
    elif f.get("days"):
        try:
            conds.append(f"期間: 直近{int(f['days'])}日")
        except (TypeError, ValueError):
            pass
    return conds


def _report_fetch(ids: list) -> list[dict]:
    """過去ログから id で記事を引く（新着順・上限 REPORT_MAX_ARTICLES）。"""
    ids = [i for i in ids if isinstance(i, str) and i][:REPORT_MAX_ARTICLES]
    if not ids:
        return []
    found: dict[str, dict] = {}
    with _archive_lock:
        _archive_init_locked()
        conn = _db()
        try:
            for i in range(0, len(ids), 400):
                part = ids[i:i + 400]
                for r in conn.execute("SELECT * FROM articles WHERE id IN (%s)"
                                      % ",".join("?" * len(part)), part):
                    found[r["id"]] = dict(r)
        finally:
            conn.close()
    arts = list(found.values())
    arts.sort(key=lambda a: a.get("sort_ts") or 0.0, reverse=True)
    return arts


def _article_line(n: int, a: dict, text: str | None = None, per: int = 0, tag: str = "") -> str:
    d = str(a.get("published") or "")[:10]
    s = f"[{n}] {tag}{d} {a.get('source') or ''}｜{a.get('title') or ''}"
    if a.get("summary"):
        s += "\n   " + str(a["summary"])[:300]
    if text and per > 0:   # 本文一括取得で得たページ本文（抜粋）
        s += "\n   本文抜粋: " + re.sub(r"\s+", " ", text)[:per]
    return s


def _chunk_lines(lines: list[str], budget: int) -> list[list[str]]:
    """文字数の予算で行を貪欲にまとめる（1行が予算超でも単独で1チャンク）。"""
    chunks: list[list[str]] = []
    cur: list[str] = []
    size = 0
    for ln in lines:
        if cur and size + len(ln) > budget:
            chunks.append(cur)
            cur, size = [], 0
        cur.append(ln)
        size += len(ln) + 1
    if cur:
        chunks.append(cur)
    return chunks


def _report_system(local: bool) -> str:
    s = ("あなたは産業ニュースの調査アナリストです。与えられた記事情報のみに基づき、"
         "日本語で正確・簡潔に書いてください。事実や数値には必ず出典番号 [n]（与えられた"
         "番号のみ）を付け、記事に無い事実・推測・一般論を加えないでください。")
    if local:
        s += ("\n出力規律: 思考過程・下書き・途中の検討は出力せず、求められた本文だけを"
              "書いてください。思考を書く必要がある場合は必ず <think> と </think> で囲んでください。")
    return s


def _groups_note(groups: list[dict] | None) -> str:
    """比較レポート用: 記事行の〔A〕〔B〕タグの意味。"""
    if not groups:
        return ""
    return ("【群の定義】" + " ／ ".join(f"〔{chr(65 + i)}〕= {str(g.get('label') or '')[:60]}" for i, g in enumerate(groups[:3]))
            + "（各記事行の先頭タグがどの群かを示す。項目にはどの群の話かを明記する）\n")


def _map_prompt(question: str, chunk_text: str, fulltext: bool = False, note: str = "") -> str:
    kind = "見出し／要約／本文抜粋" if fulltext else "見出し／要約"
    return (f"【問い】{question}\n{note}\n【記事（[番号] 日付 媒体｜{kind}）】\n{chunk_text}\n\n"
            "【指示】上の記事から、問いに関係する事実・数値・論点を箇条書きで抽出してください。\n"
            "- 各項目は「YYYY-MM-DD 媒体: 内容 [n]」の形で、末尾に必ず出典番号を付ける（複数可 [1][3]）\n"
            "- 関係の薄い記事は省いてよい。推測・一般論・感想は書かない\n"
            "- 最大12項目。箇条書きのみを出力する")


def _merge_prompt(question: str, notes: str) -> str:
    return (f"【問い】{question}\n\n【部分メモ】\n{notes}\n\n"
            "【指示】部分メモの重複を統合し、問いに関係する事実を日付順の箇条書きにまとめ直してください。"
            "各項目の末尾の出典番号 [n] は必ず残す（統合した項目は番号を併記）。新しい事実を加えない。"
            "最大20項目。箇条書きのみを出力する")


def _reduce_prompt(question: str, tname: str, sections: list[str], notes: str,
                   n_articles: int, filters: dict, note: str = "") -> str:
    conds = _filters_conds(filters)
    heads = "\n".join(f"{i}. ## {s}" for i, s in enumerate(sections, 1))
    return (f"【問い】{question}\n" + note
            + (f"【検索条件】{' ／ '.join(conds)}\n" if conds else "")
            + f"【対象】記事 {n_articles} 件（出典番号 [1]〜[{n_articles}]）\n\n"
            f"【部分メモ】\n{notes}\n\n"
            f"【指示】部分メモを統合し、Markdown で「{tname}」を書いてください。構成は次の見出し（## で始める）を順に:\n{heads}\n"
            "- 各文・各箇条書きの末尾に根拠の出典番号 [n] を付ける（与えられた番号のみ使う）\n"
            "- 重複は統合し、矛盾があれば両論を併記する。新しい事実を加えない\n"
            "- 日付は YYYY-MM-DD で書く。「# 」のタイトル行は不要（こちらで付けます）")


def _strip_bad_cites(text: str, n: int) -> tuple[str, int]:
    """範囲外の出典番号 [k] を取り除く。戻り値 (text, 除去数)。"""
    bad = 0
    def _sub(m):
        nonlocal bad
        k = int(m.group(1))
        if 1 <= k <= n:
            return m.group(0)
        bad += 1
        return ""
    return _CITE_RE.sub(_sub, text), bad


def _report_title(question: str, filters: dict, tname: str) -> str:
    conds = _filters_conds(filters)
    core = question.strip()
    if not core or core == _default_question(filters):
        core = " × ".join(c.split(": ", 1)[-1].replace("キーワード", "").strip("「」")
                          for c in conds) or "過去ログ"
    return f"{tname}: {core[:60]}"


def _default_question(filters: dict) -> str:
    conds = _filters_conds(filters)
    return ("選択した記事群（" + " ／ ".join(conds) + "）の動向を整理する") if conds \
        else "選択した記事群の動向を整理する"


def _save_report(rep: dict) -> None:
    with _archive_lock:
        _archive_init_locked()
        conn = _db()
        try:
            conn.execute("INSERT OR REPLACE INTO reports VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                         (rep["id"], rep["created_at"], rep["title"], rep["question"], rep["template"],
                          json.dumps(rep["filters"], ensure_ascii=False),
                          json.dumps(rep["article_ids"], ensure_ascii=False),
                          rep["markdown"], json.dumps(rep["sources"], ensure_ascii=False),
                          rep["model"], json.dumps(rep["stats"], ensure_ascii=False)))
            conn.commit()
        finally:
            conn.close()


def _row_to_report(r) -> dict:
    return {"id": r["id"], "created_at": r["created_at"], "title": r["title"],
            "question": r["question"], "template": r["template"],
            "filters": json.loads(r["filters_json"] or "{}"),
            "article_ids": json.loads(r["article_ids_json"] or "[]"),
            "markdown": r["markdown"], "sources": json.loads(r["sources_json"] or "[]"),
            "model": r["model"], "stats": json.loads(r["stats_json"] or "{}")}


def list_reports(limit: int = 100) -> list[dict]:
    with _archive_lock:
        _archive_init_locked()
        conn = _db()
        try:
            rows = conn.execute("SELECT id, created_at, title, template, article_ids_json FROM reports "
                                "ORDER BY created_at DESC LIMIT ?", (max(1, min(int(limit), 500)),))
            return [{"id": r["id"], "created_at": r["created_at"], "title": r["title"],
                     "template": r["template"],
                     "articles": len(json.loads(r["article_ids_json"] or "[]"))} for r in rows]
        finally:
            conn.close()


def get_report(rid: str) -> dict | None:
    with _archive_lock:
        _archive_init_locked()
        conn = _db()
        try:
            r = conn.execute("SELECT * FROM reports WHERE id = ?", (rid,)).fetchone()
            return _row_to_report(r) if r else None
        finally:
            conn.close()


def delete_report(rid: str) -> bool:
    with _archive_lock:
        _archive_init_locked()
        conn = _db()
        try:
            cur = conn.execute("DELETE FROM reports WHERE id = ?", (rid,))
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()


def _build_report(cfg: dict, question: str, template: str, ids: list, filters: dict, fulltext: bool = False,
                  groups: list[dict] | None = None, progress=None, title: str | None = None) -> dict:
    """レポートを生成して dict で返す（保存はしない）。(本文一括取得) → 部分要約(map) → 統合(reduce)。
    progress(**kw) で進捗（state / total / done / sub / articles）を通知する。
    groups（比較レポート）があれば記事行に〔A〕〔B〕のタグを付け、群の定義をプロンプトに添える。"""
    def upd(**kw):
        if progress:
            progress(**kw)
    tag_of: dict[str, str] = {}
    for gi, g in enumerate((groups or [])[:3]):
        for i in (g.get("ids") or []):
            tag_of.setdefault(str(i), f"〔{chr(65 + gi)}〕")
    note = _groups_note(groups)
    arts = _report_fetch(ids)
    if not arts:
        raise RuntimeError("対象記事が過去ログに見つかりません")
    local = cfg["provider"] == "local"
    budget = REPORT_CHUNK_CHARS_LOCAL if local else REPORT_CHUNK_CHARS_CLOUD
    par = REPORT_PARALLEL_LOCAL if local else REPORT_PARALLEL_CLOUD
    system = _report_system(local)
    pages: dict[str, dict] = {}
    n_target = 0
    if fulltext:   # 本文一括取得（キャッシュ優先・urllib→ブラウザ）。新しい記事から上限件数まで
        targets = arts[:FULLTEXT_MAX_ARTICLES]
        n_target = len(targets)
        upd(state="fetching", total=n_target, done=0, articles=len(arts), sub="")
        pages = fetch_pages(targets, progress=lambda d, t, sub: upd(total=t, done=d, sub=sub))
    per = FULLTEXT_PER_ARTICLE_LOCAL if local else FULLTEXT_PER_ARTICLE_CLOUD
    lines = [_article_line(i + 1, a, (pages.get(a["id"]) or {}).get("text"), per, tag_of.get(a["id"], ""))
             for i, a in enumerate(arts)]
    chunks = _chunk_lines(lines, budget)
    upd(state="mapping", total=len(chunks), done=0, articles=len(arts), sub="")
    done = [0]
    lock = threading.Lock()

    def do_map(ch: list[str]) -> str:
        txt = call_ai(cfg, system, _map_prompt(question, "\n".join(ch), bool(pages), note), [],
                      max_tokens=REPORT_MAX_TOKENS)
        ans, _ = _split_reasoning(txt)
        with lock:
            done[0] += 1
            upd(done=done[0])
        return ans

    with ThreadPoolExecutor(max_workers=max(1, min(par, len(chunks)))) as ex:
        notes = [n for n in ex.map(do_map, chunks) if n]
    upd(state="reducing")
    notes_text = "\n\n".join(notes)
    if len(notes) > 1 and len(notes_text) > budget * 2:   # 多段統合（メモが長すぎる場合）
        mids = []
        for g in _chunk_lines(notes_text.split("\n"), budget * 2):
            t = call_ai(cfg, system, _merge_prompt(question, "\n".join(g)), [],
                        max_tokens=REPORT_MAX_TOKENS)
            mids.append(_split_reasoning(t)[0])
        notes_text = "\n\n".join(m for m in mids if m)
    tname, sections = REPORT_TEMPLATES.get(template) or REPORT_TEMPLATES["overview"]
    body = call_ai(cfg, system, _reduce_prompt(question, tname, sections, notes_text, len(arts), filters, note),
                   [], max_tokens=REPORT_MAX_TOKENS)
    body, _ = _split_reasoning(body)
    body = re.sub(r"^\s*#\s[^\n]*\n", "", body, count=1)   # 念のため先頭のタイトル行を除去
    body, bad = _strip_bad_cites(body, len(arts))
    n_text = sum(1 for p in pages.values() if p.get("text"))
    return {
        "id": secrets.token_hex(8), "created_at": time.time(),
        "title": (title or "").strip()[:80] or _report_title(question, filters, tname),
        "question": question, "template": template if template in REPORT_TEMPLATES else "overview",
        "filters": filters, "article_ids": [a["id"] for a in arts],
        "markdown": body.strip(),
        "sources": [{"n": i + 1, "id": a["id"], "title": a.get("title"), "source": a.get("source"),
                     "published": str(a.get("published") or "")[:10], "link": a.get("link")}
                    for i, a in enumerate(arts)],
        "model": f"{cfg['provider']}:{cfg['model'] or 'default'}",
        "stats": {"articles": len(arts), "chunks": len(chunks), "bad_cites": bad,
                  "fulltext": n_text, "fulltext_failed": max(0, n_target - n_text)},
    }


def _run_report(job_id: str, question: str, template: str, ids: list, filters: dict,
                fulltext: bool = False, theme_id: str | None = None, title: str | None = None,
                groups: list[dict] | None = None) -> None:
    """ワーカースレッド: _build_report → 保存（レポート id = ジョブ id）。進捗は _jobs に書く。"""
    def upd(**kw):
        with _jobs_lock:
            _jobs[job_id].update(kw)
    try:
        cfg = ai_config()
        if cfg["provider"] != "local" and not cfg["api_key"]:
            raise RuntimeError("生成AI APIが未設定です")
        rep = _build_report(cfg, question, template, ids, filters, fulltext, groups, upd, title)
        rep["id"] = job_id
        _save_report(rep)
        if theme_id:
            _theme_set_brief(theme_id, rep["id"], rep["created_at"])
        upd(state="done", report_id=rep["id"], title=rep["title"])
    except Exception as e:
        upd(state="error", error=f"{type(e).__name__}: {str(e)[:200]}")


def _new_job(kind: str, **fields) -> str:
    """ジョブ登録（レポート生成・本文一括取得で共用）。古いジョブは完了・失敗から1時間で掃除。"""
    job_id = secrets.token_hex(8)
    with _jobs_lock:
        now = time.time()
        for k in [k for k, j in _jobs.items()
                  if j.get("state") in ("done", "error") and now - j.get("created", now) > 3600]:
            _jobs.pop(k, None)
        _jobs[job_id] = {"id": job_id, "kind": kind, "state": "queued", "total": 0, "done": 0,
                         "created": now, **fields}
    return job_id


def start_report_job(body: dict) -> dict:
    ids = body.get("ids") if isinstance(body.get("ids"), list) else []
    ids = [str(i) for i in ids if i]
    groups: list[dict] = []
    for g in (body.get("groups") if isinstance(body.get("groups"), list) else [])[:3]:   # 比較レポート: 群ごとの記事
        if isinstance(g, dict) and isinstance(g.get("ids"), list):
            gids = [str(i) for i in g["ids"] if i][:REPORT_MAX_ARTICLES]
            groups.append({"label": str(g.get("label") or "")[:60], "ids": gids})
            ids.extend(i for i in gids if i not in ids)
    ids = list(dict.fromkeys(ids))[:REPORT_MAX_ARTICLES]
    if not ids:
        return {"ok": False, "error": "記事を1件以上選択してください"}
    filters = body.get("filters") if isinstance(body.get("filters"), dict) else {}
    question = (str(body.get("question") or "")).strip()[:300] or _default_question(filters)
    template = body.get("template") if body.get("template") in REPORT_TEMPLATES else ("compare" if groups else "overview")
    fulltext = bool(body.get("fulltext"))
    theme_id = body.get("theme_id") if isinstance(body.get("theme_id"), str) and _ID_RE.fullmatch(body["theme_id"]) else None
    title = str(body.get("title") or "")[:80]
    job_id = _new_job("report", articles=len(ids), fulltext=fulltext)
    threading.Thread(target=_run_report, args=(job_id, question, template, ids, filters, fulltext, theme_id, title, groups or None),
                     daemon=True).start()
    return {"ok": True, "job_id": job_id, "articles": len(ids)}


def report_job_status(job_id: str) -> dict:
    with _jobs_lock:
        j = _jobs.get(job_id)
        return {"ok": True, **j} if j else {"ok": False, "error": "unknown job"}


# ------------------------------------------------------------------ リサーチ: テーマ（保存した検索条件）・新着差分・本文一括取得

THEME_MAX = 100                    # 保存できるテーマ数
FULLTEXT_MAX_ARTICLES = 80         # 1回の本文一括取得の上限（ページ取得は遅いので新しい記事から）
FULLTEXT_PARALLEL = 4              # 直接取得（urllib）の並列数
FULLTEXT_SELENIUM_MAX = 12         # 直接取得に失敗した記事のうちヘッドレスブラウザで再試行する上限
FULLTEXT_RETRY_AFTER = 86400       # 失敗キャッシュを再試行するまでの秒数
FULLTEXT_PER_ARTICLE_LOCAL = 1200  # レポートの部分要約に含める本文抜粋の文字数（ローカルLLM）
FULLTEXT_PER_ARTICLE_CLOUD = 2500
FULLTEXT_CHAT_LOCAL = 700          # 会話（リサーチ）で記事ごとに添える本文抜粋の文字数
FULLTEXT_CHAT_CLOUD = 1500
FULLTEXT_CHAT_MAX = 10             # 会話で本文を添える記事数の上限
_selenium_lock = threading.Lock()  # ヘッドレスブラウザは同時に1つだけ起動する
_ID_RE = re.compile(r"[0-9a-f]{16}")


# ---- 本文キャッシュ（pages）と一括取得

def _pages_get(conn, ids: list[str]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for i in range(0, len(ids), 400):
        part = ids[i:i + 400]
        for r in conn.execute("SELECT * FROM pages WHERE article_id IN (%s)"
                              % ",".join("?" * len(part)), part):
            out[r["article_id"]] = dict(r)
    return out


def pages_cached(ids: list[str]) -> dict[str, dict]:
    """記事 id → キャッシュ済み本文（成功・失敗とも）。"""
    ids = [i for i in ids if isinstance(i, str) and i]
    if not ids:
        return {}
    with _archive_lock:
        _archive_init_locked()
        conn = _db()
        try:
            return _pages_get(conn, ids)
        finally:
            conn.close()


def _pages_put(rows: list[tuple]) -> None:
    if not rows:
        return
    with _archive_lock:
        _archive_init_locked()
        conn = _db()
        try:
            conn.executemany("INSERT OR REPLACE INTO pages VALUES (?,?,?,?,?,?,?)", rows)
            conn.commit()
        finally:
            conn.close()


def fetch_pages(arts: list[dict], progress=None) -> dict[str, dict]:
    """記事群の本文を一括取得する（キャッシュ優先）。

    1) キャッシュ（本文あり、または失敗から FULLTEXT_RETRY_AFTER 秒以内）はそのまま使う
    2) 残りを urllib で並列に直接取得
    3) それでも取れないものは FULLTEXT_SELENIUM_MAX 件までヘッドレスブラウザで直列に再試行
       （selenium 未導入・起動不可なら最初の1件で打ち切る）
    結果は pages にキャッシュ。progress(done, total, 段階の説明) で進捗を通知する。
    戻り値 {article_id: {"text", "via", "error", "cached"}}（本文が無い記事も error つきで含む）。
    記事リンクは第三者由来のため、内部アドレスは SSRF 対策として取得しない。"""
    arts = [a for a in arts if a.get("id")][:FULLTEXT_MAX_ARTICLES]
    if not arts:
        return {}
    now = time.time()
    cached = pages_cached([a["id"] for a in arts])
    out: dict[str, dict] = {}
    todo: list[dict] = []
    for a in arts:
        c = cached.get(a["id"])
        if c and (c.get("text") or now - (c.get("fetched_at") or 0) < FULLTEXT_RETRY_AFTER):
            out[a["id"]] = {"text": c.get("text") or "", "via": c.get("via") or "",
                            "error": c.get("error") or "", "cached": True}
        else:
            todo.append(a)
    total = len(todo)
    state = {"done": 0}

    def tick(sub: str) -> None:
        if progress:
            try:
                progress(state["done"], total, sub)
            except Exception:
                pass

    tick("キャッシュ確認")
    if not todo:
        return out

    def direct(a: dict) -> tuple[dict, dict]:
        u = safe_url(a.get("link"))
        if not u or _host_is_internal(u):
            return a, {"text": "", "via": "", "error": "URLが不正か内部アドレス", "final": True}
        text, err = _fetch_page_urllib(u)
        if text and not err:
            return a, {"text": text, "via": "urllib", "error": "", "final": True}
        return a, {"text": text, "via": "urllib" if text else "",
                   "error": err or "本文が取れませんでした", "final": False}

    pending: list[tuple[dict, dict]] = []
    with ThreadPoolExecutor(max_workers=max(1, min(FULLTEXT_PARALLEL, len(todo)))) as ex:
        for a, res in ex.map(direct, todo):
            if res["final"]:
                out[a["id"]] = {"text": res["text"], "via": res["via"], "error": res["error"], "cached": False}
                state["done"] += 1
                tick("直接取得")
            else:
                pending.append((a, res))
    # 直接取得できなかったものをヘッドレスブラウザで再試行（上限あり・直列）
    retry = pending[:FULLTEXT_SELENIUM_MAX]
    skipped = pending[FULLTEXT_SELENIUM_MAX:]
    browser_dead = ""
    for k, (a, res) in enumerate(retry, 1):
        if browser_dead:
            skipped.append((a, res))
            continue
        tick(f"ブラウザで再試行 {k}/{len(retry)}")
        with _selenium_lock:
            s_text, s_err = _fetch_page_selenium(safe_url(a.get("link")) or "")
        if s_text and not s_err:
            res = {"text": s_text, "via": "selenium", "error": ""}
        else:
            if s_err.startswith(("selenium未インストール", "ブラウザ起動失敗", "設定の")):
                browser_dead = s_err
            best = s_text or res["text"]
            res = {"text": best, "via": "selenium" if s_text else res["via"],
                   "error": (f"本文が不完全な可能性（直接取得: {res['error']} / ブラウザ: {s_err or 'OK'}）"
                             if best else f"直接取得: {res['error']} / ブラウザ: {s_err}")}
        out[a["id"]] = {**res, "cached": False}
        state["done"] += 1
    for a, res in skipped:   # ブラウザ再試行の上限超過・ブラウザ不可
        note = browser_dead or f"ブラウザ再試行は1回あたり{FULLTEXT_SELENIUM_MAX}件まで"
        out[a["id"]] = {"text": res["text"], "via": res["via"],
                        "error": f"{res['error']}（{note}）", "cached": False}
        state["done"] += 1
    tick("完了")
    _pages_put([(a["id"], a.get("link"), out[a["id"]]["text"][:MAX_PAGE_TEXT], out[a["id"]]["via"],
                 out[a["id"]]["error"][:300], now, len(out[a["id"]]["text"][:MAX_PAGE_TEXT]))
                for a in todo if a["id"] in out])
    return out


def _run_fulltext(job_id: str, ids: list[str]) -> None:
    """ワーカースレッド: 本文一括取得のみ（結果はキャッシュへ。UI には件数と記事ごとの可否を返す）。"""
    def upd(**kw):
        with _jobs_lock:
            _jobs[job_id].update(kw)
    try:
        arts = _report_fetch(ids)[:FULLTEXT_MAX_ARTICLES]
        if not arts:
            raise RuntimeError("対象記事が過去ログに見つかりません")
        upd(state="fetching", total=len(arts), done=0, sub="")
        res = fetch_pages(arts, progress=lambda d, t, sub: upd(total=t, done=d, sub=sub))
        summary = {"ok": sum(1 for r in res.values() if r["text"] and not r["error"]),
                   "partial": sum(1 for r in res.values() if r["text"] and r["error"]),
                   "failed": sum(1 for r in res.values() if not r["text"]),
                   "cached": sum(1 for r in res.values() if r["cached"]),
                   "selenium": sum(1 for r in res.values() if r["via"] == "selenium")}
        upd(state="done", summary=summary,
            results={k: {"ok": bool(v["text"]), "via": v["via"], "chars": len(v["text"]),
                         "error": (v["error"] or "")[:160]} for k, v in res.items()})
    except Exception as e:
        upd(state="error", error=f"{type(e).__name__}: {str(e)[:200]}")


def start_fulltext_job(body: dict) -> dict:
    ids = body.get("ids") if isinstance(body.get("ids"), list) else []
    ids = [str(i) for i in ids if i][:FULLTEXT_MAX_ARTICLES]
    if not ids:
        return {"ok": False, "error": "記事を1件以上選択してください"}
    job_id = _new_job("fulltext", articles=len(ids))
    threading.Thread(target=_run_fulltext, args=(job_id, ids), daemon=True).start()
    return {"ok": True, "job_id": job_id, "articles": len(ids)}


# ---- テーマ（保存した検索条件）・新着差分・ブリーフ

def _clean_filters(f) -> dict:
    """UI から来た検索条件を保存用に正規化する（情報源は id・不正値は捨てる）。"""
    f = f if isinstance(f, dict) else {}
    srcs = f.get("sources") if isinstance(f.get("sources"), list) else []
    try:
        days = max(0, min(int(f.get("days") or 0), 3650))
    except (TypeError, ValueError):
        days = 0
    fr, to = f.get("from"), f.get("to")
    return {"q": str(f.get("q") or "").strip()[:100],
            "sources": [s.strip()[:60] for s in srcs if isinstance(s, str) and s.strip()][:50],
            "category": f.get("category") if f.get("category") in CATEGORIES else "",
            "days": days,
            "from": fr.strip()[:10] if isinstance(fr, str) and _parse_day(fr) is not None else "",
            "to": to.strip()[:10] if isinstance(to, str) and _parse_day(to) is not None else ""}


def _source_names_conn(conn, ids: list[str]) -> list[str]:
    ids = [i for i in ids if isinstance(i, str) and i][:50]
    if not ids:
        return []
    names = {r[0]: r[1] for r in conn.execute(
        "SELECT source_id, MAX(source) FROM articles WHERE source_id IN (%s) GROUP BY source_id"
        % ",".join("?" * len(ids)), ids)}
    return [names.get(i) or i for i in ids]


def _source_names(ids: list[str]) -> list[str]:
    """source_id → 表示名（過去ログに記録された名前。無ければ id のまま）。"""
    with _archive_lock:
        _archive_init_locked()
        conn = _db()
        try:
            return _source_names_conn(conn, ids)
        finally:
            conn.close()


def _theme_row(conn, r, syn: dict | None = None) -> dict:
    f = json.loads(r["filters_json"] or "{}")
    t = {"id": r["id"], "name": r["name"], "filters": f,
         "created_at": r["created_at"], "updated_at": r["updated_at"],
         "last_seen_at": r["last_seen_at"], "last_brief_at": r["last_brief_at"],
         "last_brief_id": r["last_brief_id"],
         "kind": (r["kind"] if "kind" in r.keys() else None) or "theme"}
    t["conds"] = _filters_conds({**f, "sources": _source_names_conn(conn, f.get("sources") or [])})
    since, until = _filters_window(f)
    parsed = parse_query(f.get("q", ""), syn)
    where, params, _ = _search_where(f.get("q", ""), f.get("sources"), since, until,
                                     f.get("category") or None, None, parsed)
    t["total"] = conn.execute("SELECT COUNT(*) " + _SEARCH_FROM + where, params).fetchone()[0]
    where, params, _ = _search_where(f.get("q", ""), f.get("sources"), since, until,
                                     f.get("category") or None, t["last_seen_at"] or 0.0, parsed)
    t["new"] = conn.execute("SELECT COUNT(*) " + _SEARCH_FROM + where, params).fetchone()[0]
    return t


def list_themes() -> list[dict]:
    """テーマ一覧。new = 前回「既読」以降に過去ログへ入った該当記事の件数（archived_at 基準）。"""
    syn = _synonym_map()
    with _archive_lock:
        _archive_init_locked()
        conn = _db()
        try:
            return [_theme_row(conn, r, syn) for r in conn.execute("SELECT * FROM themes ORDER BY updated_at DESC")]
        finally:
            conn.close()


def get_theme(tid: str) -> dict | None:
    if not (isinstance(tid, str) and _ID_RE.fullmatch(tid)):
        return None
    with _archive_lock:
        _archive_init_locked()
        conn = _db()
        try:
            r = conn.execute("SELECT * FROM themes WHERE id = ?", (tid,)).fetchone()
            return _theme_row(conn, r) if r else None
        finally:
            conn.close()


def save_theme(body: dict) -> dict:
    """現在の検索条件をテーマとして保存（id 指定があれば名前・条件を上書き）。"""
    name = str(body.get("name") or "").strip()[:60]
    if not name:
        return {"ok": False, "error": "テーマ名を入力してください"}
    filters = _clean_filters(body.get("filters"))
    if not (filters["q"] or filters["sources"] or filters["category"]):
        return {"ok": False, "error": "キーワード・情報源・カテゴリのいずれかを指定してください"}
    tid = body.get("id") if isinstance(body.get("id"), str) and _ID_RE.fullmatch(body["id"]) else None
    kind = "watch" if body.get("kind") == "watch" else ("theme" if body.get("kind") == "theme" else None)
    now = time.time()
    with _archive_lock:
        _archive_init_locked()
        conn = _db()
        try:
            if tid and conn.execute("SELECT 1 FROM themes WHERE id = ?", (tid,)).fetchone():
                conn.execute("UPDATE themes SET name = ?, filters_json = ?, updated_at = ? WHERE id = ?",
                             (name, json.dumps(filters, ensure_ascii=False), now, tid))
                if kind:
                    conn.execute("UPDATE themes SET kind = ? WHERE id = ?", (kind, tid))
            else:
                if conn.execute("SELECT COUNT(*) FROM themes").fetchone()[0] >= THEME_MAX:
                    return {"ok": False, "error": f"テーマは最大 {THEME_MAX} 件までです"}
                tid = secrets.token_hex(8)
                conn.execute("INSERT INTO themes(id, name, filters_json, created_at, updated_at, last_seen_at, "
                             "last_brief_at, last_brief_id, kind) VALUES (?,?,?,?,?,?,?,?,?)",
                             (tid, name, json.dumps(filters, ensure_ascii=False), now, now, now, None, None,
                              kind or "theme"))
            conn.commit()
            t = _theme_row(conn, conn.execute("SELECT * FROM themes WHERE id = ?", (tid,)).fetchone())
        finally:
            conn.close()
    return {"ok": True, "theme": t}


def theme_seen(tid: str) -> dict:
    """「既読にする」: 新着差分の基準時刻を今に更新。"""
    if not (isinstance(tid, str) and _ID_RE.fullmatch(tid)):
        return {"ok": False, "error": "unknown theme"}
    with _archive_lock:
        _archive_init_locked()
        conn = _db()
        try:
            cur = conn.execute("UPDATE themes SET last_seen_at = ? WHERE id = ?", (time.time(), tid))
            conn.commit()
            if cur.rowcount <= 0:
                return {"ok": False, "error": "unknown theme"}
            t = _theme_row(conn, conn.execute("SELECT * FROM themes WHERE id = ?", (tid,)).fetchone())
        finally:
            conn.close()
    return {"ok": True, "theme": t}


def delete_theme(tid: str) -> bool:
    if not (isinstance(tid, str) and _ID_RE.fullmatch(tid)):
        return False
    with _archive_lock:
        _archive_init_locked()
        conn = _db()
        try:
            cur = conn.execute("DELETE FROM themes WHERE id = ?", (tid,))
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()


def _theme_set_brief(tid: str, report_id: str, at: float) -> None:
    with _archive_lock:
        _archive_init_locked()
        conn = _db()
        try:
            conn.execute("UPDATE themes SET last_brief_at = ?, last_brief_id = ? WHERE id = ?", (at, report_id, tid))
            conn.commit()
        finally:
            conn.close()


def start_theme_brief(body: dict) -> dict:
    """テーマの「ブリーフ」: 直近N日（既定7日＝週次）または前回ブリーフ以降に入った該当記事で
    レポート生成ジョブ（既定は要点ブリーフ）を開始する。完了時にテーマへ last_brief を記録。"""
    t = get_theme(str(body.get("id") or ""))
    if not t:
        return {"ok": False, "error": "unknown theme"}
    f = t["filters"]
    try:
        days = max(1, min(int(body.get("days") or 7), 365))
    except (TypeError, ValueError):
        days = 7
    now = time.time()
    use_last = body.get("since") == "last" and bool(t.get("last_brief_at"))
    since = float(t["last_brief_at"]) if use_last else now - days * 86400
    d0 = datetime.fromtimestamp(since, timezone.utc).astimezone().strftime("%Y-%m-%d")
    d1 = datetime.fromtimestamp(now, timezone.utc).astimezone().strftime("%Y-%m-%d")
    period = f"前回ブリーフ（{d0}）以降" if use_last else f"直近{days}日"
    # 「前回以降」は保存日時（archived_at）基準＝前回のあと過去ログに入った記事。「直近N日」は公開日時基準
    arts = archive_search(f.get("q", ""), REPORT_MAX_ARTICLES, f.get("sources"),
                          None if use_last else since, None, f.get("category") or None,
                          since if use_last else None)
    if not arts:
        return {"ok": False, "error": f"{period}に該当する記事がありません"}
    template = body.get("template") if body.get("template") in REPORT_TEMPLATES else "brief"
    filters = {"theme": t["name"], "q": f.get("q", ""), "sources": _source_names(f.get("sources") or []),
               "category": f.get("category", ""), "hit": len(arts),
               **({"from": d0, "to": d1, "days": 0} if use_last else {"days": days, "from": "", "to": ""})}
    question = f"テーマ「{t['name']}」の{period}の動き（新しい事実・変化・注目点）を整理する"
    r = start_report_job({"ids": [a["id"] for a in arts], "question": question, "template": template,
                          "filters": filters, "fulltext": bool(body.get("fulltext")), "theme_id": t["id"],
                          "title": f"ブリーフ: {t['name']}（{period}）"})
    if r.get("ok"):
        r.update(articles=len(arts), period=period)
    return r


# ------------------------------------------------------------------ リサーチ: 企業・製品ウォッチ（固有名詞の候補抽出・週別推移）

ENTITY_MAX = 40                     # 候補の上限
ENTITY_AI_TITLES = 80               # AI 抽出に渡す見出しの上限
_ENT_KATAKANA_RE = re.compile(r"[A-Z]{2,6}[ァ-ヴー]{2,}|[ァ-ヴー]{3,}")                  # JFEスチール / トヨタ
_ENT_ASCII_RE = re.compile(r"(?<![A-Za-z0-9])(?:[A-Z][A-Za-z0-9&.\-]+(?: (?:[A-Z][A-Za-z0-9&.\-]+|of|and|de))*|[A-Z]{2,6}[0-9]{0,3})(?![A-Za-z0-9])")
_ENT_ORG_RE = re.compile(r"[一-龥々〆ァ-ヴーA-Za-z0-9]{2,12}(?:製鉄所|製鉄|製鋼|製作所|重工業|重工|電機|電工|電子|機械|産業|技研|自動車|工業|商事|物産|"
                         r"化学|化成|金属|製薬|食品|電力|ガス|銀行|証券|保険|不動産|建設|運輸|航空|鉄道|通信|精機|精工|"
                         r"エンジニアリング|スチール|マテリアルズ|テクノロジーズ|ソリューションズ|システムズ|ホールディングス|HD|グループ|大学|研究所|機構)")
_ENT_STOP = set("ニュース サービス システム プロジェクト エネルギー ビジネス デジタル メーカー ユーザー データ ソリューション リリース "
                "インタビュー ランキング コラム シリーズ レポート プラットフォーム テクノロジー マーケット リスク コスト シェア トップ "
                "ポイント スタート セミナー イベント オンライン グローバル サプライチェーン カーボンニュートラル リサイクル グリーン "
                "ベース レベル モデル タイプ ケース プラン ビジョン ニーズ トレンド シフト ブーム ショック ステンレス アルミ アルミニウム バッテリー "
                "電気自動車 燃料電池自動車 商用車 乗用車 鉄鋼業 製造業 自動車業界 電力会社 ガス会社 大手銀行 地方銀行".split())
_ENT_STOP_ASCII = set("RSS PR NEW TOP NEWS THE AND FOR WITH FROM JP CO LTD INC CEO CTO CFO Q A IT EV AI IoT DX GX SDGs ESG M&A".split())


def extract_entities(rows: list[dict]) -> list[dict]:
    """見出し（と要約の先頭）から 企業・組織・製品 らしい固有名詞の候補を抽出し、出現記事数で並べる。
    形態素解析なしの経験則: カタカナ語（3文字以上・英字＋カタカナ）、英字の固有名詞・略語、
    組織名の接尾辞（製鉄・自動車・HD など）。一般語は除外リストで落とす。"""
    counts: dict[str, int] = {}
    surface: dict[str, str] = {}
    kinds: dict[str, str] = {}
    for a in rows:
        text = f"{a.get('title') or ''} {str(a.get('summary') or '')[:120]}"
        found: dict[str, tuple[str, str]] = {}
        for m in _ENT_ORG_RE.finditer(text):
            w = m.group(0)
            if w not in _ENT_STOP:
                found[_norm(w)] = (w, "org")
        for m in _ENT_KATAKANA_RE.finditer(text):
            w = m.group(0)
            if w in _ENT_STOP or len(w) > 20:
                continue
            mix = re.match(r"([A-Z]{2,6})([ァ-ヴー]+)$", w)   # 英字＋カタカナ（EVシフト・AIブーム）は一般語を除外
            if mix and (mix.group(1) in _ENT_STOP_ASCII or mix.group(2) in _ENT_STOP):
                continue
            k = _norm(w)
            if any(k in ok and k != ok for ok in found):   # 組織名の一部（トヨタ自動車 の トヨタ）は重複させない
                continue
            found.setdefault(k, (w, "katakana"))
        for m in _ENT_ASCII_RE.finditer(text):
            w = m.group(0).strip()
            if w.upper() in _ENT_STOP_ASCII or len(w) < 2 or len(w) > 30:
                continue
            k = _norm(w)
            if any(k in ok and k != ok for ok in found):
                continue
            found.setdefault(k, (w, "ascii"))
        for k, (w, kind) in found.items():
            counts[k] = counts.get(k, 0) + 1
            surface.setdefault(k, w)
            kinds.setdefault(k, kind)
    out = [{"name": surface[k], "kind": kinds[k], "count": n, "q": quote_term(surface[k])} for k, n in counts.items()]
    out.sort(key=lambda e: (-e["count"], e["name"]))
    return out[:ENTITY_MAX]


def _parse_json_array(text: str) -> list:
    """LLM の出力から JSON 配列を取り出す（コードフェンス・前置き・末尾切れをなるべく救う）。"""
    t = re.sub(r"```(?:json)?", "", text or "").strip()
    i, j = t.find("["), t.rfind("]")
    if i < 0:
        return []
    cands = ([t[i:j + 1]] if j > i else []) + [t[i:]]   # 「最後の ] まで」と「残り全部（末尾切れ）」の順に試す
    for cand in cands:
        for attempt in (cand, re.sub(r",\s*([\]}])", r"\1", cand)):   # 末尾カンマの除去
            try:
                v = json.loads(attempt)
                return v if isinstance(v, list) else []
            except ValueError:
                pass
        k = cand.rfind("}")   # 末尾が切れている → 最後の完全な要素まで
        if k > 0:
            try:
                v = json.loads(re.sub(r",\s*$", "", cand[:k + 1].rstrip()) + "]")
                return v if isinstance(v, list) else []
            except ValueError:
                pass
    return []


def extract_entities_ai(rows: list[dict]) -> dict:
    """ローカルLLM で見出しから 企業・組織・製品 を抽出（別表記つき）。出現記事数は見出し・要約に対して数える。"""
    cfg = ai_config()
    if cfg["provider"] != "local" and not cfg["api_key"]:
        return {"ok": False, "need_setup": True, "error": "生成AI APIが未設定です。設定からAPIキーを登録してください。"}
    rows = [a for a in rows if a.get("title")][:ENTITY_AI_TITLES]
    if not rows:
        return {"ok": False, "error": "対象の記事がありません"}
    system = ("あなたは産業ニュースの固有名詞抽出器です。指示された JSON だけを出力し、説明文を書かないでください。"
              + ("\n思考過程を書く場合は必ず <think> と </think> で囲んでください。" if cfg["provider"] == "local" else ""))
    prompt = ("以下のニュース見出しに登場する 企業・組織・製品・ブランド の固有名詞を抽出してください。\n"
              "出力は JSON 配列のみ。各要素は {\"name\": \"正式名\", \"kind\": \"企業\"|\"組織\"|\"製品\", \"aliases\": [\"略称や別表記\"]}。\n"
              "一般名詞（鉄鋼・EV・高炉 など）や地名は含めない。最大25件。見出しに無い名前を作らない。\n\n"
              + "\n".join(f"- {a['title']}" for a in rows))
    try:
        txt, _ = _split_reasoning(call_ai(cfg, system, prompt, [], max_tokens=1500))
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}
    items = _parse_json_array(txt)
    texts = [_norm(f"{a.get('title') or ''} {str(a.get('summary') or '')[:120]}") for a in rows]
    out: list[dict] = []
    seen: set[str] = set()
    for it in items:
        if not isinstance(it, dict):
            continue
        name = str(it.get("name") or "").strip()[:40]
        if not name or _norm(name) in seen:
            continue
        seen.add(_norm(name))
        aliases = [str(x).strip()[:40] for x in (it.get("aliases") or []) if isinstance(x, (str, int)) and str(x).strip()][:6]
        aliases = [x for x in aliases if _norm(x) != _norm(name)]
        keys = [_norm(name)] + [_norm(x) for x in aliases]
        n = sum(1 for t in texts if any(k and k in t for k in keys))
        out.append({"name": name, "kind": str(it.get("kind") or "")[:10], "aliases": aliases, "count": n,
                    "q": "|".join(quote_term(x) for x in [name] + aliases)})
    out.sort(key=lambda e: (-e["count"], e["name"]))
    return {"ok": True, "entities": out[:ENTITY_MAX], "titles": len(rows), "raw_items": len(items)}


def watch_trends(weeks: int = 8) -> dict:
    """ウォッチ（kind=watch のテーマ）ごとの週別件数（直近 weeks 週・月曜始まり・古い順）。"""
    weeks = max(2, min(int(weeks or 8), 26))
    today = datetime.now().date()
    start = today - timedelta(days=today.weekday() + 7 * (weeks - 1))
    since = datetime(start.year, start.month, start.day).timestamp()
    labels = [(start + timedelta(days=7 * i)).isoformat() for i in range(weeks)]
    syn = _synonym_map()
    out: dict[str, dict] = {}
    with _archive_lock:
        _archive_init_locked()
        conn = _db()
        try:
            for r in conn.execute("SELECT * FROM themes WHERE coalesce(kind,'theme') = 'watch'"):
                f = json.loads(r["filters_json"] or "{}")
                where, params, _ = _search_where(f.get("q", ""), f.get("sources"), since, None,
                                                 f.get("category") or None, None, parse_query(f.get("q", ""), syn))
                counts = [0] * weeks
                for (ts,) in conn.execute("SELECT a.sort_ts " + _SEARCH_FROM + where, params):
                    d = datetime.fromtimestamp(float(ts or 0)).date()
                    i = (d - start).days // 7
                    if 0 <= i < weeks:
                        counts[i] += 1
                out[r["id"]] = {"weeks": counts, "total": sum(counts)}
        finally:
            conn.close()
    return {"labels": labels, "trends": out}


# ------------------------------------------------------------------ リサーチ: 比較ビュー（2つの条件の件数・情報源・固有名詞・報道量）

COMPARE_IDS = 60   # AI 比較レポートに渡す各群の記事上限


def _side_summary(label: str, f: dict) -> dict:
    """1つの条件（q / sources / category / 期間 / archived）を集計する。"""
    since, until = _filters_window(f)
    parsed = parse_query(f.get("q", ""))
    rows = archive_search(f.get("q", ""), 300, f.get("sources"), since, until, f.get("category") or None,
                          f.get("archived"), "new", parsed)
    srcs: dict[str, int] = {}
    for a in rows:
        k = a.get("source") or a.get("source_id") or ""
        srcs[k] = srcs.get(k, 0) + 1
    hist = archive_histogram(f.get("q", ""), f.get("sources"), f.get("category") or None, parsed, since, until)
    return {"label": label, "total": len(rows), "conds": _filters_conds({**f, "sources": _source_names(f.get("sources") or [])}),
            "sources": [{"name": k, "count": v} for k, v in sorted(srcs.items(), key=lambda x: (-x[1], x[0]))[:6]],
            "entities": extract_entities(rows)[:10],
            "hist": {"unit": hist["unit"], "buckets": hist["buckets"], "total": hist["total"]},
            "ids": [a["id"] for a in rows[:COMPARE_IDS]],
            "newest": str(rows[0].get("published") or "")[:10] if rows else "",
            "oldest": str(rows[-1].get("published") or "")[:10] if rows else ""}


def _prev_window(f: dict) -> dict:
    """同じ条件で「1つ前の期間」の条件を作る（直近N日 → その前のN日、from〜to → 同じ長さの直前）。"""
    since, until = _filters_window(f)
    now = time.time()
    if since is None:
        return {}
    until = until if until is not None else now
    length = max(86400.0, until - since)
    d0 = datetime.fromtimestamp(since - length).date().isoformat()
    d1 = datetime.fromtimestamp(since - 1).date().isoformat()
    return {"q": f.get("q", ""), "sources": f.get("sources") or [], "category": f.get("category") or "",
            "days": 0, "from": d0, "to": d1,                      # from/to は表示用（条件文）
            "since_ts": since - length, "until_ts": since}        # 実際の範囲は秒単位で直前の同じ長さ


def compare_conditions(body: dict) -> dict:
    """A（現在の条件）と B（テーマ／ウォッチ、同条件の前の期間、または任意の条件）を比較する。"""
    a = _clean_filters(body.get("a"))
    try:
        a["archived"] = float(body.get("a", {}).get("archived")) if isinstance(body.get("a"), dict) and body["a"].get("archived") else None
    except (TypeError, ValueError):
        a["archived"] = None
    bspec = body.get("b") if isinstance(body.get("b"), dict) else {}
    if bspec.get("theme_id"):
        t = get_theme(str(bspec.get("theme_id")))
        if not t:
            return {"ok": False, "error": "unknown theme"}
        b = dict(t["filters"])
        blabel = ("ウォッチ: " if t.get("kind") == "watch" else "テーマ: ") + t["name"]
    elif bspec.get("prev"):
        b = _prev_window(a)
        if not b:
            return {"ok": False, "error": "期間（直近N日 または 日付範囲）を指定すると「前の期間」と比較できます"}
        blabel = f"前の期間（{b['from']}〜{b['to']}）"
    else:
        b = _clean_filters(bspec)
        if not (b["q"] or b["sources"] or b["category"] or b["days"] or b["from"] or b["to"]):
            return {"ok": False, "error": "比較相手（テーマ・前の期間・条件）を指定してください"}
        blabel = str(bspec.get("label") or "条件B")[:60]
    alabel = str(body.get("a", {}).get("label") or "現在の条件")[:60] if isinstance(body.get("a"), dict) else "現在の条件"
    A = _side_summary(alabel, a)
    B = _side_summary(blabel, b)
    ea = {_norm(e["name"]): e["name"] for e in A["entities"]}
    eb = {_norm(e["name"]): e["name"] for e in B["entities"]}
    common = [ea[k] for k in ea if k in eb]
    return {"ok": True, "a": A, "b": B, "common": common,
            "only_a": [ea[k] for k in ea if k not in eb], "only_b": [eb[k] for k in eb if k not in ea],
            "filters_b": b}


# ---- 書き出し（Markdown / Word）

def report_markdown(rep: dict) -> str:
    conds = _filters_conds(rep.get("filters") or {})
    d = datetime.fromtimestamp(rep["created_at"], timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M")
    head = [f"# {rep['title']}", "",
            f"- 作成: {d}", f"- 問い: {rep['question']}",
            f"- 対象記事: {len(rep.get('sources') or [])} 件"]
    if conds:
        head.append("- 検索条件: " + " ／ ".join(conds))
    out = "\n".join(head) + "\n\n" + rep["markdown"].strip() + "\n\n## 出典\n"
    for s in rep.get("sources") or []:
        out += f"- [{s['n']}] {s.get('published') or ''} {s.get('source') or ''}｜{s.get('title') or ''}"
        if s.get("link"):
            out += f" <{s['link']}>"
        out += "\n"
    return out


# ------------------------------------------------------------------ リサーチ: 文書生成（Word / Excel / PowerPoint / PDF）× ローカルLLM
# 内容の構成・抽出・要約は LLM、ファイルの組み立ては docgen（標準ライブラリ）。生成物は exports/ に保存し一覧・再DL・削除できる。

EXPORT_DIR = BASE / "exports"
EXPORT_KINDS = {
    "docx": ("Word", ".docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
    "xlsx": ("Excel", ".xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
    "pptx": ("PowerPoint", ".pptx", "application/vnd.openxmlformats-officedocument.presentationml.presentation"),
    "pdf": ("PDF", ".pdf", "application/pdf"),
}
EXPORT_MAX = 200               # 保存する生成物の上限（古いものから削除）
FACTS_CHUNK_LOCAL = 3000       # 事実・数値の抽出で1回に渡す記事テキスト（文字）
FACTS_CHUNK_CLOUD = 10000
FACTS_MAX_ROWS = 120
SLIDES_DEFAULT = 8
SLIDES_MAX = 20


def _parse_json_object(text: str) -> dict:
    t = re.sub(r"```(?:json)?", "", text or "").strip()
    i, j = t.find("{"), t.rfind("}")
    if i < 0 or j <= i:
        return {}
    for attempt in (t[i:j + 1], re.sub(r",\s*([\]}])", r"\1", t[i:j + 1])):
        try:
            v = json.loads(attempt)
            return v if isinstance(v, dict) else {}
        except ValueError:
            pass
    return {}


def _json_system(local: bool) -> str:
    s = ("あなたは産業ニュースの調査アナリストです。与えられた資料のみに基づき、指示された JSON だけを出力してください。"
         "説明文・前置き・コードフェンスは不要です。資料に無い事実・推測を加えないでください。")
    if local:
        s += "\n思考過程を書く必要がある場合は必ず <think> と </think> で囲み、その後に JSON だけを書いてください。"
    return s


def compose_facts(cfg: dict, arts: list[dict], question: str, progress=None) -> list[dict]:
    """記事群から 事実・数値表（日付・企業・項目・値・単位・補足・出典番号）を LLM で抽出する（記事チャンクごと）。"""
    local = cfg["provider"] == "local"
    budget = FACTS_CHUNK_LOCAL if local else FACTS_CHUNK_CLOUD
    lines = [_article_line(i + 1, a) for i, a in enumerate(arts)]
    chunks = _chunk_lines(lines, budget)
    system = _json_system(local)
    rows: list[dict] = []
    for k, ch in enumerate(chunks, 1):
        if progress:
            progress(done=k - 1, total=len(chunks))
        prompt = (f"【問い】{question}\n\n【記事（[番号] 日付 媒体｜見出し／要約）】\n" + "\n".join(ch) +
                  "\n\n【指示】上の記事から、数値や固有の事実（投資額・生産能力・時期・数量・比率・価格など）を抽出し、JSON 配列だけを出力してください。\n"
                  "各要素は {\"date\": \"YYYY-MM-DD\", \"entity\": \"企業・組織\", \"topic\": \"何についての値か\", \"value\": \"数値\", "
                  "\"unit\": \"単位\", \"note\": \"補足（30字以内）\", \"cite\": 出典番号(整数)} の形。\n"
                  "数値や具体的事実が無い記事は省く。推測しない。最大15件。")
        try:
            txt, _ = _split_reasoning(call_ai(cfg, system, prompt, [], max_tokens=2500))
        except Exception:
            continue
        for it in _parse_json_array(txt):
            if not isinstance(it, dict):
                continue
            try:
                cite = int(it.get("cite") or 0)
            except (TypeError, ValueError):
                cite = 0
            if not (1 <= cite <= len(arts)):
                continue
            row = {"date": str(it.get("date") or "")[:10], "entity": str(it.get("entity") or "")[:40],
                   "topic": str(it.get("topic") or "")[:60], "value": str(it.get("value") or "")[:30],
                   "unit": str(it.get("unit") or "")[:16], "note": str(it.get("note") or "")[:60], "cite": cite}
            if row["topic"] or row["value"]:
                rows.append(row)
        if len(rows) >= FACTS_MAX_ROWS:
            break
    if progress:
        progress(done=len(chunks), total=len(chunks))
    rows.sort(key=lambda r: (r["date"] or "9999", r["cite"]))
    return rows[:FACTS_MAX_ROWS]


def compose_summary(cfg: dict, rep: dict, instructions: str) -> dict:
    """レポートからエグゼクティブサマリー（3〜5文・出典番号つき）とキーワードを LLM で作る。"""
    local = cfg["provider"] == "local"
    n = len(rep.get("sources") or [])
    prompt = (f"【レポート】\n{rep['markdown'][:12000 if not local else 6000]}\n\n【出典番号】[1]〜[{n}]\n\n"
              "【指示】上のレポートのエグゼクティブサマリーを JSON オブジェクトだけで出力してください: "
              "{\"summary\": [\"1文（60字以内）。末尾に出典番号 [n]\", ...（3〜5件）], \"keywords\": [\"キーワード\", ...（5件以内）]}"
              + (f"\n想定読者・体裁の指示: {instructions}" if instructions else ""))
    try:
        txt, _ = _split_reasoning(call_ai(cfg, _json_system(local), prompt, [], max_tokens=1200))
    except Exception:
        return {}
    obj = _parse_json_object(txt)
    summ = [str(x)[:120] for x in (obj.get("summary") or []) if isinstance(x, (str, int, float)) and str(x).strip()][:5]
    summ = [_strip_bad_cites(s, n)[0] for s in summ]
    kws = [str(x)[:30] for x in (obj.get("keywords") or []) if isinstance(x, (str, int, float)) and str(x).strip()][:8]
    return {"summary": summ, "keywords": kws}


def compose_slides(cfg: dict, rep: dict, instructions: str, n_slides: int) -> list[dict]:
    """レポートからスライド構成（見出し・箇条書き・発表者ノート）を LLM で作る。表紙・出典は Python 側で付ける。"""
    local = cfg["provider"] == "local"
    n = len(rep.get("sources") or [])
    n_slides = max(2, min(int(n_slides or SLIDES_DEFAULT), SLIDES_MAX))
    prompt = (f"【レポート】\n{rep['markdown'][:12000 if not local else 6000]}\n\n【出典番号】[1]〜[{n}]\n\n"
              f"【指示】上のレポートからプレゼン資料のスライド構成を作り、JSON 配列だけを出力してください。スライドは {n_slides} 枚以内。\n"
              "各要素は {\"title\": \"スライド見出し（20字以内）\", \"bullets\": [\"要点（45字以内。末尾に出典番号 [n]）\", ...（3〜5件）], "
              "\"notes\": \"発表者ノート（2〜3文）\"} の形。\n"
              "1枚目は全体像（要点）、最後は示唆・次に注目すべき点にする。表紙と出典一覧はこちらで付けるので含めない。レポートに無い事実を加えない。"
              + (f"\n想定読者・体裁の指示: {instructions}" if instructions else ""))
    try:
        txt, _ = _split_reasoning(call_ai(cfg, _json_system(local), prompt, [], max_tokens=3000))
    except Exception:
        return []
    out: list[dict] = []
    for it in _parse_json_array(txt):
        if not isinstance(it, dict):
            continue
        title = str(it.get("title") or "").strip()[:40]
        bullets = [_strip_bad_cites(str(b).strip(), n)[0][:120] for b in (it.get("bullets") or []) if isinstance(b, (str, int, float)) and str(b).strip()][:6]
        if not title and not bullets:
            continue
        out.append({"title": title or "要点", "bullets": bullets, "notes": str(it.get("notes") or "")[:400]})
    return out[:n_slides]


def _fmt_dt(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M")


def report_doc_model(rep: dict, summary: dict | None = None, facts: list[dict] | None = None) -> dict:
    """レポート → docgen の文書モデル（Word / PDF 共通）。"""
    conds = _filters_conds(rep.get("filters") or {})
    meta = [f"作成: {_fmt_dt(rep['created_at'])}", f"対象記事: {len(rep.get('sources') or [])} 件"]
    if conds:
        meta.append("検索条件: " + " ／ ".join(conds))
    if rep.get("model"):
        meta.append(f"生成モデル: {rep['model']}")
    blocks: list[dict] = []
    if summary and summary.get("summary"):
        blocks.append({"t": "h", "level": 2, "text": "エグゼクティブサマリー"})
        blocks.append({"t": "ul", "items": summary["summary"]})
        if summary.get("keywords"):
            blocks.append({"t": "p", "text": "キーワード: " + "・".join(summary["keywords"])})
    blocks.extend(docgen.md_to_blocks(rep.get("markdown") or ""))
    if facts:
        blocks.append({"t": "h", "level": 2, "text": "事実・数値表"})
        blocks.append({"t": "table", "header": ["日付", "企業・組織", "項目", "値", "単位", "補足", "出典"],
                       "rows": [[f["date"], f["entity"], f["topic"], f["value"], f["unit"], f["note"], f"[{f['cite']}]"] for f in facts[:60]]})
    return {"title": rep["title"], "subtitle": f"問い: {rep.get('question') or ''}", "meta": meta, "blocks": blocks,
            "sources": rep.get("sources") or [], "header": "Prism リサーチ", "footer": rep["title"][:40], "toc": True}


def report_docx(rep: dict) -> bytes:
    """Word（.docx）。見出し・箇条書き・表・出典リンク・ヘッダー/フッター・目次フィールド（docgen）。"""
    return docgen.build_docx(report_doc_model(rep))


def _articles_sheet_rows(arts: list[dict]) -> list[list]:
    return [[str(a.get("published") or "")[:10], a.get("source") or "", a.get("category") or "", a.get("title") or "",
             {"v": a.get("link") or "", "link": a.get("link") or ""} if a.get("link") else "", (a.get("summary") or "")[:300]] for a in arts]


def build_export_xlsx(title: str, question: str, conds: list[str], arts: list[dict], facts: list[dict]) -> bytes:
    """Excel: 概要 / 記事一覧 / 事実・数値 / 情報源別 / 日付別 / 出典。"""
    by_src: dict[str, int] = {}
    by_day: dict[str, int] = {}
    for a in arts:
        by_src[a.get("source") or ""] = by_src.get(a.get("source") or "", 0) + 1
        d = str(a.get("published") or "")[:10]
        by_day[d] = by_day.get(d, 0) + 1
    idx = {a["id"]: i + 1 for i, a in enumerate(arts)}
    link_of = {i + 1: a.get("link") or "" for i, a in enumerate(arts)}
    sheets = [
        {"name": "概要", "columns": [{"title": "項目", "width": 16}, {"title": "内容", "width": 80}], "filter": False,
         "rows": [["タイトル", title], ["問い", question], ["作成", _fmt_dt(time.time())], ["対象記事", len(arts)],
                  ["検索条件", " ／ ".join(conds) if conds else "—"], ["事実・数値の行数", len(facts)],
                  ["備考", "事実・数値はローカルLLMが記事の見出し・要約から抽出した値です。出典番号は『出典』シートの番号です"]]},
        {"name": "記事一覧", "columns": [{"title": "日付", "width": 12}, {"title": "媒体", "width": 16}, {"title": "カテゴリ", "width": 10},
                                      {"title": "見出し", "width": 60}, {"title": "URL", "width": 40}, {"title": "要約", "width": 70}],
         "rows": _articles_sheet_rows(arts)},
        {"name": "事実・数値", "columns": [{"title": "日付", "width": 12}, {"title": "企業・組織", "width": 20}, {"title": "項目", "width": 30},
                                        {"title": "値", "width": 14}, {"title": "単位", "width": 10}, {"title": "補足", "width": 36},
                                        {"title": "出典番号", "width": 9}, {"title": "出典URL", "width": 40}],
         "rows": [[f["date"], f["entity"], f["topic"], _num_or_str(f["value"]), f["unit"], f["note"], f["cite"],
                   {"v": link_of.get(f["cite"], ""), "link": link_of.get(f["cite"], "")} if link_of.get(f["cite"]) else ""] for f in facts]},
        {"name": "情報源別", "columns": [{"title": "媒体", "width": 20}, {"title": "件数", "width": 10}],
         "rows": [[k, v] for k, v in sorted(by_src.items(), key=lambda x: (-x[1], x[0]))]},
        {"name": "日付別", "columns": [{"title": "日付", "width": 12}, {"title": "件数", "width": 10}],
         "rows": [[k, v] for k, v in sorted(by_day.items())]},
        {"name": "出典", "columns": [{"title": "番号", "width": 7}, {"title": "日付", "width": 12}, {"title": "媒体", "width": 16},
                                  {"title": "見出し", "width": 60}, {"title": "URL", "width": 40}],
         "rows": [[idx[a["id"]], str(a.get("published") or "")[:10], a.get("source") or "", a.get("title") or "",
                   {"v": a.get("link") or "", "link": a.get("link") or ""} if a.get("link") else ""] for a in arts]},
    ]
    return docgen.build_xlsx(sheets)


def _num_or_str(v: str):
    s = str(v or "").replace(",", "").strip()
    try:
        f = float(s)
        return int(f) if f.is_integer() and "." not in s else f
    except ValueError:
        return v


def build_export_pptx(rep: dict, slides: list[dict], arts: list[dict]) -> bytes:
    """PowerPoint: 表紙 → LLM のスライド → 情報源別の件数（簡易棒グラフ）→ 出典。"""
    conds = _filters_conds(rep.get("filters") or {})
    by_src: dict[str, int] = {}
    for a in arts:
        by_src[a.get("source") or ""] = by_src.get(a.get("source") or "", 0) + 1
    top = sorted(by_src.items(), key=lambda x: (-x[1], x[0]))[:8]
    deck_slides = list(slides) or [{"title": "要点", "bullets": [ln.strip("-・ ") for ln in rep["markdown"].splitlines() if ln.strip().startswith(("-", "・"))][:5], "notes": ""}]
    if top:
        deck_slides.append({"title": "情報源別の記事件数", "chart": {"title": f"対象記事 {len(arts)} 件の内訳", "labels": [k for k, _ in top], "values": [v for _, v in top]},
                            "bullets": [f"最多は {top[0][0]}（{top[0][1]}件）" + (f"、次いで {top[1][0]}（{top[1][1]}件）" if len(top) > 1 else "")],
                            "notes": "情報源ごとの報道量。媒体による視点の違いを見るときの参考。"})
    srcs = rep.get("sources") or []
    per = 10
    for k in range(0, len(srcs), per):
        chunk = srcs[k:k + per]
        deck_slides.append({"title": "出典" + (f"（{k // per + 1}/{(len(srcs) + per - 1) // per}）" if len(srcs) > per else ""),
                            "bullets": [f"[{s['n']}] {s.get('published') or ''} {s.get('source') or ''}｜{(s.get('title') or '')[:48]}" for s in chunk],
                            "notes": "\n".join(f"[{s['n']}] {s.get('link') or ''}" for s in chunk)})
    deck = {"title": rep["title"], "subtitle": f"問い: {rep.get('question') or ''}" + (f"　／　{' ／ '.join(conds)}" if conds else ""),
            "footer": f"Prism リサーチ　{_fmt_dt(rep['created_at'])}　記事 {len(arts)} 件", "slides": deck_slides,
            "cover_notes": "このスライドはローカルLLMがレポートから構成し、Prism が組み立てました。各要点の [n] は出典番号です。"}
    return docgen.build_pptx(deck)


def _pdf_font_path() -> str | None:
    s = load_settings()
    pref = str(s.get("pdf_font") or "").strip() or None
    return docgen.find_jp_font(pref)


def _exports_prune(conn) -> None:
    rows = conn.execute("SELECT id, filename FROM exports ORDER BY created_at DESC").fetchall()
    for r in rows[EXPORT_MAX:]:
        try:
            (EXPORT_DIR / r["filename"]).unlink(missing_ok=True)
        except OSError:
            pass
        conn.execute("DELETE FROM exports WHERE id = ?", (r["id"],))


def _save_export(row: dict, data: bytes) -> None:
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    (EXPORT_DIR / row["filename"]).write_bytes(data)
    with _archive_lock:
        _archive_init_locked()
        conn = _db()
        try:
            conn.execute("INSERT OR REPLACE INTO exports VALUES (?,?,?,?,?,?,?,?,?)",
                         (row["id"], row["created_at"], row["kind"], row["title"], row["filename"], row["bytes"],
                          row.get("report_id") or "", row.get("instructions") or "", json.dumps(row.get("meta") or {}, ensure_ascii=False)))
            _exports_prune(conn)
            conn.commit()
        finally:
            conn.close()


def _row_to_export(r) -> dict:
    return {"id": r["id"], "created_at": r["created_at"], "kind": r["kind"], "kind_name": EXPORT_KINDS.get(r["kind"], ("?",))[0],
            "title": r["title"], "filename": r["filename"], "bytes": r["bytes"], "report_id": r["report_id"],
            "instructions": r["instructions"], "meta": json.loads(r["meta_json"] or "{}")}


def list_exports(limit: int = 100) -> list[dict]:
    with _archive_lock:
        _archive_init_locked()
        conn = _db()
        try:
            return [_row_to_export(r) for r in conn.execute("SELECT * FROM exports ORDER BY created_at DESC LIMIT ?",
                                                             (max(1, min(int(limit), 500)),))]
        finally:
            conn.close()


def get_export(eid: str) -> dict | None:
    if not (isinstance(eid, str) and _ID_RE.fullmatch(eid)):
        return None
    with _archive_lock:
        _archive_init_locked()
        conn = _db()
        try:
            r = conn.execute("SELECT * FROM exports WHERE id = ?", (eid,)).fetchone()
            return _row_to_export(r) if r else None
        finally:
            conn.close()


def export_file(eid: str) -> tuple[bytes, str, str] | None:
    """(bytes, content-type, 表示用ファイル名)。"""
    e = get_export(eid)
    if not e:
        return None
    p = EXPORT_DIR / e["filename"]
    if not p.is_file():
        return None
    _, ext, ctype = EXPORT_KINDS.get(e["kind"], ("", "", "application/octet-stream"))
    safe = re.sub(r"[\\/:*?\"<>|\r\n]+", "_", e["title"])[:60] or "export"
    return p.read_bytes(), ctype, safe + ext


def delete_export(eid: str) -> bool:
    e = get_export(eid)
    if not e:
        return False
    try:
        (EXPORT_DIR / e["filename"]).unlink(missing_ok=True)
    except OSError:
        pass
    with _archive_lock:
        conn = _db()
        try:
            conn.execute("DELETE FROM exports WHERE id = ?", (eid,))
            conn.commit()
        finally:
            conn.close()
    return True


def _run_export(job_id: str, kind: str, body: dict) -> None:
    """ワーカースレッド: 対象の解決（レポート or 記事）→ LLM で構成・抽出 → ファイル組み立て → 保存。"""
    def upd(**kw):
        with _jobs_lock:
            _jobs[job_id].update(kw)
    try:
        cfg = ai_config()
        if cfg["provider"] != "local" and not cfg["api_key"]:
            raise RuntimeError("生成AI APIが未設定です")
        instructions = str(body.get("instructions") or "").strip()[:300]
        rep = get_report(str(body.get("report_id") or "")) if body.get("report_id") else None
        ids = [str(i) for i in (body.get("ids") if isinstance(body.get("ids"), list) else []) if i][:REPORT_MAX_ARTICLES]
        filters = body.get("filters") if isinstance(body.get("filters"), dict) else {}
        if rep is None and body.get("theme_id"):
            t = get_theme(str(body["theme_id"]))
            if t:
                f = t["filters"]
                since, until = _filters_window(f)
                ids = [a["id"] for a in archive_search(f.get("q", ""), REPORT_MAX_ARTICLES, f.get("sources"), since, until, f.get("category") or None)]
                filters = {**f, "sources": _source_names(f.get("sources") or []), "theme": t["name"]}
        if rep is None and not ids:
            raise RuntimeError("対象（保存済みレポート、または記事）を指定してください")
        if rep is None and kind != "xlsx":   # Word / PowerPoint / PDF はレポートが土台。無ければ先に作る
            upd(state="preparing", label="レポートを生成中")
            question = str(body.get("question") or "").strip()[:300] or _default_question(filters)
            rep = _build_report(cfg, question, "overview", ids, filters, bool(body.get("fulltext")), None, upd)
            rep["id"] = secrets.token_hex(8)
            _save_report(rep)
        arts = _report_fetch(rep["article_ids"] if rep else ids)
        if not arts:
            raise RuntimeError("対象記事が過去ログに見つかりません")
        question = rep["question"] if rep else (str(body.get("question") or "").strip()[:300] or _default_question(filters))
        title = (rep["title"] if rep else _report_title(question, filters, "記事一覧"))[:80]
        conds = _filters_conds((rep or {}).get("filters") or filters)
        facts: list[dict] = []
        summary: dict = {}
        slides: list[dict] = []
        if kind in ("docx", "pdf", "xlsx"):
            upd(state="composing", label="事実・数値を抽出中", total=0, done=0)
            facts = compose_facts(cfg, arts, question, progress=lambda **kw: upd(**kw))
        if kind in ("docx", "pdf") and rep:
            upd(state="composing", label="エグゼクティブサマリーを作成中", total=0, done=0)
            summary = compose_summary(cfg, rep, instructions)
        if kind == "pptx" and rep:
            try:
                n_slides = int(body.get("slides") or SLIDES_DEFAULT)
            except (TypeError, ValueError):
                n_slides = SLIDES_DEFAULT
            upd(state="composing", label="スライド構成を作成中", total=0, done=0)
            slides = compose_slides(cfg, rep, instructions, n_slides)
        upd(state="building", label="ファイルを組み立て中")
        meta = {"facts": len(facts), "summary": len(summary.get("summary") or []), "slides": len(slides), "articles": len(arts)}
        if kind == "docx":
            data = docgen.build_docx(report_doc_model(rep, summary, facts))
        elif kind == "pdf":
            fp = _pdf_font_path()
            if not fp:
                raise RuntimeError("PDF 用の日本語 TrueType フォントが見つかりません。設定の「PDF フォント」にフォントファイル（例: C:\\Windows\\Fonts\\YuGothM.ttc）を指定してください")
            data = docgen.build_pdf(report_doc_model(rep, summary, facts), fp)
            meta["font"] = os.path.basename(fp)
        elif kind == "xlsx":
            data = build_export_xlsx(title, question, conds, arts, facts)
        else:
            data = build_export_pptx(rep, slides, arts)
        _, ext, _ = EXPORT_KINDS[kind]
        row = {"id": job_id, "created_at": time.time(), "kind": kind, "title": title, "filename": job_id + ext, "bytes": len(data),
               "report_id": rep["id"] if rep else "", "instructions": instructions, "meta": meta}
        _save_export(row, data)
        upd(state="done", export={**row, "kind_name": EXPORT_KINDS[kind][0]}, report_id=row["report_id"])
    except Exception as e:
        upd(state="error", error=f"{type(e).__name__}: {str(e)[:200]}")


def start_export_job(body: dict) -> dict:
    kind = body.get("kind")
    if kind not in EXPORT_KINDS:
        return {"ok": False, "error": "kind は docx / xlsx / pptx / pdf のいずれかです"}
    has_src = bool(body.get("report_id") or (isinstance(body.get("ids"), list) and body["ids"]) or body.get("theme_id"))
    if not has_src:
        return {"ok": False, "error": "対象（保存済みレポート・選択記事・テーマ）を指定してください"}
    if kind == "pdf" and not _pdf_font_path():
        return {"ok": False, "error": "PDF 用の日本語 TrueType フォントが見つかりません（Windows: Yu Gothic / Meiryo、Linux: IPA ゴシック等）。設定でフォントのパスを指定してください"}
    job_id = _new_job("export", export_kind=kind, label="準備中")
    threading.Thread(target=_run_export, args=(job_id, kind, body), daemon=True).start()
    return {"ok": True, "job_id": job_id, "kind": kind}


def detect_export_intent(text: str) -> dict | None:
    """会話文から「〜を pptx にして」のような書き出し意図を拾う（形式と指示）。該当しなければ None。"""
    t = str(text or "")
    kinds = [("pptx", r"pptx|パワポ|パワーポイント|powerpoint|スライド|プレゼン"), ("xlsx", r"xlsx|excel|エクセル|スプレッドシート|表計算"),
             ("docx", r"docx|word|ワード(?!プレス)"), ("pdf", r"pdf")]
    if not re.search(r"にして|に変換|で出力|で書き出|書き出し|作って|作成|生成|エクスポート|出して|化して|にまとめ", t, re.I):
        return None
    for k, pat in kinds:
        if re.search(pat, t, re.I):
            return {"kind": k, "instructions": t[:300]}
    return None


# ------------------------------------------------------------------ デモ記事（オフライン時）

# (age_min, category, source, title, summary)
_DEMO = [
    (7,  "テクノロジー", "Prism Tech", "国産オープン大規模言語モデルが日本語ベンチで最高精度を記録",
     "研究チームが公開した新モデルは、日本語の読解・要約タスクで既存モデルを上回る結果を示した。学習データと重みは商用利用可能なライセンスで配布される。"),
    (14, "世界", "Prism World", "主要国首脳会議が開幕、気候変動と経済安全保障が主要議題に",
     "各国の代表が集まり、脱炭素の投資枠組みとサプライチェーンの強靱化について議論を交わした。共同声明は会期末に採択される見通し。"),
    (23, "ビジネス", "Prism Biz", "半導体設備投資が過去最高を更新、AI需要が牽引",
     "調査会社の集計によると、今年の世界の半導体製造装置への投資額は前年比で大幅に増加した。データセンター向けの先端プロセスが投資を押し上げている。"),
    (35, "科学", "Prism Science", "深宇宙望遠鏡が最遠方の銀河候補を捉える",
     "観測チームは、初期宇宙に存在したとみられる銀河の光を検出したと発表した。分光観測による距離の確定が今後の焦点となる。"),
    (40, "専門", "Prism Journal", "新規高強度鋼板の疲労特性、専門誌が査読論文を掲載",
     "自動車・建材向けの新しい高張力鋼について、繰り返し荷重下での亀裂進展を評価した研究が学術誌に掲載された。組織制御による長寿命化の指針が示されている。"),
    (58, "専門", "Prism Journal", "産業用ロボットの力制御に関する国際会議、査読採択率を公表",
     "今年の会議では触覚フィードバックと学習制御を組み合わせた研究が目立ち、製造現場での実装事例の報告が増えたと専門誌がまとめた。"),
    (42, "スポーツ", "Prism Sports", "国内リーグ、若手主体のチームが首位に浮上",
     "終盤の連勝で勝ち点を伸ばし、リーグ戦の首位に立った。監督は「守備の集中力が結果につながった」と語った。"),
    (51, "エンタメ", "Prism Ent", "話題の長編アニメ映画、公開2週で興行収入の節目を突破",
     "口コミの広がりから動員が伸び続けており、配給会社は上映館の拡大を決めた。海外での公開も相次いで決まっている。"),
    (63, "テクノロジー", "Prism Tech", "ブラウザ標準にローカルAI推論API、主要ベンダーが実装で合意",
     "端末内で完結する軽量な推論を標準化する動きが進む。プライバシー保護とオフライン動作の両立が期待されている。"),
    (72, "総合", "Prism News", "全国で交通系ICの相互利用が拡大、地方路線にも対応",
     "利用者はひとつのカードで広域の移動が可能になる。運賃精算の共通基盤を各社が順次導入している。"),
    (88, "世界", "Prism World", "再生可能エネルギーの発電比率、複数地域で過去最高に",
     "風力と太陽光の伸びが顕著で、送電網の増強と蓄電池の普及が課題として挙げられている。"),
    (96, "ビジネス", "Prism Biz", "スタートアップ資金調達、ディープテック分野に資金が集中",
     "気候・素材・バイオなど、実装に時間のかかる領域への長期投資が目立つ。大企業との協業事例も増えている。"),
    (108, "科学", "Prism Science", "新しい触媒でアンモニア合成の省エネ化に道",
     "常温常圧に近い条件での反応に成功したとする研究が報告された。肥料や燃料としての応用が見込まれる。"),
    (121, "テクノロジー", "Prism Tech", "オープンソースの表計算ツールが大型アップデート、実時間共同編集に対応",
     "複数人での同時編集と履歴管理が加わった。プラグイン機構によって外部データ連携も容易になっている。"),
    (140, "スポーツ", "Prism Sports", "陸上短距離で日本新記録、若手選手が世界大会へ弾み",
     "追い風参考ながら自己ベストを大きく更新した。本人は「秋の大会でも記録を狙いたい」とコメント。"),
    (155, "エンタメ", "Prism Ent", "配信ドラマの国際共同制作が加速、複数言語で同時公開へ",
     "制作費の分担と市場の拡大を狙い、各国のスタジオが連携する事例が増えている。"),
    (168, "総合", "Prism News", "自治体のデジタル窓口、オンライン申請の対象手続きを大幅拡大",
     "来庁不要で完結する手続きが増える。マイナンバーとの連携で本人確認の手間も軽減される。"),
    (182, "世界", "Prism World", "国際物流の運賃指数が落ち着き、荷動きは緩やかに回復",
     "港湾の混雑が解消に向かい、主要航路の運賃が下落した。年末商戦に向けた在庫確保の動きもみられる。"),
    (205, "科学", "Prism Science", "海洋観測ブイの群れが黒潮の微細な変動を可視化",
     "自律型の観測機を多数展開することで、これまで捉えにくかった渦の挙動が明らかになりつつある。"),
    (223, "テクノロジー", "Prism Tech", "軽量ロボットアーム、家庭向けに低価格化の波",
     "教育や自作の用途で普及が進む。オープンな制御ソフトの充実が価格低下を後押ししている。"),
    (245, "ビジネス", "Prism Biz", "地域金融機関がAIで与信審査を高速化、中小企業融資を後押し",
     "決算データの読み取りを自動化し、審査期間の短縮につなげた。説明可能性の確保が今後の論点となる。"),
    (270, "総合", "Prism News", "全国的に空気の乾燥続く、週末は広く晴れの見込み",
     "気象台は火の取り扱いに注意を呼びかけている。行楽地は多くの人出が予想される。"),
]


def demo_articles() -> list[dict]:
    now = time.time()
    out = []
    for age_min, cat, src, title, summary in _DEMO:
        ts = now - age_min * 60
        dt = datetime.fromtimestamp(ts, timezone.utc)
        aid = hashlib.md5(f"demo:{title}".encode("utf-8")).hexdigest()[:12]
        out.append({
            "id": aid, "source": src, "source_id": "demo",
            "category": cat, "title": title, "link": "",
            "summary": summary, "thumbnail": None,
            "published": dt.isoformat(), "published_ts": ts,
        })
    out.sort(key=lambda a: a["published_ts"], reverse=True)
    return out


# ------------------------------------------------------------------ HTTP

def source_status(sources: list[dict], errors: dict[str, str]) -> list[dict]:
    return [{
        "id": s.get("id", ""), "name": s.get("name", ""), "url": s.get("url", ""),
        "category": s.get("category", "総合"), "enabled": s.get("enabled", True),
        "error": errors.get(s.get("id", "")),
    } for s in sources]


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_a):  # 静かに
        pass

    def _json(self, obj, code: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_bytes(self, data: bytes, ctype: str, filename: str) -> None:
        """ファイルダウンロード応答（Content-Disposition は RFC 5987 で UTF-8 名を渡す）。"""
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Disposition", "attachment; filename*=UTF-8''" + quote(filename))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _same_origin(self) -> bool:
        """ブラウザからのクロスサイト書き込み(CSRF)を弾く。

        Origin / Referer が付いていて自ホストと異なれば False。Host も
        ループバックに固定し DNS リバインディングを防ぐ。
        ヘッダの無い非ブラウザ(curl 等)は許可する。"""
        host = self.headers.get("Host", "")
        hostname = host.rsplit(":", 1)[0].strip("[]").lower()
        if hostname not in ("127.0.0.1", "localhost", "::1"):
            return False   # 127.0.0.1 以外の Host はブラウザ経由の DNS リバインディング等
        for h in (self.headers.get("Origin"), self.headers.get("Referer")):
            if h and urlparse(h).netloc != host:
                return False
        return True

    def _read_body(self) -> dict:
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return {}
        if n <= 0 or n > MAX_BODY:
            return {}
        try:
            obj = json.loads(self.rfile.read(n).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}
        return obj if isinstance(obj, dict) else {}   # 非オブジェクトJSONで落ちない

    # ------------------------------------------------------------------ GET
    def do_GET(self):
        u = urlparse(self.path)
        if u.path in ("/", "/index.html"):
            try:
                body = UI_FILE.read_bytes()
            except OSError:
                self._json({"error": "index.html が見つかりません"}, 500)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if u.path == "/api/articles":
            q = parse_qs(u.query)
            force = q.get("refresh", ["0"])[0] in ("1", "true")
            data = get_feed(force=force)
            sources = load_sources()
            self._json({
                "articles": data["articles"],
                "sources": source_status(sources, data["errors"]),
                "categories": CATEGORIES,
                "offline": data["offline"],
                "updated": data["updated"],
                "errors": data["errors"],
                "count": len(data["articles"]),
            })
            return

        if u.path == "/api/sources":
            with _cache_lock:
                errors = dict(_cache["errors"]) if _cache["ts"] else {}
            self._json({"sources": source_status(load_sources(), errors),
                        "categories": CATEGORIES})
            return

        if u.path == "/api/sources/diagnose":   # 1件を実取得して失敗理由を切り分ける
            sid = (parse_qs(u.query).get("id") or [""])[0]
            src = next((s for s in load_sources() if s.get("id") == sid), None)
            if not src:
                self._json({"ok": False, "error": "unknown source"}, 404)
                return
            self._json({"ok": True, "diag": diagnose_source(src)})
            return

        if u.path == "/api/archive/search":   # 過去ログの条件検索（キーワード×情報源×期間×カテゴリ）
            q = parse_qs(u.query)
            g = lambda k, d="": (q.get(k) or [d])[0]
            sources = [s for s in g("sources").split(",") if s.strip()]
            category = g("category") if g("category") in CATEGORIES else None
            try:
                days = int(g("days") or 0)
            except ValueError:
                days = 0
            since, until = _filters_window({"from": g("from"), "to": g("to"), "days": days})
            try:   # テーマの「新着のみ」: 前回確認(epoch)より後に保存された記事だけ
                archived = float(g("archived")) if g("archived") else None
            except ValueError:
                archived = None
            parsed = parse_query(g("q"))
            order = "rel" if g("order") == "rel" else "new"
            arts = archive_search(g("q"), g("limit", "60"), sources, since, until, category, archived, order, parsed)
            groups = group_duplicates(arts) if g("dedup", "1") not in ("0", "false") else 0
            self._json({"ok": True, "count": len(arts), "articles": arts, "stats": archive_stats(),
                        "fts": _fts_ok, "order": order, "expanded": parsed["expanded"], "dup_groups": groups,
                        "filters": {"q": g("q"), "sources": sources, "category": category,
                                    "from": g("from"), "to": g("to"), "days": days, "archived": archived}})
            return

        if u.path == "/api/archive/entities":   # 検索結果に出てくる 企業・製品 らしい固有名詞の候補（ウォッチ用）
            q = parse_qs(u.query)
            g = lambda k, d="": (q.get(k) or [d])[0]
            sources = [s for s in g("sources").split(",") if s.strip()]
            category = g("category") if g("category") in CATEGORIES else None
            try:
                days = int(g("days") or 0)
            except ValueError:
                days = 0
            since, until = _filters_window({"from": g("from"), "to": g("to"), "days": days})
            try:
                archived = float(g("archived")) if g("archived") else None
            except ValueError:
                archived = None
            rows = archive_search(g("q"), 300, sources, since, until, category, archived)
            self._json({"ok": True, "entities": extract_entities(rows), "articles": len(rows)})
            return

        if u.path == "/api/research/watch/trends":   # ウォッチごとの週別件数
            try:
                weeks = int((parse_qs(u.query).get("weeks") or ["8"])[0])
            except ValueError:
                weeks = 8
            self._json({"ok": True, **watch_trends(weeks)})
            return

        if u.path == "/api/archive/histogram":   # 日付×情報源の件数分布（期間以外の条件で）
            q = parse_qs(u.query)
            g = lambda k, d="": (q.get(k) or [d])[0]
            sources = [s for s in g("sources").split(",") if s.strip()]
            category = g("category") if g("category") in CATEGORIES else None
            self._json({"ok": True, **archive_histogram(g("q"), sources, category)})
            return

        if u.path == "/api/archive/stats":
            self._json({"ok": True, **archive_stats()})
            return

        if u.path == "/api/research/synonyms":   # 同義語辞書（1行1グループのテキスト）
            self._json({"ok": True, "groups": synonym_groups(), "text": synonyms_text(), "fts": _fts_ok})
            return

        if u.path == "/api/research/reports":   # 保存済みレポート一覧
            self._json({"ok": True, "reports": list_reports()})
            return

        if u.path == "/api/research/themes":   # 保存したテーマ（検索条件）一覧。新着件数つき
            self._json({"ok": True, "themes": list_themes()})
            return

        if u.path == "/api/research/report":    # レポート1件
            rep = get_report((parse_qs(u.query).get("id") or [""])[0])
            self._json({"ok": True, "report": rep} if rep else {"ok": False, "error": "unknown report"},
                       200 if rep else 404)
            return

        if u.path == "/api/research/report/status":   # 生成ジョブの進捗
            self._json(report_job_status((parse_qs(u.query).get("id") or [""])[0]))
            return

        if u.path == "/api/research/exports":   # 生成した文書ファイルの一覧
            self._json({"ok": True, "exports": list_exports()})
            return

        if u.path == "/api/research/export/file":   # 生成した文書ファイルのダウンロード
            r = export_file((parse_qs(u.query).get("id") or [""])[0])
            if not r:
                self._json({"ok": False, "error": "unknown export"}, 404)
                return
            data, ctype, fname = r
            self._send_bytes(data, ctype, fname)
            return

        if u.path == "/api/research/report/export":   # Markdown / Word で書き出し
            q = parse_qs(u.query)
            rep = get_report((q.get("id") or [""])[0])
            if not rep:
                self._json({"ok": False, "error": "unknown report"}, 404)
                return
            fmt = (q.get("fmt") or ["md"])[0]
            safe = re.sub(r"[\\/:*?\"<>|\r\n]+", "_", rep["title"])[:60] or "report"
            if fmt == "docx":
                self._send_bytes(report_docx(rep),
                                 "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                                 safe + ".docx")
            else:
                self._send_bytes(report_markdown(rep).encode("utf-8"),
                                 "text/markdown; charset=utf-8", safe + ".md")
            return

        if u.path == "/api/settings":
            self._json({"ai": ai_status(), "proxy": proxy_config(),
                        "browser": browser_settings_raw(),
                        "pdf_font": str(load_settings().get("pdf_font") or ""), "pdf_font_found": bool(_pdf_font_path())})
            return

        self._json({"error": "not found"}, 404)

    # ------------------------------------------------------------------ POST
    def do_POST(self):
        u = urlparse(self.path)
        if not self._same_origin():
            self._json({"error": "cross-origin request refused"}, 403)
            return
        try:
            clen = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            clen = 0
        if clen > MAX_BODY:
            self._json({"error": "payload too large"}, 413)
            return

        if u.path == "/api/refresh":
            data = get_feed(force=True)
            self._json({"ok": True, "count": len(data["articles"]),
                        "offline": data["offline"], "updated": data["updated"],
                        "errors": data["errors"]})
            return

        if u.path == "/api/sources":  # 追加
            body = self._read_body()
            name = (body.get("name") or "").strip()
            url = safe_url(body.get("url"))
            if not name or not url:
                self._json({"ok": False, "error": "名前と有効なURL(http/https)は必須です"}, 400)
                return
            cat = body.get("category") or "総合"
            if cat not in CATEGORIES:
                cat = "総合"
            sources = add_source(name, url, cat)
            self._invalidate()
            self._json({"ok": True, "sources": source_status(sources, {})})
            return

        if u.path == "/api/sources/toggle":
            sid = (parse_qs(u.query).get("id") or [""])[0]
            sources, hit = toggle_source(sid)
            if not hit:
                self._json({"ok": False, "error": "unknown source"}, 404)
                return
            self._invalidate()
            self._json({"ok": True, "sources": source_status(sources, {})})
            return

        if u.path == "/api/sources/enable":  # 一括で有効/無効を設定
            body = self._read_body()
            ids = body.get("ids")
            ids = [str(i) for i in ids] if isinstance(ids, list) else []
            enabled = bool(body.get("enabled"))
            sources, n = set_enabled(ids, enabled)
            if n:
                self._invalidate()
            self._json({"ok": True, "changed": n, "sources": source_status(sources, {})})
            return

        if u.path == "/api/settings":  # AI API 設定の保存
            body = self._read_body()
            provider = body.get("provider")
            if provider not in AI_PROVIDERS:
                provider = "anthropic"
            base_url = (body.get("base_url") or "").strip()
            if base_url and not safe_url(base_url):
                self._json({"ok": False, "error": "base_url は http/https の有効なURLにしてください"}, 400)
                return
            ai = {"provider": provider, "base_url": base_url,
                  "model": (body.get("model") or "").strip()}
            # api_key: 未指定/空なら既存を保持（画面には返さないため）
            new_key = body.get("api_key")
            if new_key:
                ai["api_key"] = str(new_key)
            elif body.get("clear_key"):
                ai["api_key"] = ""
            else:
                ai["api_key"] = ai_config()["api_key"]
            # プロキシ設定（llmlab と同じ流儀: 使う/環境変数/明示URL）
            use_proxy = bool(body.get("use_proxy", True))
            purl = (body.get("proxy_url") or "").strip()
            if not use_proxy:
                purl = ""   # 無効時は URL を保持・検証しない
            elif purl and not safe_url(purl):
                self._json({"ok": False, "error": "proxy_url は http/https の有効なURLにしてください"}, 400)
                return
            ca_bundle = (body.get("ca_bundle") or "").strip()   # 社内プロキシCA(任意・絶対パス)
            settings = load_settings()
            settings["ai"] = ai
            settings["proxy"] = {"use_proxy": use_proxy, "proxy_url": purl, "ca_bundle": ca_bundle}
            # 本文取得のヘッドレスブラウザ設定（いずれも任意・空=自動検出）
            settings["browser"] = {"binary": (body.get("browser_binary") or "").strip(),
                                   "driver": (body.get("driver_path") or "").strip()}
            if "pdf_font" in body:   # PDF 書き出し用の日本語 TrueType フォント（任意・空=自動検出）
                settings["pdf_font"] = str(body.get("pdf_font") or "").strip()
            save_settings(settings)
            self._json({"ok": True, "ai": ai_status(), "proxy": proxy_config(),
                        "browser": browser_settings_raw(), "pdf_font": settings.get("pdf_font", ""),
                        "pdf_font_found": bool(_pdf_font_path())})
            return

        if u.path == "/api/proxy/test":  # 接続テスト（フォーム値で試すだけ・保存しない）
            self._json(proxy_test(self._read_body()))
            return

        if u.path == "/api/research/report":  # レポート生成ジョブの開始（非同期・進捗は status で）
            r = start_report_job(self._read_body())
            self._json(r, 200 if r.get("ok") else 400)
            return

        if u.path == "/api/research/fulltext":  # 選択記事の本文一括取得ジョブ（進捗は report/status で）
            r = start_fulltext_job(self._read_body())
            self._json(r, 200 if r.get("ok") else 400)
            return

        if u.path == "/api/research/themes":  # テーマの保存（id があれば上書き）
            r = save_theme(self._read_body())
            self._json(r, 200 if r.get("ok") else 400)
            return

        if u.path == "/api/research/themes/seen":  # テーマを既読に（新着差分の基準時刻を更新）
            r = theme_seen(str(self._read_body().get("id") or ""))
            self._json(r, 200 if r.get("ok") else 404)
            return

        if u.path == "/api/research/themes/brief":  # テーマのブリーフ生成（直近N日 or 前回以降）
            r = start_theme_brief(self._read_body())
            self._json(r, 200 if r.get("ok") else 400)
            return

        if u.path == "/api/research/synonyms":  # 同義語辞書の保存
            self._json(save_synonyms(str(self._read_body().get("text") or "")))
            return

        if u.path == "/api/research/export":  # 文書生成ジョブ（Word / Excel / PowerPoint / PDF × ローカルLLM）
            r = start_export_job(self._read_body())
            self._json(r, 200 if r.get("ok") else 400)
            return

        if u.path == "/api/research/export/intent":  # 会話文から書き出し意図（形式）を判定
            self._json({"ok": True, "intent": detect_export_intent(str(self._read_body().get("text") or ""))})
            return

        if u.path == "/api/research/compare":  # 比較ビュー: A（現在の条件）と B（テーマ／前の期間／条件）
            r = compare_conditions(self._read_body())
            self._json(r, 200 if r.get("ok") else 400)
            return

        if u.path == "/api/research/entities/ai":  # ローカルLLM で 企業・組織・製品 を抽出（別表記つき）
            body = self._read_body()
            ids = body.get("ids") if isinstance(body.get("ids"), list) else []
            r = extract_entities_ai(_report_fetch([str(i) for i in ids if i][:ENTITY_AI_TITLES]))
            self._json(r, 200 if r.get("ok") else 400)
            return

        if u.path == "/api/ai/chat":  # 生成AIへの質問
            self._json(ai_chat(self._read_body()))
            return

        self._json({"error": "not found"}, 404)

    # ------------------------------------------------------------------ DELETE
    def do_DELETE(self):
        u = urlparse(self.path)
        if not self._same_origin():
            self._json({"error": "cross-origin request refused"}, 403)
            return
        if u.path == "/api/research/report":   # 保存済みレポートの削除
            rid = (parse_qs(u.query).get("id") or [""])[0]
            ok = delete_report(rid)
            self._json({"ok": ok} if ok else {"ok": False, "error": "unknown report"}, 200 if ok else 404)
            return

        if u.path == "/api/research/themes":   # テーマの削除
            ok = delete_theme((parse_qs(u.query).get("id") or [""])[0])
            self._json({"ok": ok} if ok else {"ok": False, "error": "unknown theme"}, 200 if ok else 404)
            return

        if u.path == "/api/research/export":   # 生成した文書ファイルの削除
            ok = delete_export((parse_qs(u.query).get("id") or [""])[0])
            self._json({"ok": ok} if ok else {"ok": False, "error": "unknown export"}, 200 if ok else 404)
            return

        if u.path == "/api/sources":
            sid = (parse_qs(u.query).get("id") or [""])[0]
            remain, changed = delete_source(sid)
            if not changed:
                self._json({"ok": False, "error": "unknown source"}, 404)
                return
            self._invalidate()
            self._json({"ok": True, "sources": source_status(remain, {})})
            return
        self._json({"error": "not found"}, 404)

    def _invalidate(self):
        with _cache_lock:
            _cache["ts"] = 0.0


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Prism — ニュースポータル")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--open", action="store_true", help="起動時にブラウザを開く")
    ap.add_argument("--demo", action="store_true", help="ネットワークを使わずデモ記事で起動")
    args = ap.parse_args(argv)

    if args.demo:  # デモ固定: 常にデモ記事を返す（ネットワークを使わない）
        global DEMO
        DEMO = True
        with _cache_lock:
            _cache.update({"articles": demo_articles(), "errors": {}, "offline": True,
                           "updated": datetime.now(timezone.utc).isoformat(), "ts": time.time()})

    server = ThreadingHTTPServer((HOST, args.port), Handler)
    url = f"http://{HOST}:{args.port}"
    print(f"Prism ニュースポータル: {url}  (Ctrl+C で終了)")
    if args.open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n停止します")


if __name__ == "__main__":
    main()
