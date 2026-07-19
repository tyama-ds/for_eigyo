#!/usr/bin/env python3
"""Kaleido Agents — マルチエージェント・オーケストレータ.

オーケストレータ（メインエージェント）が依頼をステップに分解し、
サブエージェント（プランナー/リサーチャー/アナリスト/ライター/レビュアー）が
ツールモジュール（計算機・日付・単位変換・Web取得・LLM など）を使って依頼を完結する。

    python kaleido-agents/server.py            # http://127.0.0.1:8790
    python kaleido-agents/server.py --port 9400 --open

- 標準ライブラリのみ（pip install 不要）。127.0.0.1 にのみ bind し外部公開しない
- エージェントの実行エンジンと GUI はブラウザ側（index.html / app.js）
- サーバーはブラウザからは扱えない2つのツールを提供する
    /api/fetch  … Web ページ取得（SSRF対策つき、テキスト抽出して返す）
    /api/llm    … LLM 呼び出し（ローカルLLM / OpenAI互換 / Anthropic。設定は kaleido.config.json）
- LLM 未設定でもルールベースの計画・実行だけで動作する

ローカルLLM（news-portal / llmlab と同じ流儀）:
- provider "local" は OpenAI 互換エンドポイント（Ollama / LM Studio / llama.cpp 等）。
  既定は Ollama の http://localhost:11434/v1。APIキーは任意
- ローカル（内部アドレス）の LLM へは常にプロキシ非経由で直結する
- クラウドAI・Web取得は use_proxy 設定に従う
  （use_proxy=False → 直結 / proxy_url 指定 → そのURL / 空 → 環境変数 HTTP(S)_PROXY）
- TLS を傍受する社内プロキシの CA は ca_bundle（任意）で指定できる
- 推論系ローカルLLM（DeepSeek-R1 / QwQ 等）の <think>…</think> は本文から分離する
"""
from __future__ import annotations

import argparse
import html
import ipaddress
import json
import os
import re
import socket
import ssl
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
import urllib.error
import urllib.request

BASE = Path(__file__).resolve().parent
CONFIG_FILE = BASE / "kaleido.config.json"

HOST = "127.0.0.1"
DEFAULT_PORT = 8790
FETCH_TIMEOUT = 12       # 秒。/api/fetch の1リクエストあたり
FETCH_MAX_BYTES = 800_000
FETCH_MAX_REDIRECTS = 3
LLM_TIMEOUT = 180        # 秒。ローカルLLMは遅いことがあるので長め

PROVIDERS = ("none", "local", "openai", "anthropic")
LOCAL_DEFAULT_BASE = "http://localhost:11434/v1"   # Ollama の OpenAI互換エンドポイント

DEFAULT_CONFIG = {
    "provider": "none",          # none | local | openai | anthropic
    "base_url": "",              # local: http://localhost:11434/v1 など OpenAI互換の /v1
    "model": "",
    "api_key": "",               # local はキー任意
    # プロキシ（llmlab / news-portal と同じ3モード）
    "use_proxy": True,           # False=直結（環境変数のプロキシも無視）
    "proxy_url": "",             # 空なら環境変数 HTTP(S)_PROXY を使用
    "ca_bundle": "",             # 社内プロキシのCA証明書パス（任意）
}

_config_lock = threading.Lock()


# ---------------------------------------------------------------- config

def load_config() -> dict:
    with _config_lock:
        if CONFIG_FILE.exists():
            try:
                data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
                cfg = dict(DEFAULT_CONFIG)
                cfg.update({k: data.get(k, v) for k, v in DEFAULT_CONFIG.items()})
                if cfg["provider"] == "ollama":     # 旧設定の移行（native API → OpenAI互換）
                    cfg["provider"], cfg["base_url"] = "local", ""
                if cfg["provider"] not in PROVIDERS:
                    cfg["provider"] = "none"
                cfg["use_proxy"] = bool(cfg["use_proxy"])
                return cfg
            except (OSError, ValueError):
                pass
        return dict(DEFAULT_CONFIG)


