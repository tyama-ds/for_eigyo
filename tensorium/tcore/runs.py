"""学習結果（run）の保存・一覧・削除。data/runs/<run_id>/ 以下に JSON と重みを置く。"""
from __future__ import annotations

import json
import re
import shutil
import time
import uuid
from pathlib import Path

from .config import DATA_DIR

RUNS_DIR = DATA_DIR / "runs"
_ID_RE = re.compile(r"^[A-Za-z0-9_\-]{4,64}$")

LIST_FIELDS = ("id", "name", "created", "family", "family_name", "model", "model_name", "task", "target",
               "dataset", "metrics", "duration_sec", "device", "n_params", "status", "primary_metric",
               "n_train", "n_val", "n_test", "classes", "spec", "hparams")


def new_run_id() -> str:
    return time.strftime("r%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]


def run_dir(run_id: str, create: bool = False) -> Path:
    if not _ID_RE.match(run_id or ""):
        raise ValueError("不正な run id")
    d = RUNS_DIR / run_id
    if create:
        d.mkdir(parents=True, exist_ok=True)
    return d


def write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1, allow_nan=True), encoding="utf-8")
    tmp.replace(path)


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def save_meta(run_id: str, meta: dict) -> None:
    write_json(run_dir(run_id, create=True) / "meta.json", meta)


def load_meta(run_id: str) -> dict | None:
    p = run_dir(run_id) / "meta.json"
    if not p.exists():
        return None
    return read_json(p)


def list_runs() -> list[dict]:
    out = []
    if not RUNS_DIR.exists():
        return out
    for d in RUNS_DIR.iterdir():
        p = d / "meta.json"
        if not p.exists():
            continue
        try:
            meta = read_json(p)
        except (OSError, json.JSONDecodeError):
            continue
        out.append({k: meta.get(k) for k in LIST_FIELDS})
    out.sort(key=lambda m: m.get("created") or "", reverse=True)
    return out


def delete_run(run_id: str) -> bool:
    d = run_dir(run_id)
    if not d.exists():
        return False
    shutil.rmtree(d)
    return True


def rename_run(run_id: str, name: str) -> dict | None:
    meta = load_meta(run_id)
    if meta is None:
        return None
    meta["name"] = name.strip()[:80] or meta.get("name")
    save_meta(run_id, meta)
    return meta
