"""設定の読み込み（config/default.json ＋ 実行時設定 pqb.config.json）。

- 「初期値（仮）」はすべて config/default.json にある。コードには埋め込まない。
- LLM / DB の接続情報など人が UI で変えるものは pqb.config.json（git 管理外）に保存し、
  default.json に深いマージで重ねる。
- データ置き場は data/（環境変数 PQB_DATA_DIR で変更可）。
"""
from __future__ import annotations

import copy
import json
import os
import threading
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent          # アプリのフォルダ
CONFIG_DIR = BASE / "config"
RUNTIME_CONFIG_FILE = BASE / "pqb.config.json"
DATA_DIR = Path(os.environ.get("PQB_DATA_DIR") or (BASE / "data"))

_lock = threading.Lock()
_SECRET_KEYS = ("api_key",)


def _deep_merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for key, val in over.items():
        if isinstance(val, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], val)
        else:
            out[key] = copy.deepcopy(val)
    return out


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    return data if isinstance(data, dict) else {}


def load_defaults() -> dict:
    return _read_json(CONFIG_DIR / "default.json")


def load_runtime() -> dict:
    return _read_json(RUNTIME_CONFIG_FILE)


def load_config() -> dict:
    """default.json に pqb.config.json を重ねた実効設定。"""
    with _lock:
        return _deep_merge(load_defaults(), load_runtime())


def save_runtime(partial: dict) -> dict:
    """UI／CLI からの部分更新を実行時設定に保存し、実効設定を返す。

    許可するキーは default.json に存在するものだけ（型も合わせる）。
    api_key は空文字なら「変更なし」扱い（UI に平文を返さないため）。
    """
    defaults = load_defaults()
    with _lock:
        runtime = load_runtime()

        def apply(dst: dict, src: dict, ref: dict) -> None:
            for key, val in src.items():
                if key not in ref or key.startswith("_"):
                    continue
                ref_val = ref[key]
                if isinstance(ref_val, dict):
                    if isinstance(val, dict):
                        dst.setdefault(key, {})
                        apply(dst[key], val, ref_val)
                    continue
                if key in _SECRET_KEYS:
                    if isinstance(val, str) and val.strip():
                        dst[key] = val.strip()
                    continue
                if isinstance(ref_val, bool):
                    dst[key] = bool(val)
                elif isinstance(ref_val, int) and not isinstance(ref_val, bool):
                    try:
                        dst[key] = int(val)
                    except (TypeError, ValueError):
                        continue
                elif isinstance(ref_val, float):
                    try:
                        dst[key] = float(val)
                    except (TypeError, ValueError):
                        continue
                elif isinstance(ref_val, str):
                    dst[key] = str(val).strip() if isinstance(val, str) else str(val)
                elif isinstance(ref_val, list):
                    if isinstance(val, list):
                        dst[key] = val

        apply(runtime, partial, defaults)
        # 明示的なクリア
        for section, flag in (("llm", "clear_api_key"), ("db", "clear_api_key")):
            if isinstance(partial.get(section), dict) and partial[section].get(flag):
                runtime.setdefault(section, {})["api_key"] = ""
        RUNTIME_CONFIG_FILE.write_text(json.dumps(runtime, ensure_ascii=False, indent=2) + "\n",
                                       encoding="utf-8")
    return load_config()


def public_config(cfg: dict) -> dict:
    """api_key を伏せて UI に返せる形。"""
    out = copy.deepcopy(cfg)
    for section in ("llm", "db"):
        sec = out.get(section)
        if isinstance(sec, dict):
            sec["has_api_key"] = bool(sec.get("api_key"))
            sec.pop("api_key", None)
    return out


def load_purposes() -> dict:
    data = _read_json(CONFIG_DIR / "purposes.json")
    return {k: v for k, v in data.items() if not k.startswith("_")}


def purpose_settings(purpose: str) -> dict:
    purposes = load_purposes()
    return purposes.get(purpose) or purposes.get("prior_art") or {
        "label": purpose, "population_range": [200, 1500], "selection": "recall_first",
        "overall_rule": "min_required"}


def list_dialects() -> list[str]:
    return sorted(p.stem for p in (CONFIG_DIR / "dialects").glob("*.json"))


def load_dialect(dialect_id: str) -> dict:
    path = CONFIG_DIR / "dialects" / f"{dialect_id}.json"
    if not path.exists():
        raise KeyError(f"未知の方言です: {dialect_id}")
    return _read_json(path)


def list_csv_dialects() -> list[str]:
    return sorted(p.stem for p in (CONFIG_DIR / "csv_dialects").glob("*.json"))


def load_csv_dialect(dialect_id: str) -> dict:
    path = CONFIG_DIR / "csv_dialects" / f"{dialect_id}.json"
    if not path.exists():
        raise KeyError(f"未知の CSV 方言です: {dialect_id}")
    return _read_json(path)


def data_dir() -> Path:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return DATA_DIR