def save_config(new_cfg: dict) -> dict:
    cfg = load_config()
    for key in ("provider", "base_url", "model", "proxy_url", "ca_bundle"):
        if key in new_cfg and isinstance(new_cfg[key], str):
            cfg[key] = new_cfg[key].strip()
    if cfg["provider"] not in PROVIDERS:
        cfg["provider"] = "none"
    if "use_proxy" in new_cfg:
        cfg["use_proxy"] = bool(new_cfg["use_proxy"])
    if not cfg["use_proxy"]:
        cfg["proxy_url"] = ""    # 直結時は URL を保持しない（news-portal と同じ）
    # api_key は空文字で送られてきたら「変更なし」扱い（UIに平文を返さないため）
    if isinstance(new_cfg.get("api_key"), str) and new_cfg["api_key"].strip():
        cfg["api_key"] = new_cfg["api_key"].strip()
    if new_cfg.get("clear_api_key"):
        cfg["api_key"] = ""
    with _config_lock:
        CONFIG_FILE.write_text(
            json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    return cfg


def public_config(cfg: dict) -> dict:
    return {
        "provider": cfg["provider"],
        "base_url": cfg["base_url"],
        "model": cfg["model"],
        "has_key": bool(cfg["api_key"]),
        "use_proxy": cfg["use_proxy"],
        "proxy_url": cfg["proxy_url"],
        "ca_bundle": cfg["ca_bundle"],
        "providers": list(PROVIDERS),
    }


# ---------------------------------------------------------------- proxy / opener

def _is_private_addr(host: str) -> bool:
    """host が私有/ループバック/リンクローカル等のIPに解決されるなら True。"""
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return True
    for info in infos:
        try:
            addr = ipaddress.ip_address(info[4][0])
        except ValueError:
            return True
        if (addr.is_private or addr.is_loopback or addr.is_link_local
                or addr.is_reserved or addr.is_multicast or addr.is_unspecified):
            return True
    return False


def _host_is_internal(url: str) -> bool:
    """URL のホストが内部（ローカル/プライベート）アドレスに解決されるか。"""
    try:
        host = urlparse(url).hostname
        return bool(host) and _is_private_addr(host)
    except (ValueError, UnicodeError):
        return False


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: N802
        return None


def _ssl_context(cfg: dict):
    """HTTPS 検証用の SSL コンテキスト。TLS を傍受する社内プロキシの CA は
    ca_bundle か環境変数 SSL_CERT_FILE で指定できる。検証は常に有効。"""
    ca = cfg.get("ca_bundle") or os.environ.get("SSL_CERT_FILE") or ""
    try:
        if ca and os.path.exists(ca):
            return ssl.create_default_context(cafile=ca)
    except (ssl.SSLError, OSError):
        pass
    return None   # None → urllib 既定（システムCA・環境変数を反映）


def _make_opener(force_direct: bool = False, cfg: dict | None = None):
    """設定に従って urllib の opener を作る（llmlab / news-portal と同じ3モード）。

    - force_direct=True または use_proxy=False → 直結（環境変数のプロキシも無視）
    - use_proxy=True + proxy_url             → その URL を使用
    - use_proxy=True + 空                     → 環境変数 HTTP(S)_PROXY を使用
    リダイレクトは自前で検査するため常に無効化する。
    """
    cfg = cfg or load_config()
    handlers = [_NoRedirect()]
    ctx = _ssl_context(cfg)
    if ctx is not None:
        handlers.append(urllib.request.HTTPSHandler(context=ctx))   # 社内CAを信頼
    if force_direct or not cfg["use_proxy"]:
        handlers.append(urllib.request.ProxyHandler({}))            # 直結
    elif cfg["proxy_url"]:
        p = cfg["proxy_url"]
        handlers.append(urllib.request.ProxyHandler({"http": p, "https": p}))
    # それ以外は環境変数のプロキシ（build_opener が既定の ProxyHandler を付与）
    return urllib.request.build_opener(*handlers)


# ---------------------------------------------------------------- /api/fetch

_TAG_DROP_RE = re.compile(
    r"<(script|style|noscript|svg|iframe|head)[^>]*>.*?</\1>",
    re.IGNORECASE | re.DOTALL,
)
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)


