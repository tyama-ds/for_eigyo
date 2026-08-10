"""接続設定の管理（jupyter-local-llm の llmlab.configure と同じ考え方）。

OpenAI 互換エンドポイント（Ollama / LM Studio / vLLM / llama.cpp server 等）に接続する。
設定はアプリフォルダ直下の JSON に保存する（api_key を含むためリポジトリには入れない。
.gitignore 済み）。
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
CONFIG_FILE = BASE / "rag_orchestrator.config.json"

DEFAULT_CONFIG: dict = {
    # チャット（生成）用の OpenAI 互換エンドポイント。例: http://127.0.0.1:11434/v1
    "base_url": "",
    "api_key": "",
    "model": "",
    # 埋め込み。省略時は model / base_url / api_key を流用する。
    # サーバに /v1/embeddings が無い場合は embed_base_url に別サーバを指定する。
    "embed_model": "",
    "embed_base_url": "",
    "embed_api_key": "",
    # 生成パラメータ。max_tokens は推論モデル（Qwen3 等）が思考にトークンを
    # 使うことを見込んだ既定値（小さいと JSON 抽出が出力前に切れる）
    "context_window": 8192,
    "request_timeout": 180.0,
    "max_tokens": 4096,
    # プロキシ。use_proxy=False なら環境変数のプロキシも無視して直結する。
    # use_proxy=True で proxy_url 空なら環境変数（HTTP(S)_PROXY）を使う。
    "use_proxy": False,
    "proxy_url": "",
}

_lock = threading.Lock()


def load_config() -> dict:
    with _lock:
        cfg = dict(DEFAULT_CONFIG)
        if CONFIG_FILE.exists():
            try:
                data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    for key in DEFAULT_CONFIG:
                        if key in data and isinstance(data[key], type(DEFAULT_CONFIG[key])):
                            cfg[key] = data[key]
            except (OSError, ValueError):
                pass
        return cfg


def save_config(new_cfg: dict) -> dict:
    """UI から受けた部分更新を検証してマージ・保存する。"""
    cfg = load_config()
    for key in ("base_url", "model", "embed_model", "embed_base_url", "proxy_url"):
        if key in new_cfg and isinstance(new_cfg[key], str):
            cfg[key] = new_cfg[key].strip()
    # api_key は空文字なら「変更なし」扱い（UI に平文を返さないため）
    for key, clear_flag in (("api_key", "clear_api_key"), ("embed_api_key", "clear_embed_api_key")):
        if isinstance(new_cfg.get(key), str) and new_cfg[key].strip():
            cfg[key] = new_cfg[key].strip()
        if new_cfg.get(clear_flag):
            cfg[key] = ""
    if "use_proxy" in new_cfg:
        cfg["use_proxy"] = bool(new_cfg["use_proxy"])
    for key, lo, hi in (("context_window", 512, 1_000_000),
                        ("request_timeout", 5.0, 3600.0),
                        ("max_tokens", 64, 32768)):
        if key in new_cfg:
            try:
                val = float(new_cfg[key])
            except (TypeError, ValueError):
                continue
            val = max(lo, min(hi, val))
            cfg[key] = int(val) if key != "request_timeout" else val
    with _lock:
        CONFIG_FILE.write_text(
            json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    return cfg


def public_config(cfg: dict) -> dict:
    """api_key を伏せた、UI へ返してよい形。"""
    out = {k: v for k, v in cfg.items() if k not in ("api_key", "embed_api_key")}
    out["has_key"] = bool(cfg["api_key"])
    out["has_embed_key"] = bool(cfg["embed_api_key"])
    return out


def chat_configured(cfg: dict) -> bool:
    return bool(cfg["base_url"] and cfg["model"])


def embed_configured(cfg: dict) -> bool:
    """埋め込みが使えそうか（エンドポイント設定があるか）。実疎通は /api/config/test で確認。"""
    return bool((cfg["embed_base_url"] or cfg["base_url"]) and (cfg["embed_model"] or cfg["model"]))
