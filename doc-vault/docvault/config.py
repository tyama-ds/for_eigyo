"""設定（接続プロファイル・データ取り扱いポリシー・RAG 設定）。

接続プロファイルは2つ:
  - local: 社内・ローカルの LLM（Ollama / LM Studio / vLLM / 社内 Azure など）。
           **社外秘（internal）文書を送ってよい接続先**
  - cloud: クラウド LLM（Claude / OpenAI / Gemini 等）。**公開（public）文書だけ**を扱う

文書ごとに機密区分（internal / public）を持ち、LLM 呼び出しは必ず
「扱うデータの区分」を伴う。cloud プロファイルへ internal データを送ろうとすると
llm.PolicyError で止まる（UI 設定の誤りでも漏れない多重防御）。

設定ファイルは api_key を含むためリポジトリに入れない（.gitignore 済み）。
保存先はアプリ直下の docvault.config.json（--config または環境変数 DOCVAULT_CONFIG で変更可）。
"""
from __future__ import annotations

import copy
import json
import threading
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
CONFIG_FILE = BASE / "docvault.config.json"

PROFILE_NAMES = ("local", "cloud")
PROVIDERS = ("openai", "azure", "anthropic")
SECRET_KEYS = ("api_key", "embed_api_key")

DEFAULT_PROFILE: dict = {
    "provider": "openai",      # openai（OpenAI 互換全般）/ azure（Azure OpenAI）/ anthropic（Claude）
    "base_url": "",            # openai: …/v1、azure: https://<res>.openai.azure.com、anthropic: 空で既定
    "api_key": "",
    "model": "",               # azure はデプロイ名
    "ingest_model": "",        # 取り込み（グラフ抽出・要約）用。空なら model を使う
    "api_version": "2024-10-21",   # azure のみ
    "effort": "",              # anthropic のみ（low/medium/high…）。空ならモデル既定
    # 埋め込み（OpenAI 互換 /embeddings または Azure）。anthropic には埋め込み API が無い
    "embed_provider": "openai",
    "embed_base_url": "",      # 空なら base_url（provider が openai/azure のとき）
    "embed_api_key": "",       # 空なら api_key
    "embed_model": "",         # 空なら埋め込みを使わない（BM25 + グラフで検索）
    "max_tokens": 4096,
    "context_window": 32768,
    "timeout": 180.0,
    "use_proxy": False,        # False なら環境変数のプロキシも無視して直結
    "proxy_url": "",
}

DEFAULT_CONFIG: dict = {
    "profiles": {
        "local": {**DEFAULT_PROFILE, "base_url": "http://127.0.0.1:11434/v1"},
        "cloud": {**DEFAULT_PROFILE, "provider": "anthropic", "model": "claude-opus-5-5",
                  "use_proxy": True},
    },
    # 判定モデル Jev（TypeSafe）を OpenRouter Decisions API 経由で使う設定（公開文書の判定のみ）
    "jev": {
        "endpoint": "https://openrouter.ai/api/alpha/decisions",
        "api_key": "",
        "model": "typesafe/jev-1.14",
        "timeout": 60.0,
        "use_proxy": True,
        "proxy_url": "",
    },
    "policy": {
        # 新規取り込み文書の既定区分（迷ったら社外秘扱い）
        "default_sensitivity": "internal",
        # 公開文書の取り込み処理（グラフ抽出・要約・埋め込み）に使うプロファイル
        "public_ingest_profile": "cloud",
        # 相談の既定モード: auto=社外秘を含むならローカル・公開のみならクラウド /
        # local=常にローカル / cloud=クラウド（社外秘文書は検索対象から外す）
        "chat_mode": "auto",
    },
    "rag": {
        "chunk_size": 900,
        "top_k": 8,
        "graph": True,             # 取り込み時のナレッジグラフ抽出（GraphRAG / LightRAG 系）
        "doc_summary": True,       # 文書要約（RAPTOR 的な上位ノード。要約質問・文書横断に使う）
        "router": True,            # 質問ルーター（検索要否・戦略の判定、会話からの質問補完）
        "corrective": True,        # 根拠不足なら再検索（CRAG）
        "rerank": False,           # LLM による再順位付け（精度↑・呼び出し+1）
        # DecisionRAG（arXiv:2610.35868）: 検索器を使わず、文書の階層（フォルダ→文書→節→段落）を
        # 判定モデルでたどる。自動モードでは事実・比較の質問に使い、Hybrid 検索と融合する
        "decision": True,
        "decision_judge": "llm",   # 判定モデル: llm（ローカル/クラウドの LLM）/ jev（論文で使われた Jev）
        "decision_beam": 4,        # 各段で残す枝の数（ビーム幅 k）
        "decision_budget": 8,      # 1質問あたりの判定（呼び出し）の上限
        "decision_choice": True,   # 最後に候補を選択式で並べ替える（論文の multiple-choice 段）
        "max_graph_chunks": 400,   # 1文書あたりグラフ抽出するチャンク数の上限（費用の上限）
    },
}

