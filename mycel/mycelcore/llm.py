"""LLM API クライアント（標準ライブラリのみ）。

- openai: POST {base_url}/chat/completions, {base_url}/embeddings
- azure:  POST {base_url}/openai/deployments/{model}/chat/completions?api-version=...
          （キーは ``api-key`` ヘッダ）
- 推論モデルの <think>…</think> は回答から除去する（rag-orchestrator と同じ扱い）
"""
from __future__ import annotations

import json
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request

_THINK_BLOCK_RE = re.compile(r"<think(?:ing)?>.*?</think(?:ing)?>", re.DOTALL | re.IGNORECASE)
_THINK_OPEN_RE = re.compile(r"<think(?:ing)?>", re.IGNORECASE)


def strip_think(text: str) -> str:
    out = _THINK_BLOCK_RE.sub("", text or "")
    low = out.lower()
    for tag in ("</think>", "</thinking>"):
        idx = low.rfind(tag)
        if idx != -1:
            out = out[idx + len(tag):]
            break
    m = _THINK_OPEN_RE.search(out)
    if m:
        out = out[:m.start()]
    return out.strip()


class LLMError(RuntimeError):
    """接続・API エラー。UI にそのまま出せる日本語メッセージを持つ。"""


class LLMClient:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        if not cfg.get("use_proxy"):
            proxy = urllib.request.ProxyHandler({})
        elif cfg.get("proxy_url"):
            proxy = urllib.request.ProxyHandler({"http": cfg["proxy_url"], "https": cfg["proxy_url"]})
        else:
            proxy = urllib.request.ProxyHandler()
        self._opener = urllib.request.build_opener(proxy)

    # ------------------------------------------------------------ URL と認証
    def _endpoint(self, kind: str) -> tuple[str, dict, str]:
        """(URL, ヘッダ, payload に入れる model 名) を返す。kind は chat / embed。"""
        cfg = self.cfg
        if kind == "chat":
            base, key, model = cfg.get("base_url", ""), cfg.get("api_key", ""), cfg.get("model", "")
            if not (base and model):
                raise LLMError("LLM が未設定です（設定の「LLM」で URL とモデルを登録してください）")
        else:
            base = cfg.get("embed_base_url") or cfg.get("base_url", "")
            key = cfg.get("embed_api_key") or cfg.get("api_key", "")
            model = cfg.get("embed_model", "")
            if not (base and model):
                raise LLMError("埋め込みモデルが未設定です")
        base = base.rstrip("/")
        headers = {"Content-Type": "application/json"}
        path = "chat/completions" if kind == "chat" else "embeddings"
        if cfg.get("provider") == "azure":
            if base.endswith("/openai"):
                base = base[: -len("/openai")]
            ver = urllib.parse.quote(cfg.get("api_version") or "2024-10-21")
            url = f"{base}/openai/deployments/{urllib.parse.quote(model)}/{path}?api-version={ver}"
            if key:
                headers["api-key"] = key
        else:
            url = f"{base}/{path}"
            if key:
                headers["Authorization"] = f"Bearer {key}"
        return url, headers, model

    def _post(self, url: str, headers: dict, payload: dict) -> dict:
        req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                     method="POST", headers=headers)
        timeout = float(self.cfg.get("request_timeout") or 120.0)
        last: Exception | None = None
        for attempt in range(2):
            try:
                with self._opener.open(req, timeout=timeout) as res:
                    return json.loads(res.read().decode("utf-8", errors="replace"))
            except urllib.error.HTTPError as e:
                try:
                    detail = e.read().decode("utf-8", errors="replace")[:300]
                except OSError:
                    detail = ""
                if e.code in (429, 502, 503) and attempt == 0:
                    last = e
                    time.sleep(2.0)
                    continue
                hint = {401: "（API キーを確認してください）", 404: "（URL かモデル名を確認してください）"}
                raise LLMError(f"LLM API エラー HTTP {e.code}{hint.get(e.code, '')}: "
                               f"{detail or e.reason}") from e
            except (urllib.error.URLError, socket.timeout, OSError, ValueError) as e:
                last = e
                if attempt == 0:
                    time.sleep(1.0)
        raise LLMError(f"LLM に接続できません: {last}") from last

    # ------------------------------------------------------------ API
    def chat(self, messages: list[dict] | str, *, system: str = "",
             max_tokens: int | None = None, temperature: float | None = None) -> str:
        if isinstance(messages, str):
            messages = [{"role": "user", "content": messages}]
        if system:
            messages = [{"role": "system", "content": system}] + messages
        url, headers, model = self._endpoint("chat")
        payload = {
            "messages": messages,
            "max_tokens": int(max_tokens or self.cfg.get("max_tokens") or 2048),
            "temperature": float(self.cfg.get("temperature", 0.2) if temperature is None else temperature),
        }
        if self.cfg.get("provider") != "azure":
            payload["model"] = model
        data = self._post(url, headers, payload)
        if isinstance(data.get("error"), dict):
            raise LLMError(f"LLM API エラー: {data['error'].get('message', data['error'])}")
        choices = data.get("choices") or []
        msg = (choices[0].get("message") or {}) if choices else {}
        return strip_think(msg.get("content") or "")

    def embed(self, texts: list[str]) -> list[list[float]]:
        url, headers, model = self._endpoint("embed")
        out: list[list[float]] = []
        for i in range(0, len(texts), 32):
            batch = texts[i:i + 32]
            payload = {"input": batch}
            if self.cfg.get("provider") != "azure":
                payload["model"] = model
            rows = self._post(url, headers, payload).get("data") or []
            if len(rows) != len(batch):
                raise LLMError(f"埋め込み応答の件数が合いません: {len(rows)} != {len(batch)}")
            rows.sort(key=lambda r: r.get("index", 0))
            for r in rows:
                vec = r.get("embedding")
                if not isinstance(vec, list) or not vec:
                    raise LLMError("埋め込み応答に embedding がありません")
                out.append([float(x) for x in vec])
        return out
