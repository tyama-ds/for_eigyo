"""OpenAI 互換エンドポイントへの薄いクライアント（標準ライブラリのみ）。

- chat: POST {base_url}/chat/completions
- embed: POST {embed_base_url or base_url}/embeddings
- 推論モデルの思考過程 <think>…</think> は応答から除去する（llmlab と同じ挙動）
- プロキシ: use_proxy=False は環境変数も無視して直結。True は proxy_url（空なら環境変数）
- 呼び出し回数・文字数をジョブ単位で数えられるよう、クライアントはインスタンスで持つ
"""
from __future__ import annotations

import json
import re
import socket
import threading
import time
import urllib.error
import urllib.request

_THINK_BLOCK_RE = re.compile(r"<think(?:ing)?>(.*?)</think(?:ing)?>",
                             re.DOTALL | re.IGNORECASE)
_THINK_OPEN_RE = re.compile(r"<think(?:ing)?>", re.IGNORECASE)


def split_think(text: str) -> tuple[str, str]:
    """応答を (回答, 推論過程) に分離する。

    Qwen3 / DeepSeek-R1 系の思考トークンの出方すべてに対応する:
    - 閉じた <think>…</think> ブロック（複数可）
    - チャットテンプレートが開始タグを消費し **閉じタグだけ残る** 場合
    - max_tokens 到達などで **閉じタグが出ないまま切れた** 場合（従来は回答に漏れていた）
    """
    if not text:
        return "", ""
    thinks: list[str] = []

    def _collect(m: re.Match) -> str:
        thinks.append(m.group(1).strip())
        return ""

    out = _THINK_BLOCK_RE.sub(_collect, text)
    # 閉じタグだけが残る場合: 最後の閉じタグまでを思考として扱う
    low = out.lower()
    for tag in ("</think>", "</thinking>"):
        idx = low.rfind(tag)
        if idx != -1:
            thinks.append(out[:idx].strip())
            out = out[idx + len(tag):]
            break
    # 未閉じの開始タグ（トークン切れ）: それ以降すべてを思考として扱う
    m = _THINK_OPEN_RE.search(out)
    if m:
        thinks.append(out[m.end():].strip())
        out = out[:m.start()]
    return out.strip(), "\n\n".join(t for t in thinks if t)


def strip_think(text: str) -> str:
    """<think>…</think>（未閉じ含む）を除去した回答テキストを返す。"""
    return split_think(text)[0]


class LLMError(RuntimeError):
    """接続・API エラー。UI にそのまま表示できる日本語メッセージを持つ。"""


class LLMClient:
    """1ジョブ（ingest / query）ごとに生成し、統計を集計する。"""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self._lock = threading.Lock()
        self.stats = {
            "chat_calls": 0, "chat_prompt_chars": 0, "chat_completion_chars": 0,
            "embed_calls": 0, "embed_texts": 0, "llm_seconds": 0.0,
        }
        self._opener = self._build_opener(cfg)

    # ------------------------------------------------------------ 低レベル
    @staticmethod
    def _build_opener(cfg: dict) -> urllib.request.OpenerDirector:
        if not cfg.get("use_proxy"):
            proxy = urllib.request.ProxyHandler({})          # 直結（環境変数も無視）
        elif cfg.get("proxy_url"):
            proxy = urllib.request.ProxyHandler(
                {"http": cfg["proxy_url"], "https": cfg["proxy_url"]})
        else:
            proxy = urllib.request.ProxyHandler()             # 環境変数のプロキシ
        return urllib.request.build_opener(proxy)

    def _post_json(self, url: str, payload: dict, api_key: str) -> dict:
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=body, method="POST", headers=headers)
        timeout = float(self.cfg.get("request_timeout") or 180.0)
        last_err: Exception | None = None
        for attempt in range(2):                              # 一時的な切断は1回だけ再試行
            try:
                start = time.time()
                with self._opener.open(req, timeout=timeout) as res:
                    data = json.loads(res.read().decode("utf-8", errors="replace"))
                with self._lock:
                    self.stats["llm_seconds"] += time.time() - start
                return data
            except urllib.error.HTTPError as e:
                detail = ""
                try:
                    detail = e.read().decode("utf-8", errors="replace")[:300]
                except OSError:
                    pass
                raise LLMError(f"LLM API エラー HTTP {e.code}: {detail or e.reason}") from e
            except (urllib.error.URLError, socket.timeout, OSError, ValueError) as e:
                last_err = e
                if attempt == 0:
                    time.sleep(1.0)
        raise LLMError(f"LLM に接続できません: {last_err}") from last_err

    # ------------------------------------------------------------ chat
    def chat(self, prompt: str, *, system: str = "", max_tokens: int | None = None,
             temperature: float = 0.0, want_think: bool = False):
        """回答文字列を返す。``want_think=True`` なら (回答, 推論過程) のタプル。

        推論過程は content 中の <think>…</think>（未閉じ含む）に加えて、
        reasoning parser 付きサーバ（vLLM / SGLang / LM Studio 等）が返す
        message.reasoning_content / message.reasoning フィールドも回収する。
        """
        cfg = self.cfg
        if not (cfg.get("base_url") and cfg.get("model")):
            raise LLMError("LLM が未設定です（設定タブから Base URL / Model を登録してください）")
        base = cfg["base_url"].rstrip("/")
        messages = ([{"role": "system", "content": system}] if system else [])
        messages.append({"role": "user", "content": prompt})
        payload = {
            "model": cfg["model"],
            "messages": messages,
            "max_tokens": int(max_tokens or cfg.get("max_tokens") or 1600),
            "temperature": temperature,
        }
        data = self._post_json(f"{base}/chat/completions", payload, cfg.get("api_key", ""))
        choices = data.get("choices") or []
        msg = (choices[0].get("message") or {}) if choices else {}
        text, think = split_think(msg.get("content") or "")
        reasoning = str(msg.get("reasoning_content") or msg.get("reasoning") or "").strip()
        if reasoning:
            think = f"{reasoning}\n\n{think}" if think else reasoning
        with self._lock:
            self.stats["chat_calls"] += 1
            self.stats["chat_prompt_chars"] += len(prompt) + len(system)
            self.stats["chat_completion_chars"] += len(text)
        return (text, think) if want_think else text

    # ------------------------------------------------------------ embeddings
    def embed(self, texts: list[str]) -> list[list[float]]:
        cfg = self.cfg
        base = (cfg.get("embed_base_url") or cfg.get("base_url") or "").rstrip("/")
        model = cfg.get("embed_model") or cfg.get("model") or ""
        key = cfg.get("embed_api_key") or cfg.get("api_key") or ""
        if not (base and model):
            raise LLMError("埋め込みが未設定です（設定タブの Embed 欄を確認してください）")
        out: list[list[float]] = []
        for i in range(0, len(texts), 64):                    # まとめて投げすぎない
            batch = texts[i:i + 64]
            data = self._post_json(f"{base}/embeddings",
                                   {"model": model, "input": batch}, key)
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
                self.stats["embed_calls"] += 1
                self.stats["embed_texts"] += len(batch)
        return out
