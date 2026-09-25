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
    # ---- データ拡張（知識蒸留）に使う教師 LLM
    "llm_provider": "openai",    # openai（OpenAI 互換）/ anthropic / builtin（LLM なしの内蔵生成）
    "llm_base_url": "",          # 空なら既定（OpenAI: https://api.openai.com/v1 / Anthropic: https://api.anthropic.com）
    "llm_model": "",             # 例: gpt-4o-mini / claude-sonnet-5 / qwen3:14b
    "llm_api_key": "",
    "llm_max_tokens": 4096,
    "llm_timeout": 180,
    "llm_temperature": 0.9,
    "llm_concurrency": 2,
}
SECRET_KEYS = ("hf_token", "llm_api_key")


def load_settings() -> dict:
    cfg = dict(DEFAULTS)
    try:
        cfg.update(json.loads(CONFIG_FILE.read_text(encoding="utf-8")))
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return cfg


def _write_private(path: Path, text: str) -> None:
    """0600 で原子的に書く（API キーを含むため他ユーザーから読めないようにする）。"""
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)


def save_settings(update: dict) -> dict:
    cfg = load_settings()
    for k, v in update.items():
        if k.endswith("_clear") and k[:-6] in SECRET_KEYS:      # 例: llm_api_key_clear: true
            if v:
                cfg[k[:-6]] = ""
            continue
        if k not in DEFAULTS:
            continue
        if k in SECRET_KEYS and (v is None or v == ""):
            continue                      # 空 / null なら変更なし
        if k.endswith("_base_url") and v:
            from .llm import LLMError, validate_base_url
            try:
                v = validate_base_url(str(v))
            except LLMError as e:
                raise ValueError(str(e)) from e
        if isinstance(DEFAULTS[k], bool):
            v = bool(v)
        elif isinstance(DEFAULTS[k], float):
            try:
                v = float(v)
            except (TypeError, ValueError):
                v = DEFAULTS[k]
        elif isinstance(DEFAULTS[k], int):
            try:
                v = int(v)
            except (TypeError, ValueError):
                v = DEFAULTS[k]
        else:
            v = str(v)
        cfg[k] = v
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    _write_private(CONFIG_FILE, json.dumps(cfg, ensure_ascii=False, indent=2) + "\n")
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
