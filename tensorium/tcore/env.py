"""依存ライブラリ・デバイスの診断。"""
from __future__ import annotations

import importlib.util
import os
import platform
import sys
from importlib import metadata

LIBS = [
    ("torch", "torch", "PyTorch — 全ファミリーの学習に必須（ベースライン以外）"),
    ("transformers", "transformers", "Hugging Face Transformers — BERT 系 / SentenceBERT に必須"),
    ("sentence_transformers", "sentence-transformers", "SentenceBERT（任意。無くても transformers で平均プーリング）"),
    ("numpy", "numpy", "数値計算（torch と一緒に入る）"),
    ("openpyxl", "openpyxl", "XLSX 読み込み（任意。無くても内蔵リーダーで読める）"),
    ("fugashi", "fugashi", "日本語 BERT（tohoku-nlp 系）のトークナイザに必要"),
    ("unidic_lite", "unidic-lite", "fugashi 用辞書（tohoku-nlp v2/v3 系）"),
    ("ipadic", "ipadic", "fugashi 用辞書（旧 tohoku / sonoisa 系）"),
    ("sentencepiece", "sentencepiece", "DeBERTa / LUKE / GLuCoSE 等のトークナイザに必要"),
    ("accelerate", "accelerate", "任意"),
]

INSTALL_HINTS = [
    {"label": "CPU 版 PyTorch（GPU なしの PC）", "cmd": "pip install torch --index-url https://download.pytorch.org/whl/cpu"},
    {"label": "CUDA 版 PyTorch（NVIDIA GPU あり）", "cmd": "pip install torch"},
    {"label": "Transformers / SentenceBERT / XLSX", "cmd": "pip install transformers sentence-transformers openpyxl"},
    {"label": "日本語 BERT（東北大 v3 系）", "cmd": "pip install fugashi unidic-lite"},
    {"label": "DeBERTa / LUKE 等", "cmd": "pip install sentencepiece"},
]


def _version(dist: str) -> str | None:
    try:
        return metadata.version(dist)
    except metadata.PackageNotFoundError:
        return None


def check_libs() -> list[dict]:
    out = []
    for mod, dist, desc in LIBS:
        installed = importlib.util.find_spec(mod) is not None
        out.append({"module": mod, "dist": dist, "installed": installed,
                    "version": _version(dist) if installed else None, "desc": desc})
    return out


def check_device(pref: str = "auto") -> dict:
    info: dict = {"torch": False, "cuda": False, "mps": False, "selected": "cpu", "gpu_name": None,
                  "cpu_count": os.cpu_count()}
    if importlib.util.find_spec("torch") is None:
        return info
    try:
        import torch
    except Exception as e:  # noqa: BLE001 — 壊れたインストールでも UI に理由を出す
        info["error"] = f"torch のインポートに失敗: {e}"
        return info
    info["torch"] = True
    info["torch_version"] = torch.__version__
    info["threads"] = torch.get_num_threads()
    try:
        info["cuda"] = torch.cuda.is_available()
        if info["cuda"]:
            info["gpu_name"] = torch.cuda.get_device_name(0)
            info["gpu_count"] = torch.cuda.device_count()
    except Exception:  # noqa: BLE001
        info["cuda"] = False
    try:
        info["mps"] = bool(getattr(torch.backends, "mps", None) and torch.backends.mps.is_available())
    except Exception:  # noqa: BLE001
        info["mps"] = False
    info["selected"] = resolve_device(pref, info)
    return info


def resolve_device(pref: str, info: dict | None = None) -> str:
    info = info or check_device("cpu")
    if pref == "cuda" and info.get("cuda"):
        return "cuda"
    if pref == "mps" and info.get("mps"):
        return "mps"
    if pref == "auto":
        if info.get("cuda"):
            return "cuda"
        if info.get("mps"):
            return "mps"
    return "cpu"


def env_payload(cfg: dict) -> dict:
    libs = check_libs()
    have = {lib["module"] for lib in libs if lib["installed"]}
    return {
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "libs": libs,
        "device": check_device(cfg.get("device", "auto")),
        "families_available": {
            "hf": {"torch", "transformers"} <= have,
            "sbert": {"torch", "transformers"} <= have,
            "scratch": "torch" in have,
            "tabular": "torch" in have,
            "baseline": True,
        },
        "install_hints": INSTALL_HINTS,
        "hf_offline_env": bool(os.environ.get("HF_HUB_OFFLINE")),
        "hf_endpoint_env": os.environ.get("HF_ENDPOINT", ""),
        "proxy_env": os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or "",
    }
