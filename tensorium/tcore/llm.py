"""LLM API クライアント（標準ライブラリのみ）。データ拡張（知識蒸留）の教師モデルとして使う。

- provider = "openai"    : OpenAI 互換 chat/completions（OpenAI / Azure 互換 / Ollama / LM Studio / vLLM / llama.cpp）
- provider = "anthropic" : Anthropic Messages API
- provider = "builtin"   : LLM を使わない内蔵生成（augment 側で処理。chat() は呼ばれない）
- プロキシ: 設定 use_proxy=False で環境変数も無視して直結。proxy_url があればそれを使う
- 推論モデルの <think>…</think> は応答から除去する
"""
from __future__ import annotations

import json
import re
import threading
import time
import urllib.error
import urllib.request

DEFAULT_BASE = {"openai": "https://api.openai.com/v1", "anthropic": "https://api.anthropic.com"}
ANTHROPIC_VERSION = "2023-06-01"
_THINK_RE = re.compile(r"<think(?:ing)?>.*?</think(?:ing)?>", re.DOTALL | re.IGNORECASE)


class LLMError(RuntimeError):
    """接続・API エラー（UI にそのまま表示できる日本語メッセージ）。"""


def strip_think(text: str) -> str:
    """<think>…</think>（閉じタグのみ / 未閉じも含む）を除いた本文を返す。"""
    out = _THINK_RE.sub("", text or "")
    low = out.lower()
    for tag in ("</think>", "</thinking>"):
        idx = low.rfind(tag)
        if idx != -1:
            out = out[idx + len(tag):]
            break
    m = re.search(r"<think(?:ing)?>", out, re.IGNORECASE)
    if m:                                   # 未閉じ（トークン切れ）: 以降はすべて思考として捨てる
        out = out[:m.start()]
    return out.strip()