_lock = threading.Lock()


def _merge(base: dict, data: dict) -> dict:
    """型が一致するキーだけ上書き（未知キー・型違いは捨てる）。"""
    out = copy.deepcopy(base)
    if not isinstance(data, dict):
        return out
    for key, default in base.items():
        if key not in data:
            continue
        val = data[key]
        if isinstance(default, dict):
            out[key] = _merge(default, val)
        elif isinstance(default, bool):
            if isinstance(val, bool):
                out[key] = val
        elif isinstance(default, (int, float)) and not isinstance(default, bool):
            if isinstance(val, (int, float)) and not isinstance(val, bool):
                out[key] = type(default)(val)
        elif isinstance(val, type(default)):
            out[key] = val
    return out


def load_config() -> dict:
    with _lock:
        data = {}
        if CONFIG_FILE.exists():
            try:
                data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                data = {}
        return _merge(DEFAULT_CONFIG, data)


def _clamp(val, lo, hi, cast):
    try:
        return cast(max(lo, min(hi, float(val))))
    except (TypeError, ValueError):
        return None


def save_config(update: dict) -> dict:
    """UI からの部分更新を検証してマージ・保存する。api_key は空なら変更なし。"""
    cfg = load_config()
    profiles = update.get("profiles") if isinstance(update, dict) else None
    if isinstance(profiles, dict):
        for name in PROFILE_NAMES:
            src = profiles.get(name)
            if not isinstance(src, dict):
                continue
            dst = cfg["profiles"][name]
            for key in ("base_url", "model", "ingest_model", "api_version", "effort",
                        "embed_base_url", "embed_model", "proxy_url"):
                if isinstance(src.get(key), str):
                    dst[key] = src[key].strip()
            for key in ("provider", "embed_provider"):
                if src.get(key) in PROVIDERS:
                    dst[key] = src[key]
            if dst["embed_provider"] == "anthropic":      # 埋め込み API は無い
                dst["embed_provider"] = "openai"
            for key in SECRET_KEYS:
                if isinstance(src.get(key), str) and src[key].strip():
                    dst[key] = src[key].strip()
                if src.get(f"clear_{key}"):
                    dst[key] = ""
            if "use_proxy" in src:
                dst["use_proxy"] = bool(src["use_proxy"])
            for key, lo, hi, cast in (("max_tokens", 256, 128000, int),
                                      ("context_window", 2048, 2_000_000, int),
                                      ("timeout", 5, 3600, float)):
                if key in src:
                    val = _clamp(src[key], lo, hi, cast)
                    if val is not None:
                        dst[key] = val
    policy = update.get("policy") if isinstance(update, dict) else None
    if isinstance(policy, dict):
        if policy.get("default_sensitivity") in ("internal", "public"):
            cfg["policy"]["default_sensitivity"] = policy["default_sensitivity"]
        if policy.get("public_ingest_profile") in PROFILE_NAMES:
            cfg["policy"]["public_ingest_profile"] = policy["public_ingest_profile"]
        if policy.get("chat_mode") in ("auto", "local", "cloud"):
            cfg["policy"]["chat_mode"] = policy["chat_mode"]
    rag = update.get("rag") if isinstance(update, dict) else None
    if isinstance(rag, dict):
        for key in ("graph", "doc_summary", "router", "corrective", "rerank", "decision",
                    "decision_choice"):
            if key in rag:
                cfg["rag"][key] = bool(rag[key])
        for key, lo, hi in (("chunk_size", 300, 4000), ("top_k", 3, 30),
                            ("max_graph_chunks", 10, 5000), ("decision_beam", 1, 10),
                            ("decision_budget", 2, 30)):
            if key in rag:
                val = _clamp(rag[key], lo, hi, int)
                if val is not None:
                    cfg["rag"][key] = val
        if rag.get("decision_judge") in ("llm", "jev"):
            cfg["rag"]["decision_judge"] = rag["decision_judge"]
    jev = update.get("jev") if isinstance(update, dict) else None
    if isinstance(jev, dict):
        for key in ("endpoint", "model", "proxy_url"):
            if isinstance(jev.get(key), str):
                cfg["jev"][key] = jev[key].strip()
        if isinstance(jev.get("api_key"), str) and jev["api_key"].strip():
            cfg["jev"]["api_key"] = jev["api_key"].strip()
        if jev.get("clear_api_key"):
            cfg["jev"]["api_key"] = ""
        if "use_proxy" in jev:
            cfg["jev"]["use_proxy"] = bool(jev["use_proxy"])
        if "timeout" in jev:
            val = _clamp(jev["timeout"], 5, 600, float)
            if val is not None:
                cfg["jev"]["timeout"] = val
    with _lock:
        CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    return cfg


