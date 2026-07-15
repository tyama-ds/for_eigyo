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
    /api/llm    … LLM 呼び出し（Ollama / OpenAI互換 / Anthropic。設定は kaleido.config.json）
- LLM 未設定でもルールベースの計画・実行だけで動作する
"""
from __future__ import annotations

import argparse
import html
import ipaddress
import json
import re
import socket
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

DEFAULT_CONFIG = {
    "provider": "none",          # none | ollama | openai | anthropic
    "base_url": "",              # ollama: http://127.0.0.1:11434 / openai互換: http://.../v1
    "model": "",
    "api_key": "",
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
                return cfg
            except (OSError, ValueError):
                pass
        return dict(DEFAULT_CONFIG)


def save_config(new_cfg: dict) -> dict:
    cfg = load_config()
    for key in ("provider", "base_url", "model"):
        if key in new_cfg and isinstance(new_cfg[key], str):
            cfg[key] = new_cfg[key].strip()
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
    }


# ---------------------------------------------------------------- /api/fetch

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


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: N802
        return None


_opener = urllib.request.build_opener(_NoRedirect)

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
            with _opener.open(req, timeout=FETCH_TIMEOUT) as res:
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

def _post_json(url: str, payload: dict, headers: dict) -> dict:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/json", **headers,
    })
    with urllib.request.urlopen(req, timeout=LLM_TIMEOUT) as res:
        return json.loads(res.read().decode("utf-8", errors="replace"))


def call_llm(prompt: str, system: str = "", max_tokens: int = 1200) -> dict:
    cfg = load_config()
    provider = cfg["provider"]
    if provider == "none":
        return {"error": "LLM が未設定です（設定画面から接続先を登録してください）"}
    try:
        if provider == "ollama":
            base = (cfg["base_url"] or "http://127.0.0.1:11434").rstrip("/")
            messages = ([{"role": "system", "content": system}] if system else [])
            messages.append({"role": "user", "content": prompt})
            data = _post_json(f"{base}/api/chat", {
                "model": cfg["model"] or "llama3.1",
                "messages": messages,
                "stream": False,
                "options": {"num_predict": max_tokens},
            }, {})
            return {"text": (data.get("message") or {}).get("content", "")}
        if provider == "openai":
            base = (cfg["base_url"] or "http://127.0.0.1:1234/v1").rstrip("/")
            headers = {}
            if cfg["api_key"]:
                headers["Authorization"] = f"Bearer {cfg['api_key']}"
            messages = ([{"role": "system", "content": system}] if system else [])
            messages.append({"role": "user", "content": prompt})
            data = _post_json(f"{base}/chat/completions", {
                "model": cfg["model"] or "gpt-4o-mini",
                "messages": messages,
                "max_tokens": max_tokens,
            }, headers)
            choices = data.get("choices") or []
            text = (choices[0].get("message") or {}).get("content", "") if choices else ""
            return {"text": text}
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
            })
            blocks = data.get("content") or []
            text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
            return {"text": text}
        return {"error": f"未知のプロバイダです: {provider}"}
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
            prompt = str(body.get("prompt", ""))[:60_000]
            system = str(body.get("system", ""))[:8_000]
            try:
                max_tokens = max(64, min(int(body.get("max_tokens", 1200)), 8192))
            except (TypeError, ValueError):
                max_tokens = 1200
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