class LLMClient:
    def __init__(self, cfg: dict):
        self.provider = (cfg.get("llm_provider") or "openai").strip()
        self.base_url = (cfg.get("llm_base_url") or DEFAULT_BASE.get(self.provider, "")).rstrip("/")
        self.model = (cfg.get("llm_model") or "").strip()
        self.api_key = cfg.get("llm_api_key") or ""
        self.max_tokens = int(cfg.get("llm_max_tokens") or 4096)
        self.timeout = float(cfg.get("llm_timeout") or 180)
        self.temperature = float(cfg.get("llm_temperature") if cfg.get("llm_temperature") is not None else 0.9)
        self._lock = threading.Lock()
        self.stats = {"calls": 0, "errors": 0, "prompt_chars": 0, "completion_chars": 0,
                      "input_tokens": 0, "output_tokens": 0, "seconds": 0.0}
        self._opener = self._build_opener(cfg)

    @staticmethod
    def _build_opener(cfg: dict) -> urllib.request.OpenerDirector:
        if not cfg.get("use_proxy", True):
            handler = urllib.request.ProxyHandler({})
        elif cfg.get("proxy_url"):
            handler = urllib.request.ProxyHandler({"http": cfg["proxy_url"], "https": cfg["proxy_url"]})
        else:
            handler = urllib.request.ProxyHandler()
        return urllib.request.build_opener(handler)

    # ------------------------------------------------------------ 低レベル
    def _post_json(self, url: str, payload: dict, headers: dict) -> dict:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST",
                                     headers={"Content-Type": "application/json", **headers})
        try:
            with self._opener.open(req, timeout=self.timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", errors="replace")[:600]
            except Exception:  # noqa: BLE001
                pass
            if e.code in (401, 403):
                raise LLMError(f"認証エラー ({e.code})。API キーを確認してください: {body}") from e
            if e.code == 404:
                raise LLMError(f"エンドポイントまたはモデルが見つかりません (404): {url} / "
                               f"model={self.model}: {body}") from e
            if e.code == 429:
                raise LLMError(f"レート制限 (429)。並列数を下げるか時間を置いてください: {body}") from e
            raise LLMError(f"HTTP {e.code}: {body}") from e
        except urllib.error.URLError as e:
            raise LLMError(f"接続できません: {url} ({e.reason})。Base URL・プロキシ設定を確認してください") from e
        except TimeoutError as e:
            raise LLMError(f"タイムアウト（{self.timeout:.0f} 秒）。max_tokens や件数を減らしてください") from e
        except json.JSONDecodeError as e:
            raise LLMError("応答が JSON ではありません（Base URL が API のルートを指しているか確認）") from e

    def _ensure(self) -> None:
        if self.provider == "builtin":
            raise LLMError("内蔵プロバイダは LLM 呼び出しを行いません")
        if self.provider not in DEFAULT_BASE:
            raise LLMError(f"不明なプロバイダ: {self.provider}")
        if not self.model:
            raise LLMError("LLM のモデル名を設定してください（例: gpt-4o-mini / claude-sonnet-5 / qwen3:14b）")
        if not self.base_url:
            raise LLMError("Base URL を設定してください")

    # ------------------------------------------------------------ chat
    def chat(self, user: str, *, system: str = "", max_tokens: int | None = None,
             temperature: float | None = None, json_mode: bool = False) -> str:
        """1 ターンのチャット補完。応答テキスト（思考タグ除去済み）を返す。"""
        self._ensure()
        max_tokens = int(max_tokens or self.max_tokens)
        temperature = self.temperature if temperature is None else float(temperature)
        t0 = time.time()
        try:
            if self.provider == "anthropic":
                text, usage = self._chat_anthropic(user, system, max_tokens, temperature)
            else:
                text, usage = self._chat_openai(user, system, max_tokens, temperature, json_mode)
        except LLMError:
            with self._lock:
                self.stats["errors"] += 1
            raise
        finally:
            with self._lock:
                self.stats["calls"] += 1
                self.stats["seconds"] += time.time() - t0
                self.stats["prompt_chars"] += len(system) + len(user)
        with self._lock:
            self.stats["completion_chars"] += len(text)
            self.stats["input_tokens"] += int(usage.get("input", 0) or 0)
            self.stats["output_tokens"] += int(usage.get("output", 0) or 0)
        return text

    def _chat_openai(self, user, system, max_tokens, temperature, json_mode):
        messages = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": user}]
        payload: dict = {"model": self.model, "messages": messages, "max_tokens": max_tokens,
                         "temperature": temperature}
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        url = f"{self.base_url}/chat/completions"
        try:
            res = self._post_json(url, payload, headers)
        except LLMError as e:
            # response_format 非対応のサーバ（一部のローカル LLM）は外して再試行
            if json_mode and "response_format" in str(e):
                payload.pop("response_format", None)
                res = self._post_json(url, payload, headers)
            else:
                raise
        try:
            msg = res["choices"][0]["message"]
            text = msg.get("content") or ""
            if not text and msg.get("reasoning_content"):
                text = ""
        except (KeyError, IndexError, TypeError) as e:
            raise LLMError(f"OpenAI 互換応答の形式が想定外です: {json.dumps(res, ensure_ascii=False)[:300]}") from e
        usage = res.get("usage") or {}
        return strip_think(text), {"input": usage.get("prompt_tokens", 0), "output": usage.get("completion_tokens", 0)}

    def _chat_anthropic(self, user, system, max_tokens, temperature):
        payload: dict = {"model": self.model, "max_tokens": max_tokens, "temperature": temperature,
                         "messages": [{"role": "user", "content": user}]}
        if system:
            payload["system"] = system
        headers = {"x-api-key": self.api_key, "anthropic-version": ANTHROPIC_VERSION}
        res = self._post_json(f"{self.base_url}/v1/messages", payload, headers)
        try:
            text = "".join(part.get("text", "") for part in res["content"] if part.get("type") == "text")
        except (KeyError, TypeError) as e:
            raise LLMError(f"Anthropic 応答の形式が想定外です: {json.dumps(res, ensure_ascii=False)[:300]}") from e
        usage = res.get("usage") or {}
        return strip_think(text), {"input": usage.get("input_tokens", 0), "output": usage.get("output_tokens", 0)}

    # ------------------------------------------------------------ 診断
    def test(self) -> dict:
        if self.provider == "builtin":
            return {"ok": True, "message": "内蔵生成（LLM 不使用）: 接続確認は不要です"}
        t0 = time.time()
        try:
            text = self.chat('JSON で {"ok": true} とだけ返答してください。', max_tokens=64, temperature=0.0)
        except LLMError as e:
            return {"ok": False, "message": str(e)}
        return {"ok": True, "message": f"応答あり（{time.time() - t0:.1f} 秒）: {text[:80]}", "model": self.model}
