"""コーパスとインデックスの永続化（data/ 以下の JSON ファイル）。

- corpus.json          … 文書（[{id,title,text}]）と改訂番号 rev
- index_<engine>.json  … エンジンごとの構築済みインデックス（corpus_rev を記録）
外部エンジンは自前の working_dir（data/external/<engine>/）を持つ。
"""
from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
DATA_DIR = BASE / "data"
CORPUS_FILE = DATA_DIR / "corpus.json"

_lock = threading.Lock()
_ENGINE_ID_RE = re.compile(r"^[a-z0-9\-]{1,40}$")


def _index_file(engine_id: str) -> Path:
    if not _ENGINE_ID_RE.match(engine_id):
        raise ValueError(f"不正なエンジンIDです: {engine_id!r}")
    return DATA_DIR / f"index_{engine_id}.json"


# ---------------------------------------------------------------- corpus

def load_corpus() -> dict:
    with _lock:
        if CORPUS_FILE.exists():
            try:
                data = json.loads(CORPUS_FILE.read_text(encoding="utf-8"))
                if isinstance(data, dict) and isinstance(data.get("docs"), list):
                    data.setdefault("rev", 1)
                    return data
            except (OSError, ValueError):
                pass
        return {"rev": 0, "docs": []}


def save_corpus(docs: list[dict], rev: int) -> dict:
    data = {"rev": rev, "updated_at": time.time(), "docs": docs}
    with _lock:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        CORPUS_FILE.write_text(
            json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    return data


def add_docs(new_docs: list[dict]) -> dict:
    """文書を追加する。id 重複時は上書き。"""
    corpus = load_corpus()
    by_id = {d["id"]: d for d in corpus["docs"]}
    for doc in new_docs:
        by_id[doc["id"]] = doc
    return save_corpus(list(by_id.values()), corpus["rev"] + 1)


def delete_doc(doc_id: str) -> dict:
    corpus = load_corpus()
    docs = [d for d in corpus["docs"] if d["id"] != doc_id]
    if len(docs) == len(corpus["docs"]):
        return corpus
    return save_corpus(docs, corpus["rev"] + 1)


def next_doc_id(corpus: dict) -> str:
    used = {d["id"] for d in corpus["docs"]}
    n = 1
    while f"d{n}" in used:
        n += 1
    return f"d{n}"


# ---------------------------------------------------------------- index

def save_index(engine_id: str, index: dict, corpus_rev: int) -> None:
    index = dict(index)
    index["corpus_rev"] = corpus_rev
    path = _index_file(engine_id)
    with _lock:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(index, ensure_ascii=False), encoding="utf-8")


def load_index(engine_id: str) -> dict | None:
    path = _index_file(engine_id)
    with _lock:
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else None
        except (OSError, ValueError):
            return None


def index_status(engine_id: str, corpus_rev: int) -> dict:
    """UI 表示用: インデックスの有無・鮮度・統計。"""
    index = load_index(engine_id)
    if index is None:
        return {"built": False}
    return {
        "built": True,
        "built_at": index.get("built_at"),
        "corpus_rev": index.get("corpus_rev"),
        "stale": index.get("corpus_rev") != corpus_rev,
        "stats": index.get("stats") or {},
        "warnings": index.get("warnings") or [],
    }
