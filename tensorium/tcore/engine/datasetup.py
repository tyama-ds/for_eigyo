"""ファミリー共通のデータ準備・評価・メタ情報（torch 非依存）。"""
from __future__ import annotations

import random
import time
from datetime import datetime

from .. import metrics as M
from ..catalog import coerce_hparams
from ..catalog import family as family_info
from ..catalog import preset as preset_info
from ..dataio import histogram
from ..prep import PrepError, Preproc, build_spec, class_weights, make_examples, split_indices

MAX_PLOT_POINTS = 2000
MAX_EXAMPLES = 30
SNIPPET = 140


def prepare(table: dict, req: dict, log=None) -> dict:
    """spec 検証 → サンプル生成 → 分割 → エンコーダ学習。全ファミリー共通。"""
    log = log or (lambda *_: None)
    spec = build_spec(table, req.get("spec") or {})
    fam_id = req.get("family") or "baseline"
    fam = family_info(fam_id)
    if fam is None:
        raise PrepError(f"不明なモデルファミリー: {fam_id}")
    task = spec["task"]
    hp = coerce_hparams(fam_id, task, req.get("hparams"))
    model_id = (req.get("model") or "").strip() or None
    if fam.get("needs_text") and not spec["text_cols"]:
        raise PrepError(f"{fam['name']} にはテキスト列（ロール=テキスト）が 1 つ以上必要です")
    if fam_id in ("hf", "sbert") and not model_id:
        raise PrepError("事前学習モデルの ID（または保存フォルダのパス）を指定してください")
    if fam_id == "tabular" and not (spec["num_cols"] or spec["cat_cols"]):
        raise PrepError("FT-Transformer には数値またはカテゴリの列が必要です")

    examples = make_examples(table, spec)
    n_total = len(examples)
    dropped = table["n_rows"] - n_total
    if dropped:
        log(f"目的変数が欠損/無効の {dropped} 行を除外（有効 {n_total} 行）")

    # 合成データ（table["synthetic_from"] 以降の行）は学習にのみ使い、検証 / テストには混ぜない
    syn_from = table.get("synthetic_from")
    real_idx = [k for k, e in enumerate(examples) if syn_from is None or e["i"] < syn_from]
    syn_idx = [k for k, e in enumerate(examples) if syn_from is not None and e["i"] >= syn_from]
    if not real_idx:
        raise PrepError("実データの行がありません")
    strat = ([examples[k]["y"] for k in real_idx]
             if (task == "classification" and spec["split"]["stratify"]) else None)
    split0 = split_indices(len(real_idx), spec["split"]["val"], spec["split"]["test"], spec["split"]["seed"], strat)
    split = {part: [real_idx[j] for j in idx] for part, idx in split0.items()}
    train_real = list(split["train"])
    if syn_idx:
        rng = random.Random(spec["split"]["seed"])
        split["train"] = split["train"] + syn_idx
        rng.shuffle(split["train"])
        log(f"合成データ {len(syn_idx)} 行を学習データに追加（検証 / テストには含めない）")
    preproc = Preproc(spec).fit([examples[i] for i in split["train"]])

    classes = None
    y_enc: list = []
    if task == "classification":
        classes = preproc.labels.classes
        if len(classes) < 2:
            raise PrepError("分類には目的変数のクラスが 2 種類以上必要です（学習データ内）")
        if len(classes) > 500:
            raise PrepError(f"クラス数が多すぎます（{len(classes)}）。回帰にするか、目的変数を見直してください")
        y_enc = [preproc.labels.transform(e["y"]) for e in examples]
        for part in ("val", "test"):
            before = len(split[part])
            split[part] = [i for i in split[part] if y_enc[i] >= 0]
            if len(split[part]) != before:
                log(f"{part}: 学習データに無いクラスの {before - len(split[part])} 行を除外")
        n_out = len(classes)
    else:
        y_enc = [preproc.target.transform(e["y"]) for e in examples]
        n_out = 1

    cw = None
    if task == "classification" and hp.get("class_weight"):
        cw = class_weights([y_enc[i] for i in split["train"]], n_out)
        log("クラス重み: " + ", ".join(f"{c}={w:.2f}" for c, w in zip(classes, cw, strict=True)))

    log(f"分割: 学習 {len(split['train'])} / 検証 {len(split['val'])} / テスト {len(split['test'])}"
        f"{'（層化）' if strat else ''}")
    if task == "classification":
        log("クラス: " + ", ".join(classes[:12]) + (" …" if len(classes) > 12 else ""))
    return {
        "spec": spec, "family": fam_id, "family_info": fam, "hparams": hp, "model_id": model_id,
        "examples": examples, "split": split, "preproc": preproc, "task": task,
        "classes": classes, "y_enc": y_enc, "n_out": n_out, "class_weights": cw,
        "table_name": table.get("name"), "n_rows": table.get("synthetic_from", table["n_rows"]),
        "n_synthetic": len(syn_idx), "train_real": train_real,
    }


