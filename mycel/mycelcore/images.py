"""画像: VLM（画像も読めるチャットモデル）で説明文（キャプション）を作り、検索・AI の根拠にする。

- 画像は「資料」として読み込まれる（本文は空）。説明を作ると index の captions に入り、本文として扱われる
- 設定 vlm_model（例: Ollama の ``llava`` ``qwen2.5vl`` ``gemma3``）が空なら何もしない
- OpenAI 互換の chat/completions に image_url（data URL）で送る
"""
from __future__ import annotations

import base64
from pathlib import Path

from .config import chat_configured
from .extract import IMAGE_MIME
from .index import Cancelled
from .llm import LLMClient, LLMError

MAX_IMAGE_BYTES = 12 * 1024 * 1024
PROMPT = ("[TASK:caption]\nこの画像を日本語で説明してください。検索と質問の根拠に使うので、次を落とさず簡潔に:\n"
          "1) 何の画像か（図・表・写真・画面・手書きなど）と主題\n2) 読み取れる文字（見出し・ラベル・数値・日付・固有名詞）はできるだけそのまま\n"
          "3) 図表なら軸・系列・傾向・結論\n推測は「推定」と書き、300 字程度にまとめてください。")


def vlm_configured(cfg: dict) -> bool:
    return chat_configured(cfg) and bool((cfg.get("vlm_model") or "").strip())


def _data_url(p: Path) -> str:
    mime = IMAGE_MIME.get(p.suffix.lower(), "image/png")
    data = p.read_bytes()
    if len(data) > MAX_IMAGE_BYTES:
        raise LLMError(f"画像が大きすぎます（{len(data) // 1024 // 1024} MB。12 MB まで）")
    return f"data:{mime};base64," + base64.b64encode(data).decode("ascii")


def caption(app, path: str) -> dict:
    """1 枚の画像を VLM で読み、説明を保存して索引に反映する。"""
    path = app._path(path)
    cfg = app.config()
    if not vlm_configured(cfg):
        raise LLMError("画像を読むモデル（VLM）が未設定です。設定の「LLM」で「画像を読むモデル」を入力してください（例: llava, qwen2.5vl, gemma3）")
    abs_p = app.scope.abs_path(path)
    if not abs_p.is_file() or abs_p.suffix.lower() not in IMAGE_MIME:
        raise LLMError("画像ファイルが見つかりません")
    client = LLMClient({**cfg, "model": cfg["vlm_model"]})
    content = [{"type": "text", "text": PROMPT}, {"type": "image_url", "image_url": {"url": _data_url(abs_p)}}]
    text = client.chat([{"role": "user", "content": content}], temperature=0.1).strip()
    if not text:
        raise LLMError("VLM の応答が空でした")
    app.index.set_caption(path, text, cfg["vlm_model"])
    app.index.refresh(path)
    return {"path": path, "caption": text, "model": cfg["vlm_model"]}


def caption_all(app, prefixes=None, job=None, only_missing: bool = True) -> dict:
    """読み込み済みの画像をまとめて読む（ジョブ）。"""
    todo = [im for im in app.index.images(prefixes) if not (only_missing and im["captioned"])]
    done = errors = 0
    for i, im in enumerate(todo):
        if job is not None:
            if job.cancel.is_set():
                raise Cancelled()
            job.progress("画像を読んでいます（VLM）", i, len(todo), im["path"])
        try:
            caption(app, im["path"])
            done += 1
        except LLMError:
            errors += 1
            if errors >= 3 and errors > i // 2:
                raise
    return {"done": done, "errors": errors, "total": len(todo)}
