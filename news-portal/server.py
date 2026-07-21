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
import ipaddress
import json
import os
import re
import socket
import ssl
import sys
import threading
import time
import webbrowser
import zlib
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

BASE = Path(__file__).resolve().parent
FEEDS_FILE = BASE / "feeds.json"
UI_FILE = BASE / "index.html"

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


def fetch_page_text(url: str) -> str:
    """記事ページ本文をプレーンテキストで取得（失敗時は空文字）。proxy設定に従う。

    記事リンクはフィード提供者（=第三者）由来のため、内部アドレスへの取得は
    SSRF 対策として拒否し、リダイレクトも内部アドレスを追わない。
    """
    u = safe_url(url)
    if not u or _host_is_internal(u):
        return ""
    try:
        req = urllib.request.Request(u, headers={
            "User-Agent": USER_AGENT, "Accept": "text/html,*/*;q=0.8",
            "Accept-Encoding": "gzip, identity",
        })
        with _opener(block_internal=True).open(req, timeout=FETCH_TIMEOUT) as r:
            raw = r.read(MAX_FEED_BYTES + 1)
            enc = (r.headers.get("Content-Encoding") or "").lower()
        if len(raw) > MAX_FEED_BYTES:
            return ""
        if enc == "gzip" or raw[:2] == b"\x1f\x8b":
            raw = _gunzip_capped(raw, MAX_FEED_BYTES)
        text = strip_html(raw.decode("utf-8", "replace"))
        return text[:MAX_PAGE_TEXT]
    except Exception:
        return ""


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
    r"(?:Output Generation|Final (?:Answer|Response|Output)|最終(?:解答|回答|出力))"
    r"(?:\*{1,2}|_{1,2})?[ \t]*(?:\(.*?\))?[ \t]*[::]?[ \t]*$")


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


def call_ai(cfg: dict, system: str, user_content: str, history: list[dict]) -> str:
    """provider に応じて生成AIを呼び出し、本文テキストを返す。"""
    provider, key, model = cfg["provider"], cfg["api_key"], cfg["model"]
    base = cfg["base_url"].rstrip("/")
    # history は [{role, content(str)}]（user/assistant のみ想定）
    msgs = [{"role": m.get("role", "user"), "content": str(m.get("content", ""))}
            for m in history if m.get("role") in ("user", "assistant")]
    msgs.append({"role": "user", "content": user_content})

    if provider == "anthropic":
        url = (base or "https://api.anthropic.com") + "/v1/messages"
        body = {"model": model or "claude-opus-4-8",
                "max_tokens": AI_MAX_TOKENS, "messages": msgs}
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
        body["max_tokens"] = AI_MAX_TOKENS
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
    if ctx.get("kind") == "list":
        parts.append("【現在表示中の記事一覧】")
        for i, a in enumerate((ctx.get("items") or [])[:25], 1):
            parts.append(f"{i}. [{a.get('category','')}] {a.get('title','')}（{a.get('source','')}）"
                         + (f" — {a.get('summary','')}" if a.get("summary") else ""))
    else:
        parts.append("【対象の記事】")
        for f in ("title", "source", "category", "published", "summary", "link"):
            if ctx.get(f):
                parts.append(f"{f}: {ctx.get(f)}")
        # 本文取得（記事の実URLがあり、要求されていれば）
        if payload.get("fetch_page") and safe_url(ctx.get("link")):
            page = fetch_page_text(ctx.get("link"))
            if page:
                parts.append("\n【記事ページ本文（抜粋）】\n" + page)
    context_block = "\n".join(str(p) for p in parts if p)
    user_content = (context_block + "\n\n" if context_block else "") + "質問: " + question

    try:
        answer, reasoning = _split_reasoning(call_ai(cfg, system, user_content, history))
        return {"ok": True, "answer": answer, "reasoning": reasoning}
    except RuntimeError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:   # 想定外も UI に見せる
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


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

        if u.path == "/api/settings":
            self._json({"ai": ai_status(), "proxy": proxy_config()})
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
            save_settings(settings)
            self._json({"ok": True, "ai": ai_status(), "proxy": proxy_config()})
            return

        if u.path == "/api/proxy/test":  # 接続テスト（フォーム値で試すだけ・保存しない）
            self._json(proxy_test(self._read_body()))
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