def _html_to_text(raw: str) -> str:
    raw = _TAG_DROP_RE.sub(" ", raw)
    raw = re.sub(r"<br\s*/?>|</(p|div|li|tr|h[1-6]|section|article)>", "\n", raw, flags=re.I)
    raw = re.sub(r"<[^>]+>", " ", raw)
    raw = html.unescape(raw)
    raw = re.sub(r"[ \t\r\f\v]+", " ", raw)
    raw = re.sub(r"\n\s*\n+", "\n\n", raw)
    return raw.strip()


def fetch_url(url: str) -> dict:
    """SSRF 対策つきで URL を取得し、タイトルとプレーンテキストを返す。"""
    opener = _make_opener()
    current = url
    for _ in range(FETCH_MAX_REDIRECTS + 1):
        parsed = urlparse(current)
        if parsed.scheme not in ("http", "https"):
            return {"error": "http/https の URL のみ取得できます"}
        if not parsed.hostname:
            return {"error": "URL のホスト名を解釈できません"}
        if _is_private_addr(parsed.hostname):
            return {"error": "プライベート/ローカルアドレスへのアクセスは拒否しました"}
        req = urllib.request.Request(current, headers={
            "User-Agent": "Mozilla/5.0 (KaleidoAgents/1.0; local personal tool)",
            "Accept": "text/html,application/xhtml+xml,text/plain,application/json;q=0.9,*/*;q=0.5",
            "Accept-Language": "ja,en;q=0.8",
        })
        try:
            with opener.open(req, timeout=FETCH_TIMEOUT) as res:
                body = res.read(FETCH_MAX_BYTES)
                ctype = res.headers.get("Content-Type", "")
                charset = res.headers.get_content_charset() or "utf-8"
                text = body.decode(charset, errors="replace")
                title_m = _TITLE_RE.search(text)
                title = html.unescape(title_m.group(1)).strip() if title_m else ""
                if "html" in ctype or text.lstrip()[:1] == "<":
                    text = _html_to_text(text)
                return {
                    "url": current,
                    "status": res.status,
                    "title": title,
                    "text": text[:24000],
                    "truncated": len(text) > 24000,
                }
        except urllib.error.HTTPError as e:
            if e.code in (301, 302, 303, 307, 308) and e.headers.get("Location"):
                current = urllib.request.urljoin(current, e.headers["Location"])
                continue
            return {"error": f"HTTP {e.code}: {e.reason}", "url": current}
        except (urllib.error.URLError, socket.timeout, OSError, ValueError) as e:
            return {"error": f"取得に失敗しました: {e}", "url": current}
    return {"error": "リダイレクトが多すぎます", "url": url}


# ---------------------------------------------------------------- /api/llm

def _post_json(url: str, payload: dict, headers: dict,
               no_proxy: bool = False, cfg: dict | None = None) -> dict:
    """JSON を POST して JSON を返す。no_proxy=True はローカルLLM向けの直結。"""
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/json", **headers,
    })
    with _make_opener(force_direct=no_proxy, cfg=cfg).open(req, timeout=LLM_TIMEOUT) as res:
        return json.loads(res.read().decode("utf-8", errors="replace"))


_THINK_PAIR_RE = re.compile(r"<think(?:ing)?>(.*?)</think(?:ing)?>\s*",
                            re.DOTALL | re.IGNORECASE)
_THINK_CLOSE_RE = re.compile(r"</think(?:ing)?>\s*", re.IGNORECASE)


def _split_reasoning(text: str) -> tuple[str, str]:
    """推論系ローカルLLM（DeepSeek-R1 / QwQ 等）が本文に混ぜて出力する
    <think>…</think> の推論過程を分離する。開きタグ無しで </think> だけ来る
    ケース（テンプレート側で <think> が消される LM Studio 等）にも対応。
    戻り値は (最終解答, 推論過程)。全部が推論だった場合は推論を解答として返す。"""
    if not text or "</think" not in text.lower():
        return text, ""
    chunks: list[str] = []

    def _grab(m):
        chunks.append(m.group(1).strip())
        return ""

    stripped = _THINK_PAIR_RE.sub(_grab, text)
    if "</think" in stripped.lower():   # 開きタグの無い残骸: 先頭〜</think> が推論
        parts = _THINK_CLOSE_RE.split(stripped, maxsplit=1)
        chunks.insert(0, parts[0].strip())
        stripped = parts[1] if len(parts) > 1 else ""
    answer = stripped.strip()
    reasoning = "\n\n".join(c for c in chunks if c)
    if not answer:
        return (reasoning or text.strip()), ""
    return answer, reasoning