def _snippet(example: dict) -> str:
    t = example.get("text") or ""
    if not t:
        parts = [str(v) for v in (example.get("num") or []) if v is not None][:6]
        parts += [v for v in (example.get("cat") or []) if v][:6]
        t = " / ".join(parts)
    t = t.replace("\n", " ")
    return t[:SNIPPET] + ("…" if len(t) > SNIPPET else "")


def evaluate_split(bundle: dict, idx: list[int], preds: list, probs: list | None = None) -> dict | None:
    """preds: 回帰は元スケールの予測値、分類はクラス index。"""
    if not idx:
        return None
    ex = bundle["examples"]
    task = bundle["task"]
    if task == "regression":
        y_true = [ex[i]["y"] for i in idx]
        m = M.regression_metrics(y_true, preds)
        keep = M.downsample(list(range(len(idx))), MAX_PLOT_POINTS, seed=1)
        points = [[y_true[k], preds[k]] for k in keep]
        resid = [preds[k] - y_true[k] for k in range(len(idx))]
        order = sorted(range(len(idx)), key=lambda k: -abs(resid[k]))[:MAX_EXAMPLES]
        worst = [{"row": ex[idx[k]]["i"] + 2, "text": _snippet(ex[idx[k]]), "y_true": y_true[k],
                  "y_pred": preds[k], "error": resid[k]} for k in order]
        return {"metrics": m, "plot": {"points": points, "residual_hist": histogram(resid, 24)},
                "examples": worst}
    y_true = [bundle["y_enc"][i] for i in idx]
    m = M.classification_metrics(y_true, preds, bundle["classes"], probs)
    wrong = [k for k in range(len(idx)) if preds[k] != y_true[k]]
    conf = (lambda k: probs[k][preds[k]]) if probs else (lambda k: 0.0)
    wrong.sort(key=lambda k: -conf(k))
    worst = [{"row": ex[idx[k]]["i"] + 2, "text": _snippet(ex[idx[k]]),
              "true": bundle["classes"][y_true[k]], "pred": bundle["classes"][preds[k]],
              "conf": conf(k)} for k in wrong[:MAX_EXAMPLES]]
    return {"metrics": m, "plot": {"confusion": m["confusion"], "classes": bundle["classes"]},
            "examples": worst}


def short_model_name(model_id: str | None) -> str:
    if not model_id:
        return ""
    p = preset_info(model_id)
    if p:
        return p["name"]
    return model_id.replace("\\", "/").rstrip("/").split("/")[-1]


def build_meta(bundle: dict, run_id: str, req: dict, extra: dict, started: float) -> dict:
    fam = bundle["family_info"]
    label = fam["name"] if bundle["family"] != "hf" else "FT"
    default_name = f"{short_model_name(bundle['model_id']) or label} → {bundle['spec']['target']}"
    val_eval, test_eval = extra.get("eval", {}).get("val"), extra.get("eval", {}).get("test")
    pm_name, pm_val = M.primary_metric(bundle["task"], val_eval["metrics"]) if val_eval else (None, None)
    meta = {
        "id": run_id,
        "name": (req.get("name") or "").strip()[:80] or default_name,
        "created": datetime.now().isoformat(timespec="seconds"),
        "family": bundle["family"],
        "family_name": fam["name"],
        "model": bundle["model_id"],
        "model_name": short_model_name(bundle["model_id"]),
        "task": bundle["task"],
        "target": bundle["spec"]["target"],
        "dataset": {"name": bundle["table_name"], "n_rows": bundle["n_rows"]},
        "spec": bundle["spec"],
        "hparams": bundle["hparams"],
        "classes": bundle["classes"],
        "n_train": len(bundle["split"]["train"]),
        "n_val": len(bundle["split"]["val"]),
        "n_test": len(bundle["split"]["test"]),
        "n_synthetic": bundle.get("n_synthetic", 0),
        "metrics": {"val": val_eval["metrics"] if val_eval else None,
                    "test": test_eval["metrics"] if test_eval else None},
        "primary_metric": {"name": pm_name, "value": (abs(pm_val) if pm_val is not None else None),
                           "higher_is_better": bundle["task"] != "regression"},
        "duration_sec": round(time.time() - started, 2),
        "status": "done",
    }
    meta.update(extra)
    return meta
