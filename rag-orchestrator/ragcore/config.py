"""接続設定の管理（jupyter-local-llm の llmlab.configure と同じ考え方）。

OpenAI 互換エンドポイント（Ollama / LM Studio / vLLM / llama.cpp server 等）に接続する。
設定はアプリフォルダ直下の JSON に保存する（api_key を含むためリポジトリには入れない。
.gitignore 済み）。
"""
from __future__ import annotations

import copy
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
    # DecisionRAG（arXiv:2610.35868）の判定モデル: llm（上の LLM）/ api（判定 API: Jev・OpenAI・Azure）
    "decision_judge": "llm",
    "decision_beam": 4,        # 各段で残す枝の数
    "decision_budget": 8,      # 1質問あたりの判定回数の上限
    # 判定 API（テキストを生成せず確率で答える判定モデル）。接続先ごとに設定を持ち、provider で選ぶ。
    # endpoint・model が空なら既定値（decision_api.PROVIDERS）。外部サービスなので、ローカル LLM（直結）
    # とは別にプロキシを設定する（use_proxy で URL が空なら環境変数、Windows はシステムの設定）
    "decision_api": {
        "provider": "openai",          # openrouter（Jev）/ openai / azure
        "timeout": 60.0,
        "openrouter": {"endpoint": "", "api_key": "", "model": "", "use_proxy": True, "proxy_url": ""},
        "openai": {"endpoint": "", "api_key": "", "model": "", "use_proxy": True, "proxy_url": ""},
        "azure": {"endpoint": "", "api_key": "", "model": "", "api_version": "",
                  "use_proxy": True, "proxy_url": ""},
    },
}
JUDGE_PROVIDERS = ("openrouter", "openai", "azure")

_lock = threading.Lock()


def _merge_api(data) -> dict:
    """decision_api を既定値の上に重ねる（未知のキー・型違いは捨てる）。"""
    out = copy.deepcopy(DEFAULT_CONFIG["decision_api"])
    if not isinstance(data, dict):
        return out
    if data.get("provider") in JUDGE_PROVIDERS:
        out["provider"] = data["provider"]
    if isinstance(data.get("timeout"), (int, float)) and not isinstance(data["timeout"], bool):
        out["timeout"] = float(data["timeout"])
    for name in JUDGE_PROVIDERS:
        src = data.get(name)
        if isinstance(src, dict):
            for key, default in out[name].items():
                if key in src and isinstance(src[key], type(default)):
                    out[name][key] = src[key]
    return out


def _migrate(data: dict) -> dict:
    """古い設定ファイルの読み替え: Jev 専用の jev_* → decision_api の openrouter。"""
    if "decision_api" not in data and any(k.startswith("jev_") for k in data):
        sub = {k[4:]: data[k] for k in ("jev_endpoint", "jev_api_key", "jev_model", "jev_use_proxy",
                                         "jev_proxy_url") if k in data}
        data = {**data, "decision_api": {"provider": "openrouter", "openrouter": sub}}
    if data.get("decision_judge") == "jev":
        data = {**data, "decision_judge": "api"}
    return data


def load_config() -> dict:
    with _lock:
        cfg = copy.deepcopy(DEFAULT_CONFIG)
        if CONFIG_FILE.exists():
            try:
                data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    data = _migrate(data)
                    for key in DEFAULT_CONFIG:
                        if key == "decision_api":
                            cfg[key] = _merge_api(data.get(key))
                        elif key in data and isinstance(data[key], type(DEFAULT_CONFIG[key])):
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
    judge = {"jev": "api"}.get(new_cfg.get("decision_judge"), new_cfg.get("decision_judge"))
    if judge in ("llm", "api"):
        cfg["decision_judge"] = judge
    api = new_cfg.get("decision_api")
    if isinstance(api, dict):
        dst_api = cfg["decision_api"]
        if api.get("provider") in JUDGE_PROVIDERS:
            dst_api["provider"] = api["provider"]
        if "timeout" in api:
            try:
                dst_api["timeout"] = max(5.0, min(600.0, float(api["timeout"])))
            except (TypeError, ValueError):
                pass
        for name in JUDGE_PROVIDERS:
            src = api.get(name)
            if not isinstance(src, dict):
                continue
            dst = dst_api[name]
            for key in ("endpoint", "model", "api_version", "proxy_url"):
                if key in dst and isinstance(src.get(key), str):
                    dst[key] = src[key].strip()
            if isinstance(src.get("api_key"), str) and src["api_key"].strip():
                dst["api_key"] = src["api_key"].strip()
            if src.get("clear_api_key"):
                dst["api_key"] = ""
            if "use_proxy" in src:
                dst["use_proxy"] = bool(src["use_proxy"])
    for key, clear_flag in (("api_key", "clear_api_key"), ("embed_api_key", "clear_embed_api_key")):
        if isinstance(new_cfg.get(key), str) and new_cfg[key].strip():
            cfg[key] = new_cfg[key].strip()
        if new_cfg.get(clear_flag):
            cfg[key] = ""
    if "use_proxy" in new_cfg:
        cfg["use_proxy"] = bool(new_cfg["use_proxy"])
    for key, lo, hi in (("context_window", 512, 1_000_000),
                        ("request_timeout", 5.0, 3600.0),
                        ("max_tokens", 64, 32768), ("decision_beam", 1, 10),
                        ("decision_budget", 2, 30)):
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
    out = {k: copy.deepcopy(v) for k, v in cfg.items() if k not in ("api_key", "embed_api_key")}
    out["has_key"] = bool(cfg["api_key"])
    out["has_embed_key"] = bool(cfg["embed_api_key"])
    for name in JUDGE_PROVIDERS:
        sub = out["decision_api"][name]
        sub["has_api_key"] = bool(sub.get("api_key"))
        sub.pop("api_key", None)
    return out


def chat_configured(cfg: dict) -> bool:
    return bool(cfg["base_url"] and cfg["model"])


def embed_configured(cfg: dict) -> bool:
    """埋め込みが使えそうか（エンドポイント設定があるか）。実疎通は /api/config/test で確認。"""
    return bool((cfg["embed_base_url"] or cfg["base_url"]) and (cfg["embed_model"] or cfg["model"]))
