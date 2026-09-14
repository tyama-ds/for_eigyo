"""ベースライン（平均値 / 最頻値）。torch 不要。"""
from __future__ import annotations

import time
from collections import Counter

from ..prep import Preproc
from ..runs import load_meta, new_run_id, read_json, run_dir, save_meta, write_json
from .datasetup import build_meta, evaluate_split, prepare


def train_baseline(table: dict, req: dict, job) -> dict:
    started = time.time()
    b = prepare(table, req, job.log)
    job.update(pct=20.0, phase="ベースラインを計算中")
    ex, split = b["examples"], b["split"]
    if b["task"] == "regression":
        ys = [ex[i]["y"] for i in split["train"]]
        const = sum(ys) / len(ys)
        state = {"kind": "mean", "value": const}
        predict = lambda idx: ([const] * len(idx), None)  # noqa: E731
    else:
        cnt = Counter(b["y_enc"][i] for i in split["train"])
        n = sum(cnt.values())
        dist = [cnt.get(c, 0) / n for c in range(b["n_out"])]
        major = max(range(b["n_out"]), key=lambda c: dist[c])
        state = {"kind": "majority", "class_index": major, "dist": dist}
        predict = lambda idx: ([major] * len(idx), [dist] * len(idx))  # noqa: E731
    evals = {}
    for part in ("val", "test"):
        preds, probs = predict(split[part])
        evals[part] = evaluate_split(b, split[part], preds, probs)
    run_id = new_run_id()
    d = run_dir(run_id, create=True)
    write_json(d / "preproc.json", b["preproc"].to_dict())
    write_json(d / "arch.json", {"family": "baseline", "task": b["task"], "state": state})
    meta = build_meta(b, run_id, req, {"eval": evals, "curves": [], "step_losses": [], "device": "cpu",
                                        "n_params": 0, "best_epoch": 0, "epochs_run": 0}, started)
    save_meta(run_id, meta)
    job.update(pct=100.0, phase="完了")
    job.log(f"保存: {run_id}")
    return {"run_id": run_id, "metrics": meta["metrics"], "primary_metric": meta["primary_metric"]}


class BaselinePredictor:
    def __init__(self, run_id: str):
        d = run_dir(run_id)
        self.meta = load_meta(run_id)
        self.preproc = Preproc.from_dict(read_json(d / "preproc.json"))
        self.arch = read_json(d / "arch.json")
        self.spec = self.preproc.spec

    def predict_examples(self, examples: list[dict]) -> tuple[list, list | None]:
        st = self.arch["state"]
        if st["kind"] == "mean":
            return [st["value"]] * len(examples), None
        return [st["class_index"]] * len(examples), [st["dist"]] * len(examples)
