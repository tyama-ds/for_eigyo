"""設定（tensorium.config.json）。プロキシ・HF ミラー・デバイス等。"""
from __future__ import annotations

import json
import os
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
CONFIG_FILE = Path(os.environ.get("TENSORIUM_CONFIG_FILE") or BASE / "tensorium.config.json")
# 学習結果の保存先。環境変数 TENSORIUM_DATA_DIR で変更できる（既定: tensorium/data）
DATA_DIR = Path(os.environ.get("TENSORIUM_DATA_DIR") or BASE / "data")

DEFAULTS = {
    "device": "auto",            # auto / cpu / cuda / mps
    "num_threads": 0,            # 0 = torch 既定
    "hf_endpoint": "",           # 例: 社内ミラー https://hf-mirror.example
    "hf_offline": False,         # True なら Hub に接続せずキャッシュ/ローカルのみ
    "hf_cache_dir": "",          # 空なら既定（~/.cache/huggingface）
    "hf_token": "",
    "use_proxy": True,           # False なら環境変数のプロキシも無視
    "proxy_url": "",             # 空なら環境変数 HTTP(S)_PROXY
    "trust_remote_code": False,
}
SECRET_KEYS = ("hf_token",)


def load_settings() -> dict:
    cfg = dict(DEFAULTS)
    try:
        cfg.update(json.loads(CONFIG_FILE.read_text(encoding="utf-8")))
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return cfg


def save_settings(update: dict) -> dict:
    cfg = load_settings()
    for k, v in update.items():
        if k not in DEFAULTS:
            continue
        if k in SECRET_KEYS and v == "":
            continue                      # 空なら変更なし
        if isinstance(DEFAULTS[k], bool):
            v = bool(v)
        elif isinstance(DEFAULTS[k], int):
            try:
                v = int(v)
            except (TypeError, ValueError):
                v = DEFAULTS[k]
        else:
            v = str(v)
        cfg[k] = v
    CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return cfg


def public_settings(cfg: dict | None = None) -> dict:
    cfg = dict(cfg or load_settings())
    for k in SECRET_KEYS:
        cfg[k + "_set"] = bool(cfg.get(k))
        cfg[k] = ""
    return cfg


def apply_env(cfg: dict | None = None) -> None:
    """transformers / huggingface_hub をインポートする前に環境変数へ反映する。"""
    cfg = cfg or load_settings()
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
    os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
    os.environ.setdefault("TRANSFORMERS_VERBOSITY", "error")
    os.environ.setdefault("TRANSFORMERS_NO_ADVISORY_WARNINGS", "1")
    if cfg.get("hf_endpoint"):
        os.environ["HF_ENDPOINT"] = cfg["hf_endpoint"]
    if cfg.get("hf_offline"):
        os.environ["HF_HUB_OFFLINE"] = "1"
        os.environ["TRANSFORMERS_OFFLINE"] = "1"
    else:
        os.environ.pop("HF_HUB_OFFLINE", None)
        os.environ.pop("TRANSFORMERS_OFFLINE", None)
    if cfg.get("hf_cache_dir"):
        os.environ["HF_HOME"] = cfg["hf_cache_dir"]
    if cfg.get("hf_token"):
        os.environ["HF_TOKEN"] = cfg["hf_token"]
    if not cfg.get("use_proxy", True):
        for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
            os.environ.pop(k, None)
    elif cfg.get("proxy_url"):
        for k in ("HTTP_PROXY", "HTTPS_PROXY"):
            os.environ[k] = cfg["proxy_url"]
