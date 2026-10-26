"""LLM API クライアント（標準ライブラリのみ）。

- openai: POST {base_url}/chat/completions, {base_url}/embeddings
- azure:  POST {base_url}/openai/deployments/{model}/chat/completions?api-version=...
          （キーは ``api-key`` ヘッダ）
- 推論モデルの <think>…</think> は回答から除去する（rag-orchestrator と同じ扱い）
"""
from __future__ import annotations

import json
import os
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .config import effective_llm

AUDIT_PATH: Path | None = None     # 外部 API への送信記録（.mycel/external_llm.jsonl）。app が Vault を開くときに設定
BACKEND_LABEL = {"local": "ローカル LLM", "external": "外部 API"}

# 推論モデル（o1 / o3 / o3-mini / o4-mini / gpt-5 / gpt-5-mini / gpt-5-nano など）。
# temperature・top_p を受け付けず、max_tokens ではなく max_completion_tokens を使う。gpt-5-chat-latest は通常のチャットモデル
_REASONING_RE = re.compile(r"^(o[1-9](?:[.-]|$)|gpt-5(?!-chat)|gpt-oss)", re.IGNORECASE)
_OPENAI_HOSTS = (".openai.com", ".openai.azure.com", ".cognitiveservices.azure.com", ".services.ai.azure.com")


def is_reasoning_model(model: str) -> bool:
    return bool(_REASONING_RE.match((model or "").strip()))


def payload_style(cfg: dict, model: str) -> dict:
    """モデル・接続先ごとの送り方。
    tokens_key: OpenAI / Azure と推論モデルは max_completion_tokens（max_tokens は非推奨で、推論モデルは拒否する）。
                ローカルの OpenAI 互換サーバ（Ollama / LM Studio / vLLM）は max_tokens
    temperature: 推論モデルには送らない（既定値 1 以外を拒否する）
    reasoning_effort: 推論モデルで設定があれば送る（gpt-5 系: minimal/low/medium/high、o 系: low/medium/high）"""
    host = _host_of(cfg.get("base_url", "")).lower()
    reasoning = is_reasoning_model(model)
    official = cfg.get("provider") == "azure" or host.endswith(_OPENAI_HOSTS)
    effort = (cfg.get("reasoning_effort") or "").strip().lower()
    gpt5 = (model or "").lower().startswith("gpt-5") and not (model or "").lower().startswith("gpt-5-chat")
    verbosity = (cfg.get("verbosity") or "").strip().lower()
    return {"tokens_key": "max_completion_tokens" if (official or reasoning) else "max_tokens",
            "temperature": not reasoning, "reasoning": reasoning,
            "reasoning_effort": effort if (reasoning and effort and effort != "auto") else "",
            "verbosity": verbosity if (gpt5 and verbosity in ("low", "medium", "high")) else ""}


def _param_problem(msg: str) -> str | None:
    """API のエラー文から、受け付けられなかったパラメータを推定する（モデルごとの差を自動で吸収するため）。"""
    low = (msg or "").lower()
    if "max_tokens" in low and ("max_completion_tokens" in low or "unsupported" in low or "not supported" in low):
        return "max_tokens"
    if "max_completion_tokens" in low and ("unrecognized" in low or "unknown" in low or "unsupported" in low or "not supported" in low or "extra" in low):
        return "max_completion_tokens"
    if "reasoning_effort" in low or "reasoning.effort" in low:
        return "reasoning_effort"
    if "temperature" in low and ("unsupported" in low or "not supported" in low or "only the default" in low):
        return "temperature"
    if "top_p" in low and ("unsupported" in low or "not supported" in low):
        return "top_p"
    if "verbosity" in low and ("unrecognized" in low or "unknown" in low or "unsupported" in low or "not supported" in low):
        return "verbosity"
    if "'system'" in low or "system message" in low or ("role" in low and "system" in low and ("unsupported" in low or "not supported" in low)):
        return "system"
    return None

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


def _env_proxy() -> str:
    for k in ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"):
        if os.environ.get(k):
            return os.environ[k]
    return ""


def _host_of(url: str) -> str:
    try:
        return urllib.parse.urlsplit(url).hostname or ""
    except ValueError:
        return ""