# ---- function calling: 正規化フォーマット <-> 各プロバイダ形式の変換 ----
# クライアントは正規化形式のみを扱う:
#   messages: [{role: user|assistant|tool, content, tool_calls?, tool_call_id?}]
#   tools:    [{name, description, parameters(JSON Schema)}]
#   応答:     {text, tool_calls: [{id, name, arguments(dict)}]}

def _to_openai_messages(msgs: list, system: str) -> list:
    out = [{"role": "system", "content": system}] if system else []
    for m in msgs:
        role = m.get("role")
        if role == "assistant":
            entry = {"role": "assistant", "content": m.get("content") or None}
            tcs = m.get("tool_calls") or []
            if tcs:
                entry["tool_calls"] = [{
                    "id": str(t.get("id", "")), "type": "function",
                    "function": {
                        "name": str(t.get("name", "")),
                        "arguments": json.dumps(t.get("arguments") or {}, ensure_ascii=False),
                    },
                } for t in tcs]
            out.append(entry)
        elif role == "tool":
            out.append({"role": "tool", "tool_call_id": str(m.get("tool_call_id", "")),
                        "content": str(m.get("content", ""))[:8000]})
        else:
            out.append({"role": "user", "content": str(m.get("content", ""))})
    return out


def _parse_openai_tool_calls(msg: dict) -> list:
    calls = []
    for t in msg.get("tool_calls") or []:
        fn = t.get("function") or {}
        try:
            args = json.loads(fn.get("arguments") or "{}")
            if not isinstance(args, dict):
                args = {"input": str(args)}
        except ValueError:
            args = {"input": str(fn.get("arguments", ""))}
        calls.append({"id": str(t.get("id", "")), "name": str(fn.get("name", "")), "arguments": args})
    return calls


def _to_anthropic_messages(msgs: list) -> list:
    out: list = []
    for m in msgs:
        role = m.get("role")
        if role == "assistant":
            blocks = []
            if m.get("content"):
                blocks.append({"type": "text", "text": str(m["content"])})
            for t in m.get("tool_calls") or []:
                blocks.append({"type": "tool_use", "id": str(t.get("id", "")),
                               "name": str(t.get("name", "")), "input": t.get("arguments") or {}})
            entry = {"role": "assistant", "content": blocks or [{"type": "text", "text": "…"}]}
        elif role == "tool":
            entry = {"role": "user", "content": [{
                "type": "tool_result", "tool_use_id": str(m.get("tool_call_id", "")),
                "content": str(m.get("content", ""))[:8000]}]}
        else:
            entry = {"role": "user", "content": [{"type": "text", "text": str(m.get("content", ""))}]}
        # Anthropic は同一 role の連続を許さないため結合する
        if out and out[-1]["role"] == entry["role"]:
            out[-1]["content"].extend(entry["content"])
        else:
            out.append(entry)
    return out


