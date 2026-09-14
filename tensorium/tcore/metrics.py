"""回帰・分類の評価指標（純 Python）。"""
from __future__ import annotations

import math
import random


def _mean(xs):
    return sum(xs) / len(xs) if xs else float("nan")


def regression_metrics(y_true: list[float], y_pred: list[float]) -> dict:
    n = len(y_true)
    if n == 0:
        return {"n": 0}
    err = [p - t for t, p in zip(y_true, y_pred, strict=True)]
    abs_err = [abs(e) for e in err]
    mse = sum(e * e for e in err) / n
    mean_t = _mean(y_true)
    ss_tot = sum((t - mean_t) ** 2 for t in y_true)
    ss_res = sum(e * e for e in err)
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0
    nonzero = [(t, p) for t, p in zip(y_true, y_pred, strict=True) if abs(t) > 1e-12]
    mape = 100.0 * _mean([abs((p - t) / t) for t, p in nonzero]) if len(nonzero) >= n / 2 else None
    mean_p = _mean(y_pred)
    cov = sum((t - mean_t) * (p - mean_p) for t, p in zip(y_true, y_pred, strict=True))
    var_p = sum((p - mean_p) ** 2 for p in y_pred)
    pearson = cov / math.sqrt(ss_tot * var_p) if ss_tot > 0 and var_p > 0 else 0.0
    srt = sorted(abs_err)
    return {
        "n": n,
        "rmse": math.sqrt(mse),
        "mae": sum(abs_err) / n,
        "median_ae": srt[n // 2],
        "max_error": srt[-1],
        "r2": r2,
        "mape": mape,
        "pearson": pearson,
        "y_mean": mean_t,
        "y_std": math.sqrt(ss_tot / max(n - 1, 1)),
    }


def classification_metrics(y_true: list[int], y_pred: list[int], classes: list[str],
                           probs: list[list[float]] | None = None) -> dict:
    n = len(y_true)
    k = len(classes)
    if n == 0:
        return {"n": 0}
    cm = [[0] * k for _ in range(k)]
    for t, p in zip(y_true, y_pred, strict=True):
        if 0 <= t < k and 0 <= p < k:
            cm[t][p] += 1
    per_class = []
    f1_sum = prec_sum = rec_sum = 0.0
    f1_w = 0.0
    for c in range(k):
        tp = cm[c][c]
        fp = sum(cm[r][c] for r in range(k)) - tp
        fn = sum(cm[c]) - tp
        support = sum(cm[c])
        prec = tp / (tp + fp) if tp + fp > 0 else 0.0
        rec = tp / (tp + fn) if tp + fn > 0 else 0.0
        f1 = 2 * prec * rec / (prec + rec) if prec + rec > 0 else 0.0
        per_class.append({"class": classes[c], "precision": prec, "recall": rec, "f1": f1,
                          "support": support})
        f1_sum += f1
        prec_sum += prec
        rec_sum += rec
        f1_w += f1 * support
    acc = sum(cm[c][c] for c in range(k)) / n
    out = {
        "n": n,
        "accuracy": acc,
        "f1_macro": f1_sum / k,
        "f1_weighted": f1_w / n,
        "precision_macro": prec_sum / k,
        "recall_macro": rec_sum / k,
        "confusion": cm,
        "classes": classes,
        "per_class": per_class,
    }
    if probs:
        eps = 1e-7
        ll = 0.0
        for t, pr in zip(y_true, probs, strict=True):
            p = min(max(pr[t] if 0 <= t < len(pr) else eps, eps), 1 - eps)
            ll -= math.log(p)
        out["log_loss"] = ll / n
        if k == 2:
            out["auc"] = binary_auc([1 if t == 1 else 0 for t in y_true], [pr[1] for pr in probs])
            out["positive_class"] = classes[1]
    return out


def binary_auc(y: list[int], score: list[float]) -> float:
    """Mann–Whitney 統計量による ROC-AUC（同値は 0.5 で扱う）。"""
    pos = sum(y)
    neg = len(y) - pos
    if pos == 0 or neg == 0:
        return 0.5
    order = sorted(range(len(y)), key=lambda i: score[i])
    ranks = [0.0] * len(y)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and score[order[j + 1]] == score[order[i]]:
            j += 1
        avg = (i + j) / 2 + 1
        for t in range(i, j + 1):
            ranks[order[t]] = avg
        i = j + 1
    rank_sum = sum(r for r, yy in zip(ranks, y, strict=True) if yy == 1)
    return (rank_sum - pos * (pos + 1) / 2) / (pos * neg)


def downsample(indices: list[int], max_n: int = 2000, seed: int = 0) -> list[int]:
    if len(indices) <= max_n:
        return list(indices)
    rng = random.Random(seed)
    return sorted(rng.sample(list(indices), max_n))


def primary_metric(task: str, m: dict) -> tuple[str, float | None]:
    """早期終了・モデル比較に使う代表指標（大きいほど良い方向に揃える）。"""
    if task == "regression":
        return "rmse", (-m["rmse"] if m.get("rmse") is not None else None)
    return "f1_macro", m.get("f1_macro")