class LLMClient:
    """接続先（ローカル / 外部）は設定から解決する。外部 API は必ずプロキシ経由で、送信を記録する。"""

    def __init__(self, cfg: dict, backend: str | None = None):
        cfg = effective_llm(cfg, backend)
        self.cfg = cfg
        self.backend = cfg.get("_backend", "local")
        self.host = _host_of(cfg.get("base_url", ""))
        if self.backend == "external":
            proxy_url = cfg.get("proxy_url") or _env_proxy()
            if not proxy_url:
                raise LLMError("外部 API はプロキシ経由でのみ送信します。設定の「外部 API」でプロキシ URL を入力するか、"
                               "環境変数 HTTPS_PROXY を設定してください（直接の送信は行いません）")
            proxy = urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url})
        elif not cfg.get("use_proxy"):
            proxy = urllib.request.ProxyHandler({})
        elif cfg.get("proxy_url"):
            proxy = urllib.request.ProxyHandler({"http": cfg["proxy_url"], "https": cfg["proxy_url"]})
        else:
            proxy = urllib.request.ProxyHandler()
        self._opener = urllib.request.build_opener(proxy)

    def _audit(self, kind: str, payload: dict, ok: bool, error: str = "") -> None:
        """外部 API への送信を 1 行ずつ記録する（内容は残さず、何を・どれだけ送ったかだけ）。"""
        if self.backend != "external" or AUDIT_PATH is None:
            return
        task = ""
        for m in payload.get("messages") or []:
            c = m.get("content")
            text = c if isinstance(c, str) else " ".join(p.get("text", "") for p in c if isinstance(p, dict)) if isinstance(c, list) else ""
            mt = re.search(r"\[TASK:(\w+)\]", text)
            if mt:
                task = mt.group(1)
        rec = {"ts": time.time(), "kind": kind, "task": task, "model": self.cfg.get("model", ""), "host": self.host,
               "chars": len(json.dumps(payload, ensure_ascii=False)), "ok": ok}
        if error:
            rec["error"] = error[:200]
        try:
            AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
            with AUDIT_PATH.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except OSError:
            pass

    # ------------------------------------------------------------ URL と認証
    def _endpoint(self, kind: str) -> tuple[str, dict, str]:
        """(URL, ヘッダ, payload に入れる model 名) を返す。kind は chat / embed。"""
        cfg = self.cfg
        if kind in ("chat", "responses"):
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
        path = {"chat": "chat/completions", "responses": "responses", "embed": "embeddings"}[kind]
        if cfg.get("provider") == "azure":
            if base.endswith("/openai"):
                base = base[: -len("/openai")]
            api_ver = cfg.get("api_version") or "2024-10-21"
            if kind in ("chat", "responses") and is_reasoning_model(model) and api_ver[:10] < "2024-12-01":
                api_ver = "2025-04-01-preview"          # 推論モデルは古い GA 版の api-version では使えない
            if kind == "responses" and api_ver[:10] < "2025-03-01":
                api_ver = "2025-04-01-preview"          # Responses API は 2025-03-01-preview 以降
            ver = urllib.parse.quote(api_ver)
            if kind == "responses":
                url = f"{base}/openai/responses?api-version={ver}"      # Azure の Responses API はデプロイ名を body の model に入れる
            else:
                url = f"{base}/openai/deployments/{urllib.parse.quote(model)}/{path}?api-version={ver}"
            if key:
                headers["api-key"] = key
        else:
            url = f"{base}/{path}"
            if key:
                headers["Authorization"] = f"Bearer {key}"
        return url, headers, model

    def _post(self, url: str, headers: dict, payload: dict) -> dict:
        p = url.split("?")[0]
        kind = "embed" if p.endswith("/embeddings") else "responses" if p.endswith("/responses") else "chat"
        try:
            data = self._post_raw(url, headers, payload)
        except LLMError as e:
            self._audit(kind, payload, False, str(e))
            raise
        self._audit(kind, payload, True)
        return data

    def _post_raw(self, url: str, headers: dict, payload: dict) -> dict:
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
    # ------------------------------------------------------------ Responses API（/responses）
    @staticmethod
    def _to_responses_input(messages: list[dict]) -> tuple[str, list[dict]]:
        """Chat 形式の messages を Responses API の instructions / input に変換する（画像は input_image）。"""
        instructions, items = [], []
        for m in messages:
            role, c = m.get("role", "user"), m.get("content")
            if role == "system":
                instructions.append(c if isinstance(c, str) else " ".join(p.get("text", "") for p in c if isinstance(p, dict)))
                continue
            if isinstance(c, list):
                parts = []
                for part in c:
                    if not isinstance(part, dict):
                        continue
                    if part.get("type") == "text":
                        parts.append({"type": "input_text" if role == "user" else "output_text", "text": part.get("text", "")})
                    elif part.get("type") == "image_url":
                        u = part.get("image_url")
                        parts.append({"type": "input_image", "image_url": u.get("url") if isinstance(u, dict) else u})
                items.append({"role": role, "content": parts})
            else:
                items.append({"role": role, "content": [{"type": "input_text" if role == "user" else "output_text", "text": c or ""}]})
        return "\n\n".join(instructions), items

    @staticmethod
    def _responses_text(data: dict) -> tuple[str, str]:
        """Responses API の応答から本文を取り出す。(本文, 途中で切れた理由) を返す。"""
        texts = []
        for item in data.get("output") or []:
            if item.get("type") != "message":
                continue
            for part in item.get("content") or []:
                if part.get("type") in ("output_text", "text") and part.get("text"):
                    texts.append(part["text"])
                elif part.get("type") == "refusal" and part.get("refusal"):
                    texts.append(part["refusal"])
        if not texts and isinstance(data.get("output_text"), str):
            texts.append(data["output_text"])
        reason = ""
        if data.get("status") == "incomplete":
            reason = (data.get("incomplete_details") or {}).get("reason", "incomplete")
        return "\n".join(texts).strip(), reason

    def _chat_responses(self, messages: list[dict], model: str, style: dict, budget: int, temperature: float | None) -> str:
        url, headers, _ = self._endpoint("responses")
        instructions, items = self._to_responses_input(messages)
        payload: dict = {"model": model, "input": items, "max_output_tokens": budget, "store": False}   # store=false: サーバ側に会話を残さない
        if instructions:
            payload["instructions"] = instructions
        if style["temperature"]:
            payload["temperature"] = float(self.cfg.get("temperature", 0.2) if temperature is None else temperature)
        if style["reasoning_effort"]:
            payload["reasoning"] = {"effort": style["reasoning_effort"]}
        if style["verbosity"]:
            payload["text"] = {"verbosity": style["verbosity"]}
        self.last_payload_keys = sorted(k for k in payload if k != "input")
        for _ in range(4):
            try:
                data = self._post(url, headers, payload)
            except LLMError as e:
                problem = _param_problem(str(e)) if "HTTP 400" in str(e) else None
                if problem == "temperature" and "temperature" in payload:
                    payload.pop("temperature")
                elif problem == "reasoning_effort" and "reasoning" in payload:
                    payload.pop("reasoning")
                elif problem == "verbosity" and "text" in payload:
                    payload.pop("text")
                elif "store" in str(e).lower() and ("unrecognized" in str(e).lower() or "unknown" in str(e).lower() or "unsupported" in str(e).lower()) and "store" in payload:
                    payload.pop("store")
                else:
                    raise
                self.last_adjustments.append(problem)
                self.last_payload_keys = sorted(k for k in payload if k != "input")
                continue
            if isinstance(data.get("error"), dict):
                raise LLMError(f"LLM API エラー: {data['error'].get('message', data['error'])}")
            text, reason = self._responses_text(data)
            text = strip_think(text)
            if not text and reason == "max_output_tokens" and payload["max_output_tokens"] < 16384:
                payload["max_output_tokens"] = min(16384, payload["max_output_tokens"] * 4)
                self.last_adjustments.append(f"max_output_tokens={payload['max_output_tokens']}")
                continue
            if not text and reason:
                raise LLMError(f"応答が空のまま終わりました（{reason}）。設定の Max tokens を増やすか、推論の深さを下げてください")
            return text
        raise LLMError("LLM の応答を得られませんでした（Responses API）")

    def chat(self, messages: list[dict] | str, *, system: str = "",
             max_tokens: int | None = None, temperature: float | None = None) -> str:
        if isinstance(messages, str):
            messages = [{"role": "user", "content": messages}]
        if system:
            messages = [{"role": "system", "content": system}] + messages
        self.last_adjustments: list[str] = []
        if (self.cfg.get("api_style") or "chat") == "responses":
            model = self.cfg.get("model", "")
            style = payload_style(self.cfg, model)
            budget = int(max_tokens or self.cfg.get("max_tokens") or 2048)
            if style["reasoning"]:
                budget = max(budget, 4096)
            return self._chat_responses(messages, model, style, budget, temperature)
        url, headers, model = self._endpoint("chat")
        style = payload_style(self.cfg, model)
        budget = int(max_tokens or self.cfg.get("max_tokens") or 2048)
        if style["reasoning"]:
            budget = max(budget, 4096)                    # 推論トークンも上限に含まれるので少し多めに
        payload: dict = {"messages": messages, style["tokens_key"]: budget}
        if style["temperature"]:
            payload["temperature"] = float(self.cfg.get("temperature", 0.2) if temperature is None else temperature)
        if style["reasoning_effort"]:
            payload["reasoning_effort"] = style["reasoning_effort"]
        if style["verbosity"]:
            payload["verbosity"] = style["verbosity"]
        if self.cfg.get("provider") != "azure":
            payload["model"] = model
        self.last_payload_keys = sorted(k for k in payload if k != "messages")
        data = None
        for _ in range(6):                                # モデルが受け付けないパラメータは API の返事を見て外し、やり直す
            try:
                data = self._post(url, headers, payload)
            except LLMError as e:
                low = str(e).lower()
                if ("v1/responses" in low or "responses api" in low or "/responses" in low) and ("chat/completions" in low or "not supported" in low or "only supported" in low):
                    self.last_adjustments.append("api_style=responses")   # Responses API でしか使えないモデル: 切り替えてやり直す
                    return self._chat_responses(messages, model, style, budget, temperature)
                problem = _param_problem(str(e)) if "HTTP 400" in str(e) else None
                if problem == "max_tokens" and "max_tokens" in payload:
                    payload["max_completion_tokens"] = payload.pop("max_tokens")
                elif problem == "max_completion_tokens" and "max_completion_tokens" in payload:
                    payload["max_tokens"] = payload.pop("max_completion_tokens")
                elif problem in ("temperature", "top_p", "reasoning_effort", "verbosity") and problem in payload:
                    payload.pop(problem)
                elif problem == "system" and payload["messages"] and payload["messages"][0].get("role") == "system":
                    sys_msg = payload["messages"][0]["content"]          # system を受け付けないモデル（古い o1-mini など）: 先頭の user に畳み込む
                    rest = payload["messages"][1:]
                    if rest and isinstance(rest[0].get("content"), str):
                        rest[0] = {**rest[0], "content": f"{sys_msg}\n\n{rest[0]['content']}"}
                    else:
                        rest = [{"role": "user", "content": sys_msg}] + rest
                    payload["messages"] = rest
                else:
                    raise
                self.last_adjustments.append(problem)
                self.last_payload_keys = sorted(k for k in payload if k != "messages")
                continue
            if isinstance(data.get("error"), dict):
                raise LLMError(f"LLM API エラー: {data['error'].get('message', data['error'])}")
            choices = data.get("choices") or []
            msg = (choices[0].get("message") or {}) if choices else {}
            text = strip_think(msg.get("content") or "")
            if not text and choices and choices[0].get("finish_reason") == "length":
                key = "max_completion_tokens" if "max_completion_tokens" in payload else "max_tokens"
                if style["reasoning"] and payload[key] < 16384:           # 推論トークンで使い切った: 上限を増やして 1 回だけやり直す
                    payload[key] = min(16384, payload[key] * 4)
                    self.last_adjustments.append(f"{key}={payload[key]}")
                    continue
                raise LLMError("応答が空のまま上限に達しました（推論トークンで使い切った可能性）。設定の Max tokens を増やすか、推論の深さを下げてください")
            return text
        raise LLMError("LLM の応答を得られませんでした（パラメータの調整を繰り返しても受け付けられませんでした）")

    def list_models(self) -> list[str]:
        """使えるモデル名の一覧（OpenAI 互換の /models、だめなら Ollama の /api/tags）。"""
        base = (self.cfg.get("base_url") or "").rstrip("/")
        if not base:
            raise LLMError("Base URL を入力してください")
        if self.cfg.get("provider") == "azure":
            raise LLMError("Azure OpenAI ではデプロイ名を直接入力してください")
        headers = {"Authorization": f"Bearer {self.cfg['api_key']}"} if self.cfg.get("api_key") else {}
        timeout = min(float(self.cfg.get("request_timeout") or 120.0), 15.0)
        errors = []
        root = re.sub(r"/v1$", "", base)
        for url, key in ((f"{base}/models", "data"), (f"{root}/api/tags", "models")):
            try:
                req = urllib.request.Request(url, headers=headers)
                with self._opener.open(req, timeout=timeout) as res:
                    data = json.loads(res.read().decode("utf-8", errors="replace"))
                rows = data.get(key) or []
                names = sorted({str(r.get("id") or r.get("name") or r.get("model")) for r in rows if isinstance(r, dict)} - {"None"})
                if names:
                    return names
            except (urllib.error.URLError, socket.timeout, OSError, ValueError) as e:
                errors.append(str(e))
        raise LLMError("モデル一覧を取得できません（LLM が起動しているか、URL を確認してください）"
                       + (f": {errors[0]}" if errors else ""))

    def embed(self, texts: list[str]) -> list[list[float]]:
        if self.backend == "external":
            raise LLMError("埋め込み（意味検索）はローカル LLM だけで行います（文書全文を外部に送らないため）")
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