def call_llm_chat(messages: list, system: str = "", tools: list | None = None,
                  max_tokens: int = 1400, cfg: dict | None = None) -> dict:
    """messages / tools（正規化形式）で LLM を呼び、text と tool_calls を返す。"""
    cfg = cfg or load_config()
    provider = cfg["provider"]
    if provider == "none":
        return {"error": "LLM が未設定です（設定画面から接続先を登録してください）"}
    if provider in ("openai", "anthropic") and not cfg["api_key"]:
        return {"error": f"{provider} には API キーが必要です"}
    try:
        if provider == "anthropic":
            base = (cfg["base_url"] or "https://api.anthropic.com").rstrip("/")
            payload = {
                "model": cfg["model"] or "claude-haiku-4-5-20251001",
                "max_tokens": max_tokens,
                "messages": _to_anthropic_messages(messages),
            }
            if system:
                payload["system"] = system
            if tools:
                payload["tools"] = [{
                    "name": t["name"], "description": t.get("description", ""),
                    "input_schema": t.get("parameters") or {"type": "object", "properties": {}},
                } for t in tools]
            data = _post_json(f"{base}/v1/messages", payload, {
                "x-api-key": cfg["api_key"], "anthropic-version": "2023-06-01",
            }, cfg=cfg)
            blocks = data.get("content") or []
            text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
            calls = [{"id": b.get("id", ""), "name": b.get("name", ""),
                      "arguments": b.get("input") or {}}
                     for b in blocks if b.get("type") == "tool_use"]
            return {"text": text, "tool_calls": calls}

        # OpenAI 互換（local / openai）
        if provider == "local":
            base = (cfg["base_url"] or LOCAL_DEFAULT_BASE).rstrip("/")
            default_model, no_proxy = "llama3.1", _host_is_internal(base)
        else:
            base = (cfg["base_url"] or "https://api.openai.com/v1").rstrip("/")
            default_model, no_proxy = "gpt-4o-mini", False
        headers = {"Authorization": f"Bearer {cfg['api_key']}"} if cfg["api_key"] else {}
        payload = {
            "model": cfg["model"] or default_model,
            "messages": _to_openai_messages(messages, system),
            "max_tokens": max_tokens,
        }
        if tools:
            payload["tools"] = [{"type": "function", "function": {
                "name": t["name"], "description": t.get("description", ""),
                "parameters": t.get("parameters") or {"type": "object", "properties": {}},
            }} for t in tools]
        data = _post_json(f"{base}/chat/completions", payload, headers,
                          no_proxy=no_proxy, cfg=cfg)
        msg = (data.get("choices") or [{}])[0].get("message") or {}
        content = (msg.get("content") or "").strip()
        rc = (msg.get("reasoning_content") or msg.get("reasoning") or "").strip()
        if rc:
            content = f"<think>{rc}</think>\n{content}"
        answer, reasoning = _split_reasoning(content)
        out = {"text": answer, "tool_calls": _parse_openai_tool_calls(msg)}
        if reasoning:
            out["reasoning"] = reasoning[:4000]
        return out
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", errors="replace")[:400]
        except OSError:
            pass
        return {"error": f"LLM API エラー HTTP {e.code}: {detail or e.reason}"}
    except (urllib.error.URLError, socket.timeout, OSError, ValueError) as e:
        return {"error": f"LLM に接続できません: {e}"}


def call_llm(prompt: str, system: str = "", max_tokens: int = 1200,
             cfg: dict | None = None) -> dict:
    cfg = cfg or load_config()
    provider = cfg["provider"]
    if provider == "none":
        return {"error": "LLM が未設定です（設定画面から接続先を登録してください）"}
    # ローカルLLM はAPIキー不要。クラウドはキー必須。
    if provider in ("openai", "anthropic") and not cfg["api_key"]:
        return {"error": f"{provider} には API キーが必要です（設定画面から登録してください）"}
    try:
        if provider == "anthropic":
            base = (cfg["base_url"] or "https://api.anthropic.com").rstrip("/")
            payload = {
                "model": cfg["model"] or "claude-haiku-4-5-20251001",
                "max_tokens": max_tokens,
                "messages": [{"role": "user", "content": prompt}],
            }
            if system:
                payload["system"] = system
            data = _post_json(f"{base}/v1/messages", payload, {
                "x-api-key": cfg["api_key"],
                "anthropic-version": "2023-06-01",
            }, cfg=cfg)
            blocks = data.get("content") or []
            text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
            return {"text": text}

        # OpenAI 互換（Chat Completions）— local / openai 共通
        if provider == "local":
            base = (cfg["base_url"] or LOCAL_DEFAULT_BASE).rstrip("/")
            default_model = "llama3.1"
            # ローカル(=内部アドレス)のみプロキシ非経由で直結。外部URLならプロキシ設定に従う
            no_proxy = _host_is_internal(base)
        else:
            base = (cfg["base_url"] or "https://api.openai.com/v1").rstrip("/")
            default_model = "gpt-4o-mini"
            no_proxy = False
        headers = {"Authorization": f"Bearer {cfg['api_key']}"} if cfg["api_key"] else {}
        messages = ([{"role": "system", "content": system}] if system else [])
        messages.append({"role": "user", "content": prompt})
        data = _post_json(f"{base}/chat/completions", {
            "model": cfg["model"] or default_model,
            "messages": messages,
            "max_tokens": max_tokens,
        }, headers, no_proxy=no_proxy, cfg=cfg)
        choices = data.get("choices") or [{}]
        msg = choices[0].get("message") or {}
        content = (msg.get("content") or "").strip()
        # 推論を別フィールドで返す実装（DeepSeek API / Ollama 等）は <think> に畳み、
        # 下の _split_reasoning で本文と一元的に分離する
        rc = (msg.get("reasoning_content") or msg.get("reasoning") or "").strip()
        if rc:
            content = f"<think>{rc}</think>\n{content}"
        answer, reasoning = _split_reasoning(content)
        out = {"text": answer}
        if reasoning:
            out["reasoning"] = reasoning[:4000]
        return out
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", errors="replace")[:400]
        except OSError:
            pass
        return {"error": f"LLM API エラー HTTP {e.code}: {detail or e.reason}"}
    except (urllib.error.URLError, socket.timeout, OSError, ValueError) as e:
        return {"error": f"LLM に接続できません: {e}"}


