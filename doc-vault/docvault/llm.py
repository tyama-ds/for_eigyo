"""LLM クライアント（ローカル / クラウドの切替とデータ区分ポリシーの強制）。

対応プロバイダ:
  - openai   : OpenAI 互換 API 全般（Ollama / LM Studio / vLLM / llama.cpp / OpenAI /
               Gemini の OpenAI 互換エンドポイント等）。標準ライブラリの urllib で呼ぶ
  - azure    : Azure OpenAI（/openai/deployments/<deployment>/…?api-version=）
  - anthropic: Claude（公式 SDK ``anthropic`` を使用。``pip install anthropic``）

すべての呼び出しは ``sensitivity``（扱うデータの区分 internal / public）を伴い、
cloud プロファイルへ internal データを送ろうとすると PolicyError を送出する。
"""
from __future__ import annotations

import json
import re
import socket
import threading
import time
import urllib.error
import urllib.request
from typing import Iterator

from .config import chat_ready, embed_ready

# Claude の server-side refusal fallback（"default" 指定）に対応するモデル
_FALLBACK_MODELS = {"claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5", "claude-fable-5-1"}
_NO_LOGPROBS: set = set()        # logprobs を拒否した接続先（base_url, model）
_THINK_BLOCK_RE = re.compile(r"<think(?:ing)?>.*?</think(?:ing)?>", re.DOTALL | re.IGNORECASE)


class LLMError(RuntimeError):
    """接続・API エラー。UI にそのまま表示できる日本語メッセージを持つ。"""


class PolicyError(LLMError):
    """データ区分ポリシー違反（社外秘データをクラウドへ送ろうとした）。"""


def strip_think(text: str) -> str:
    """推論モデルの <think>…</think>（閉じタグだけ・未閉じも）を除去する。"""
    if not text:
        return ""
    out = _THINK_BLOCK_RE.sub("", text)
    low = out.lower()
    for tag in ("</think>", "</thinking>"):
        idx = low.rfind(tag)
        if idx != -1:
            out = out[idx + len(tag):]
            break
    m = re.search(r"<think(?:ing)?>", out, re.IGNORECASE)
    if m:
        out = out[:m.start()]
    return out.strip()


class _ThinkFilter:
    """ストリーミング中の <think>…</think> を取り除くフィルタ。"""

    def __init__(self):
        self.buf = ""
        self.inside = False
        self.started = False

    def feed(self, piece: str) -> str:
        self.buf += piece
        out = ""
        while self.buf:
            low = self.buf.lower()
            if self.inside:
                idx = low.find("</think")
                if idx == -1:
                    self.buf = self.buf[-12:]
                    return out
                end = low.find(">", idx)
                if end == -1:
                    return out
                self.buf = self.buf[end + 1:]
                self.inside = False
                continue
            idx = low.find("<think")
            if idx == -1:
                # 閉じタグだけが来る場合（テンプレートが開始タグを消費）にも備える
                cidx = low.find("</think")
                if cidx != -1 and not self.started:
                    end = low.find(">", cidx)
                    if end == -1:
                        return out
                    self.buf = self.buf[end + 1:]
                    out = ""
                    continue
                safe = len(self.buf) - 8          # タグの途中で切れている可能性を残す
                if safe > 0:
                    out += self.buf[:safe]
                    self.started = self.started or bool(self.buf[:safe].strip())
                    self.buf = self.buf[safe:]
                return out
            out += self.buf[:idx]
            end = low.find(">", idx)
            if end == -1:
                self.buf = self.buf[idx:]
                return out
            self.buf = self.buf[end + 1:]
            self.inside = True
        return out

    def flush(self) -> str:
        rest = "" if self.inside else self.buf
        self.buf = ""
        return rest


