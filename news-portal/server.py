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
import json
import os
import re
import socket
import sys
import threading
import time
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
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
MAX_SUMMARY = 320        # 要約の最大文字数
MAX_BODY = 256 * 1024    # POST ボディの上限（バイト）
MAX_FEED_BYTES = 6 * 1024 * 1024  # 1フィードの取得上限（バイト）
USER_AGENT = "Mozilla/5.0 (compatible; PrismNewsPortal/1.0; +local)"

AI_TIMEOUT = 60           # 生成AI呼び出しのタイムアウト（秒）
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

def fetch_source(src: dict) -> tuple[str, list[dict], str | None]:
    url = src.get("url", "")
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml;q=0.9, */*;q=0.8",
            "Accept-Encoding": "gzip, identity",
        })
        with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as r:
            raw = r.read(MAX_FEED_BYTES + 1)
            enc = (r.headers.get("Content-Encoding") or "").lower()
        if len(raw) > MAX_FEED_BYTES:
            return src.get("id", ""), [], "feed too large"
        if enc == "gzip" or raw[:2] == b"\x1f\x8b":
            raw = gzip.decompress(raw)
            if len(raw) > MAX_FEED_BYTES:
                return src.get("id", ""), [], "feed too large (decompressed)"
        arts = parse_feed(raw, src)
        return src.get("id", ""), arts[:MAX_PER_SOURCE], None
    except Exception as e:  # 1フィードの失敗を全体へ波及させない（種類を問わず握る）
        return src.get("id", ""), [], f"{type(e).__name__}: {e}"


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
    # link 単位で重複除去 → 新しい順
    seen: set[str] = set()
    uniq: list[dict] = []
    for a in articles:
        if a["id"] in seen:
            continue
        seen.add(a["id"])
        uniq.append(a)
    uniq.sort(key=lambda a: a["published_ts"] or 0.0, reverse=True)
    uniq = uniq[:MAX_TOTAL]

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
        os.replace(tmp, SETTINGS_FILE)


def ai_config() -> dict:
    ai = load_settings().get("ai") or {}
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
    """記事ページ本文をプレーンテキストで取得（失敗時は空文字）。proxy対応はurllib任せ。"""
    u = safe_url(url)
    if not u:
        return ""
    try:
        req = urllib.request.Request(u, headers={
            "User-Agent": USER_AGENT, "Accept": "text/html,*/*;q=0.8",
            "Accept-Encoding": "gzip, identity",
        })
        with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as r:
            raw = r.read(MAX_FEED_BYTES + 1)
            enc = (r.headers.get("Content-Encoding") or "").lower()
        if len(raw) > MAX_FEED_BYTES:
            return ""
        if enc == "gzip" or raw[:2] == b"\x1f\x8b":
            raw = gzip.decompress(raw)
        text = strip_html(raw.decode("utf-8", "replace"))
        return text[:MAX_PAGE_TEXT]
    except Exception:
        return ""


def _http_json(url: str, body: dict, headers: dict, no_proxy: bool = False) -> dict:
    """JSON を POST して JSON を返す（proxy・CA は urllib が処理）。

    no_proxy=True のときはプロキシを経由しない（ローカルLLM向け）。
    """
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={**headers, "Content-Type": "application/json"})
    if no_proxy:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        open_fn = opener.open
    else:
        open_fn = urllib.request.urlopen
    try:
        with open_fn(req, timeout=AI_TIMEOUT) as r:
            resp = r.read(4 * 1024 * 1024)
        return json.loads(resp.decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        detail = e.read(1500).decode("utf-8", "replace")
        raise RuntimeError(f"HTTP {e.code}: {detail[:500]}")
    except (urllib.error.URLError, socket.timeout, TimeoutError, ValueError, OSError) as e:
        raise RuntimeError(f"接続エラー: {e}")


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
        default_model, no_proxy = "llama3.1", True   # localhost はプロキシ非経由
    else:
        url = (base or "https://api.openai.com/v1") + "/chat/completions"
        default_model, no_proxy = "gpt-4o-mini", False
    full = ([{"role": "system", "content": system}] if system else []) + msgs
    body = {"model": model or default_model, "messages": full, "max_tokens": AI_MAX_TOKENS}
    headers = {"Authorization": "Bearer " + key} if key else {}   # ローカルはキー任意
    data = _http_json(url, body, headers, no_proxy=no_proxy)
    choices = data.get("choices") or [{}]
    return ((choices[0].get("message") or {}).get("content") or "").strip() or "（空の応答が返りました）"


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
        answer = call_ai(cfg, system, user_content, history)
        return {"ok": True, "answer": answer}
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

        Origin / Referer が付いていて自ホストと異なれば False。
        ヘッダの無い非ブラウザ(curl 等)は許可する。"""
        host = self.headers.get("Host", "")
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
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}

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
            data = _cache if _cache["ts"] else {"errors": {}}
            self._json({"sources": source_status(load_sources(), data.get("errors", {})),
                        "categories": CATEGORIES})
            return

        if u.path == "/api/settings":
            self._json({"ai": ai_status()})
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
            settings = load_settings()
            settings["ai"] = ai
            save_settings(settings)
            self._json({"ok": True, "ai": ai_status()})
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