# ---------------------------------------------------------------- HTTP server

class Handler(BaseHTTPRequestHandler):
    server_version = "KaleidoAgents/1.0"

    # -- helpers ------------------------------------------------------
    def _send_json(self, obj: dict, status: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: Path, ctype: str) -> None:
        try:
            body = path.read_bytes()
        except OSError:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_body_json(self) -> dict:
        try:
            length = min(int(self.headers.get("Content-Length", "0")), 2_000_000)
            raw = self.rfile.read(length)
            data = json.loads(raw.decode("utf-8"))
            return data if isinstance(data, dict) else {}
        except (ValueError, OSError):
            return {}

    # -- routes -------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        route = parsed.path
        if route in ("/", "/index.html"):
            self._send_file(BASE / "index.html", "text/html; charset=utf-8")
        elif route == "/style.css":
            self._send_file(BASE / "style.css", "text/css; charset=utf-8")
        elif route == "/app.js":
            self._send_file(BASE / "app.js", "text/javascript; charset=utf-8")
        elif route == "/api/config":
            self._send_json(public_config(load_config()))
        elif route == "/api/fetch":
            qs = parse_qs(parsed.query)
            url = (qs.get("url") or [""])[0].strip()
            if not url:
                self._send_json({"error": "url パラメータが必要です"}, 400)
            else:
                self._send_json(fetch_url(url))
        elif route == "/api/health":
            self._send_json({"ok": True, "app": "kaleido-agents"})
        else:
            self.send_error(404)

    def do_POST(self) -> None:  # noqa: N802
        route = urlparse(self.path).path
        if route == "/api/config":
            cfg = save_config(self._read_body_json())
            self._send_json(public_config(cfg))
        elif route == "/api/llm":
            body = self._read_body_json()
            system = str(body.get("system", ""))[:8_000]
            try:
                max_tokens = max(64, min(int(body.get("max_tokens", 1200)), 8192))
            except (TypeError, ValueError):
                max_tokens = 1200
            messages = body.get("messages")
            tools = body.get("tools")
            if isinstance(messages, list) and messages:
                # function calling / マルチターン経路（正規化形式）
                tools = tools if isinstance(tools, list) else None
                self._send_json(call_llm_chat(messages[:64], system,
                                              (tools or [])[:32] or None, max_tokens))
            else:
                prompt = str(body.get("prompt", ""))[:60_000]
                if not prompt.strip():
                    self._send_json({"error": "prompt が空です"}, 400)
                else:
                    self._send_json(call_llm(prompt, system, max_tokens))
        elif route == "/api/llm/test":
            result = call_llm("「接続OK」とだけ返答してください。", max_tokens=64)
            self._send_json(result)
        else:
            self.send_error(404)

    def log_message(self, fmt: str, *args) -> None:  # 静かに
        sys.stderr.write("kaleido: " + fmt % args + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="Kaleido Agents server")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--open", action="store_true", help="起動後にブラウザを開く")
    args = parser.parse_args()

    server = ThreadingHTTPServer((HOST, args.port), Handler)
    url = f"http://{HOST}:{args.port}"
    print(f"Kaleido Agents: {url}  (Ctrl+C で終了)")
    if args.open:
        threading.Timer(0.6, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()