class LLM:
    """設定（2プロファイル）を受けて chat / stream / embed を提供する。統計も集計する。"""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self._lock = threading.Lock()
        self.stats = {"calls": {"local": 0, "cloud": 0}, "embed_texts": 0,
                      "prompt_chars": 0, "completion_chars": 0, "seconds": 0.0}

    # ------------------------------------------------------------ ポリシー
    def guard(self, profile: str, sensitivity: str) -> None:
        if profile not in ("local", "cloud"):
            raise LLMError(f"不明なプロファイルです: {profile}")
        if profile == "cloud" and sensitivity != "public":
            raise PolicyError("社外秘（internal）のデータはクラウド LLM へ送信できません"
                              "（文書の区分を「公開」にするか、ローカル LLM を設定してください）")

    def ready(self, profile: str) -> bool:
        return chat_ready(self.cfg, profile)

    def embed_ready(self, profile: str) -> bool:
        return embed_ready(self.cfg, profile)

    def _prof(self, profile: str) -> dict:
        return self.cfg["profiles"][profile]

    def _count(self, profile: str, prompt_chars: int, completion: str, started: float):
        with self._lock:
            self.stats["calls"][profile] += 1
            self.stats["prompt_chars"] += prompt_chars
            self.stats["completion_chars"] += len(completion)
            self.stats["seconds"] += time.time() - started

    # ------------------------------------------------------------ chat
    def chat(self, profile: str, prompt: str | list[dict], *, sensitivity: str,
             system: str = "", max_tokens: int | None = None, purpose: str = "answer") -> str:
        """回答テキスト（think 除去済み）を返す。prompt は文字列か messages 配列。"""
        self.guard(profile, sensitivity)
        if not self.ready(profile):
            raise LLMError(f"{_label(profile)} LLM が未設定です（設定タブで接続先とモデルを登録してください）")
        messages = _as_messages(prompt)
        started = time.time()
        p = self._prof(profile)
        if p["provider"] == "anthropic":
            text = "".join(self._anthropic(p, messages, system, max_tokens, purpose, stream=False))
        else:
            text = self._openai_chat(p, messages, system, max_tokens, purpose)
        text = strip_think(text)
        self._count(profile, sum(len(m["content"]) for m in messages) + len(system), text, started)
        return text

    def stream(self, profile: str, prompt: str | list[dict], *, sensitivity: str,
               system: str = "", max_tokens: int | None = None,
               purpose: str = "answer") -> Iterator[str]:
        """回答をトークン（断片）単位で返すジェネレータ。think は除去される。"""
        self.guard(profile, sensitivity)
        if not self.ready(profile):
            raise LLMError(f"{_label(profile)} LLM が未設定です（設定タブで接続先とモデルを登録してください）")
        messages = _as_messages(prompt)
        p = self._prof(profile)
        started = time.time()
        produced = []
        filt = _ThinkFilter()
        if p["provider"] == "anthropic":
            source = self._anthropic(p, messages, system, max_tokens, purpose, stream=True)
        else:
            source = self._openai_stream(p, messages, system, max_tokens, purpose)
        for piece in source:
            out = filt.feed(piece)
            if out:
                produced.append(out)
                yield out
        rest = filt.flush()
        if rest:
            produced.append(rest)
            yield rest
        self._count(profile, sum(len(m["content"]) for m in messages) + len(system),
                    "".join(produced), started)

    def chat_logprobs(self, profile: str, prompt: str | list[dict], *, sensitivity: str,
                      system: str = "", max_tokens: int | None = None,
                      purpose: str = "router") -> tuple[str, list | None]:
        """(回答テキスト（think 未除去）, トークンごとの logprobs) を返す。

        OpenAI 互換 / Azure は logprobs（上位候補つき）を要求する。Claude や logprobs を
        受け付けない接続先は (テキスト, None)。DecisionRAG の確率スコアに使う。
        """
        self.guard(profile, sensitivity)
        if not self.ready(profile):
            raise LLMError(f"{_label(profile)} LLM が未設定です（設定タブで接続先とモデルを登録してください）")
        p = self._prof(profile)
        key = (p.get("base_url"), p.get("model"))
        if p["provider"] == "anthropic" or key in _NO_LOGPROBS:
            return self.chat(profile, prompt, sensitivity=sensitivity, system=system,
                             max_tokens=max_tokens, purpose=purpose), None
        messages = _as_messages(prompt)
        url, headers, payload = self._payload(p, messages, system, max_tokens, purpose)
        payload["logprobs"] = True
        payload["top_logprobs"] = 10
        started = time.time()
        try:
            data = self._post_adaptive(p, url, payload, headers)
        except LLMError as e:
            if "logprob" not in str(e).lower():
                raise
            _NO_LOGPROBS.add(key)                 # 次回からは logprobs を要求しない
            return self.chat(profile, prompt, sensitivity=sensitivity, system=system,
                             max_tokens=max_tokens, purpose=purpose), None
        choices = data.get("choices") or []
        choice = choices[0] if choices else {}
        text = (choice.get("message") or {}).get("content") or ""
        lp = (choice.get("logprobs") or {}).get("content") if isinstance(choice.get("logprobs"), dict) else None
        self._count(profile, sum(len(m["content"]) for m in messages) + len(system), text, started)
        return text, (lp if isinstance(lp, list) and lp else None)

    # ------------------------------------------------------------ embeddings
    def embed(self, profile: str, texts: list[str], *, sensitivity: str) -> list[list[float]]:
        self.guard(profile, sensitivity)
        if not self.embed_ready(profile):
            raise LLMError(f"{_label(profile)} の埋め込みモデルが未設定です")
        p = self._prof(profile)
        provider = p["embed_provider"] if p["embed_provider"] != "anthropic" else "openai"
        base = (p.get("embed_base_url") or p.get("base_url") or "").rstrip("/")
        key = p.get("embed_api_key") or p.get("api_key") or ""
        model = p["embed_model"]
        out: list[list[float]] = []
        for i in range(0, len(texts), 64):
            batch = [t[:6000] or " " for t in texts[i:i + 64]]
            if provider == "azure":
                url = (f"{base}/openai/deployments/{model}/embeddings"
                       f"?api-version={p.get('api_version') or '2024-10-21'}")
                data = self._post(p, url, {"input": batch}, {"api-key": key} if key else {})
            else:
                data = self._post(p, f"{base}/embeddings", {"model": model, "input": batch},
                                  {"Authorization": f"Bearer {key}"} if key else {})
            rows = data.get("data") or []
            if len(rows) != len(batch):
                raise LLMError(f"埋め込み応答の件数が不正です: {len(rows)} != {len(batch)}")
            rows.sort(key=lambda r: r.get("index", 0))
            for row in rows:
                vec = row.get("embedding")
                if not isinstance(vec, list) or not vec:
                    raise LLMError("埋め込み応答に embedding がありません")
                out.append([float(x) for x in vec])
            with self._lock:
                self.stats["embed_texts"] += len(batch)
        return out

    # ------------------------------------------------------------ OpenAI 互換 / Azure
    @staticmethod
    def _opener(p: dict) -> urllib.request.OpenerDirector:
        if not p.get("use_proxy"):
            handler = urllib.request.ProxyHandler({})
        elif p.get("proxy_url"):
            handler = urllib.request.ProxyHandler({"http": p["proxy_url"], "https": p["proxy_url"]})
        else:
            handler = urllib.request.ProxyHandler()
        return urllib.request.build_opener(handler)

    def _request(self, p: dict, url: str, payload: dict, headers: dict):
        hdrs = {"Content-Type": "application/json", **headers}
        req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                     method="POST", headers=hdrs)
        return self._opener(p).open(req, timeout=float(p.get("timeout") or 180))

    def _post(self, p: dict, url: str, payload: dict, headers: dict) -> dict:
        last: Exception | None = None
        for attempt in range(2):
            try:
                with self._request(p, url, payload, headers) as res:
                    return json.loads(res.read().decode("utf-8", errors="replace"))
            except urllib.error.HTTPError as e:
                raise _http_error(e) from e
            except (urllib.error.URLError, socket.timeout, OSError, ValueError) as e:
                last = e
                if attempt == 0:
                    time.sleep(1.0)
        raise LLMError(f"LLM に接続できません: {last}") from last

    def _chat_target(self, p: dict) -> tuple[str, dict, dict]:
        base = (p.get("base_url") or "").rstrip("/")
        key = p.get("api_key") or ""
        if p["provider"] == "azure":
            url = (f"{base}/openai/deployments/{p['model']}/chat/completions"
                   f"?api-version={p.get('api_version') or '2024-10-21'}")
            return url, ({"api-key": key} if key else {}), {}
        return (f"{base}/chat/completions", ({"Authorization": f"Bearer {key}"} if key else {}),
                {"model": p["model"]})

    def _payload(self, p: dict, messages: list[dict], system: str, max_tokens: int | None,
                 purpose: str) -> tuple[str, dict, dict]:
        url, headers, base_payload = self._chat_target(p)
        msgs = ([{"role": "system", "content": system}] if system else []) + messages
        payload = {**base_payload, "messages": msgs,
                   "max_tokens": int(max_tokens or p.get("max_tokens") or 4096),
                   "temperature": 0.0 if purpose != "answer" else 0.2}
        if purpose == "ingest" and p.get("ingest_model") and p["provider"] != "azure":
            payload["model"] = p["ingest_model"]
        return url, headers, payload

    def _openai_chat(self, p, messages, system, max_tokens, purpose) -> str:
        url, headers, payload = self._payload(p, messages, system, max_tokens, purpose)
        data = self._post_adaptive(p, url, payload, headers)
        choices = data.get("choices") or []
        msg = (choices[0].get("message") or {}) if choices else {}
        return msg.get("content") or ""

    def _post_adaptive(self, p, url, payload, headers) -> dict:
        """新しめの OpenAI モデルが max_tokens / temperature を拒否したら形を変えて再送する。"""
        for _ in range(3):
            try:
                return self._post(p, url, payload, headers)
            except LLMError as e:
                msg = str(e)
                if "max_tokens" in msg and "max_completion_tokens" not in payload:
                    payload["max_completion_tokens"] = payload.pop("max_tokens")
                    continue
                if "temperature" in msg and "temperature" in payload:
                    payload.pop("temperature")
                    continue
                raise
        return self._post(p, url, payload, headers)

    def _openai_stream(self, p, messages, system, max_tokens, purpose) -> Iterator[str]:
        url, headers, payload = self._payload(p, messages, system, max_tokens, purpose)
        payload["stream"] = True
        try:
            res = self._request(p, url, payload, headers)
        except urllib.error.HTTPError as e:
            err = _http_error(e)
            if "stream" in str(err).lower():           # ストリーム非対応サーバ → 一括
                payload.pop("stream", None)
                yield self._openai_chat(p, messages, system, max_tokens, purpose)
                return
            raise err from e
        except (urllib.error.URLError, socket.timeout, OSError) as e:
            raise LLMError(f"LLM に接続できません: {e}") from e
        with res:
            ctype = res.headers.get("Content-Type", "")
            if "event-stream" not in ctype and "ndjson" not in ctype:
                # stream を無視して一括 JSON を返すサーバ
                data = json.loads(res.read().decode("utf-8", errors="replace") or "{}")
                choices = data.get("choices") or []
                yield ((choices[0].get("message") or {}).get("content") or "") if choices else ""
                return
            for raw in res:
                line = raw.decode("utf-8", errors="replace").strip()
                if not line.startswith("data:"):
                    continue
                body = line[5:].strip()
                if body == "[DONE]":
                    break
                try:
                    chunk = json.loads(body)
                except ValueError:
                    continue
                for ch in chunk.get("choices") or []:
                    piece = (ch.get("delta") or {}).get("content")
                    if piece:
                        yield piece

    # ------------------------------------------------------------ Anthropic（公式 SDK）
    def _anthropic(self, p, messages, system, max_tokens, purpose, *, stream: bool) -> Iterator[str]:
        try:
            import anthropic
        except ImportError as e:
            raise LLMError("Claude を使うには公式 SDK が必要です: pip install anthropic") from e
        client_kwargs: dict = {"timeout": float(p.get("timeout") or 180), "max_retries": 2}
        if p.get("api_key"):
            client_kwargs["api_key"] = p["api_key"]       # 空なら環境変数 / ant auth を使う
        if p.get("base_url"):
            client_kwargs["base_url"] = p["base_url"]
        if not p.get("use_proxy"):
            client_kwargs["http_client"] = anthropic.DefaultHttpxClient(trust_env=False)
        elif p.get("proxy_url"):
            client_kwargs["http_client"] = anthropic.DefaultHttpxClient(proxy=p["proxy_url"])
        client = anthropic.Anthropic(**client_kwargs)

        model = p["model"]
        if purpose == "ingest" and p.get("ingest_model"):
            model = p["ingest_model"]
        # Claude は思考にもトークンを使うため、出力上限は余裕を持たせる
        limit = max(int(max_tokens or p.get("max_tokens") or 4096), 16000)
        kwargs: dict = {"model": model, "max_tokens": limit, "messages": messages}
        if system:
            kwargs["system"] = system
        effort = p.get("effort") or ("low" if purpose in ("ingest", "router", "grade") else "")
        if effort:
            kwargs["output_config"] = {"effort": effort}
        use_fallback = model in _FALLBACK_MODELS and not p.get("base_url")
        api = client.beta.messages if use_fallback else client.messages
        if use_fallback:
            # 安全分類器による拒否時、サーバ側で推奨モデルへ自動フォールバックする
            kwargs["betas"] = ["server-side-fallback-2026-07-01"]
            kwargs["extra_body"] = {"fallbacks": "default"}
        try:
            if stream:
                with api.stream(**kwargs) as st:
                    for text in st.text_stream:
                        yield text
                    final = st.get_final_message()
            else:
                final = api.create(**kwargs)
                yield "".join(getattr(b, "text", "") for b in final.content
                              if getattr(b, "type", "") == "text")
        except anthropic.AuthenticationError as e:
            raise LLMError("Claude API の認証に失敗しました（API キーを確認してください）") from e
        except anthropic.RateLimitError as e:
            raise LLMError("Claude API のレート制限に達しました。しばらく待って再実行してください") from e
        except anthropic.APIStatusError as e:
            raise LLMError(f"Claude API エラー HTTP {e.status_code}: {getattr(e, 'message', e)}") from e
        except anthropic.APIConnectionError as e:
            raise LLMError(f"Claude API に接続できません（プロキシ設定を確認）: {e}") from e
        if getattr(final, "stop_reason", "") == "refusal":
            raise LLMError("Claude がこの依頼への回答を辞退しました（安全上の理由）")


def _as_messages(prompt) -> list[dict]:
    if isinstance(prompt, str):
        return [{"role": "user", "content": prompt}]
    out = []
    for m in prompt:
        role = m.get("role")
        if role in ("user", "assistant") and str(m.get("content", "")).strip():
            out.append({"role": role, "content": str(m["content"])})
    # 先頭は user、同じ role の連続は結合（API 制約）
    merged: list[dict] = []
    for m in out:
        if merged and merged[-1]["role"] == m["role"]:
            merged[-1]["content"] += "\n\n" + m["content"]
        else:
            merged.append(dict(m))
    while merged and merged[0]["role"] != "user":
        merged.pop(0)
    return merged or [{"role": "user", "content": " "}]


def _http_error(e: urllib.error.HTTPError) -> LLMError:
    detail = ""
    try:
        detail = e.read().decode("utf-8", errors="replace")[:400]
    except OSError:
        pass
    return LLMError(f"LLM API エラー HTTP {e.code}: {detail or e.reason}")


def _label(profile: str) -> str:
    return "ローカル" if profile == "local" else "クラウド"