def public_config(cfg: dict) -> dict:
    """api_key を伏せた、UI へ返してよい形。"""
    out = copy.deepcopy(cfg)
    for prof in out["profiles"].values():
        for key in SECRET_KEYS:
            prof[f"has_{key}"] = bool(prof.get(key))
            prof.pop(key, None)
    out["jev"]["has_api_key"] = bool(cfg["jev"].get("api_key"))
    out["jev"].pop("api_key", None)
    for name in PROFILE_NAMES:
        out["profiles"][name]["chat_ready"] = chat_ready(cfg, name)
        out["profiles"][name]["embed_ready"] = embed_ready(cfg, name)
    return out


def chat_ready(cfg: dict, profile: str) -> bool:
    p = cfg["profiles"].get(profile) or {}
    if not p.get("model"):
        return False
    if p.get("provider") == "anthropic":
        return True                     # base_url 省略可（API キーは環境変数でも可）
    return bool(p.get("base_url"))


def embed_ready(cfg: dict, profile: str) -> bool:
    p = cfg["profiles"].get(profile) or {}
    if not p.get("embed_model"):
        return False
    if p.get("embed_base_url"):
        return True
    return p.get("provider") in ("openai", "azure") and bool(p.get("base_url"))


def ingest_profile_for(cfg: dict, sensitivity: str) -> str | None:
    """文書の取り込み処理（LLM 呼び出し）に使うプロファイル。使えなければ None。"""
    if sensitivity == "public":
        pref = cfg["policy"]["public_ingest_profile"]
        for name in (pref, "local" if pref == "cloud" else "cloud"):
            if chat_ready(cfg, name):
                return name
        return None
    return "local" if chat_ready(cfg, "local") else None


def embed_profile_for(cfg: dict, sensitivity: str) -> str | None:
    if sensitivity == "public":
        pref = cfg["policy"]["public_ingest_profile"]
        for name in (pref, "local" if pref == "cloud" else "cloud"):
            if embed_ready(cfg, name):
                return name
        return None
    return "local" if embed_ready(cfg, "local") else None


def embed_space(cfg: dict, profile: str) -> str:
    """埋め込み空間の識別子（同じ空間のベクトル同士しか比較できない）。"""
    p = cfg["profiles"][profile]
    return f"{profile}:{p.get('embed_model', '')}"
