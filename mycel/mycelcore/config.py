"""設定の保存と読み込み（``mycel/mycel.config.json``、.gitignore 済み）。

API キーを含むためリポジトリには入れない。UI へ返すときはキーを伏せる。

LLM は次の 2 方式に対応する。
- ``openai``: OpenAI 互換 API（OpenAI / Ollama / LM Studio / vLLM / llama.cpp server など）
- ``azure``: Azure OpenAI（``base_url`` はリソースの URL、``model`` はデプロイ名）
"""
from __future__ import annotations

import getpass
import json
import threading
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
CONFIG_FILE = BASE / "mycel.config.json"
DEFAULT_VAULT = BASE / "vault"


def _default_user() -> str:
    try:
        return getpass.getuser()
    except Exception:
        return "me"


DEFAULT_CONFIG: dict = {
    # Vault（ノートを置くフォルダ）。空なら mycel/vault
    "vault_path": "",
    # 作成者名。変更ジャーナルや将来の共有で「誰が変えたか」に使う
    "user_name": "",
    # デイリーノートとテンプレートの置き場所（Vault からの相対フォルダ）
    "daily_folder": "日報",
    "template_folder": "テンプレート",
    # 有効にするプラグイン（plugins/ フォルダのファイル名）
    "plugins": ["change_journal"],
    # ---- LLM
    "provider": "openai",
    "base_url": "",
    "api_key": "",
    "model": "",
    "api_version": "2024-10-21",        # Azure のみ
    # 埋め込み。空欄は base_url / api_key を流用。embed_model が空なら AI 検索はキーワード方式
    "embed_model": "",
    "embed_base_url": "",
    "embed_api_key": "",
    "temperature": 0.2,
    "max_tokens": 2048,
    "request_timeout": 120.0,
    # プロキシ。use_proxy=False なら環境変数のプロキシも無視して直結
    "use_proxy": False,
    "proxy_url": "",
}

_SECRET_KEYS = ("api_key", "embed_api_key")
_STR_KEYS = ("vault_path", "user_name", "daily_folder", "template_folder", "base_url",
             "model", "api_version", "embed_model", "embed_base_url", "proxy_url")
_lock = threading.Lock()


def load_config(path: Path | None = None) -> dict:
    path = path or CONFIG_FILE
    with _lock:
        cfg = json.loads(json.dumps(DEFAULT_CONFIG))
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                data = {}
            if isinstance(data, dict):
                for key, default in DEFAULT_CONFIG.items():
                    val = data.get(key)
                    if val is None:
                        continue
                    if isinstance(default, float) and isinstance(val, int):
                        val = float(val)
                    if isinstance(val, type(default)):
                        cfg[key] = val
        if not cfg["user_name"]:
            cfg["user_name"] = _default_user()
        return cfg


def save_config(update: dict, path: Path | None = None) -> dict:
    """UI からの部分更新を検証して保存する。"""
    path = path or CONFIG_FILE
    cfg = load_config(path)
    for key in _STR_KEYS:
        if isinstance(update.get(key), str):
            cfg[key] = update[key].strip()
    if update.get("provider") in ("openai", "azure"):
        cfg["provider"] = update["provider"]
    # API キーは空文字なら「変更なし」（UI に平文を返さないため）。clear_* で消去
    for key in _SECRET_KEYS:
        if isinstance(update.get(key), str) and update[key].strip():
            cfg[key] = update[key].strip()
        if update.get("clear_" + key):
            cfg[key] = ""
    if "use_proxy" in update:
        cfg["use_proxy"] = bool(update["use_proxy"])
    if isinstance(update.get("plugins"), list):
        cfg["plugins"] = [p for p in update["plugins"] if isinstance(p, str) and p.isidentifier()]
    for key, lo, hi, cast in (("temperature", 0.0, 2.0, float),
                              ("max_tokens", 64, 65536, int),
                              ("request_timeout", 5.0, 3600.0, float)):
        if key in update:
            try:
                cfg[key] = cast(max(lo, min(hi, float(update[key]))))
            except (TypeError, ValueError):
                pass
    with _lock:
        path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    return cfg


def public_config(cfg: dict) -> dict:
    out = {k: v for k, v in cfg.items() if k not in _SECRET_KEYS}
    out["has_api_key"] = bool(cfg.get("api_key"))
    out["has_embed_api_key"] = bool(cfg.get("embed_api_key"))
    return out


def vault_path(cfg: dict) -> Path:
    return Path(cfg["vault_path"]).expanduser() if cfg.get("vault_path") else DEFAULT_VAULT


def chat_configured(cfg: dict) -> bool:
    return bool(cfg.get("base_url") and cfg.get("model"))


def embed_configured(cfg: dict) -> bool:
    return bool(cfg.get("embed_model") and (cfg.get("embed_base_url") or cfg.get("base_url")))
