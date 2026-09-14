"""前処理: 列ロールの検証、学習用サンプルの生成、分割、エンコーダ（JSON 保存可能）。"""
from __future__ import annotations

import math
import random
from collections import Counter, defaultdict

from .dataio import is_missing, parse_number

ROLES = ("target", "text", "numeric", "categorical", "ignore")
MAX_CAT_LEVELS = 200
UNK = 0


class PrepError(Exception):
    """設定の不備（ユーザーに表示するメッセージ）。"""


# ---------------------------------------------------------------- spec

def build_spec(table: dict, spec_in: dict) -> dict:
    """UI からの設定を検証し、正規化した spec を返す。"""
    columns = table["columns"]
    target = spec_in.get("target")
    if not target or target not in columns:
        raise PrepError("目的変数（予測したい列）を選んでください")
    task = spec_in.get("task") or "classification"
    if task not in ("regression", "classification"):
        raise PrepError(f"不明なタスク種別: {task}")
    roles_in = spec_in.get("roles") or {}
    roles = {}
    for c in columns:
        r = roles_in.get(c, "ignore")
        if c == target:
            r = "target"
        elif r == "target" or r not in ROLES:
            r = "ignore"
        roles[c] = r
    text_cols = [c for c in columns if roles[c] == "text"]
    num_cols = [c for c in columns if roles[c] == "numeric"]
    cat_cols = [c for c in columns if roles[c] == "categorical"]
    if not (text_cols or num_cols or cat_cols):
        raise PrepError("説明変数（テキスト / 数値 / カテゴリ）の列が 1 つもありません")
    split_in = spec_in.get("split") or {}
    try:
        val = float(split_in.get("val", 0.15))
        test = float(split_in.get("test", 0.15))
        seed = int(split_in.get("seed", 42))
    except (TypeError, ValueError) as e:
        raise PrepError("分割比率・シードは数値で指定してください") from e
    if not (0 <= val < 0.9 and 0 <= test < 0.9 and val + test < 0.9):
        raise PrepError("検証・テストの比率は合計 90% 未満にしてください")
    return {
        "target": target, "task": task, "roles": roles,
        "text_cols": text_cols, "num_cols": num_cols, "cat_cols": cat_cols,
        "split": {"val": val, "test": test, "seed": seed,
                  "stratify": bool(split_in.get("stratify", True))},
    }


def _join_text(row: list, names: list[str], idxs: list[int]) -> str:
    if len(idxs) == 1:
        v = row[idxs[0]]
        return "" if is_missing(v) else str(v).strip()
    parts = []
    for name, i in zip(names, idxs, strict=True):
        v = row[i]
        parts.append(f"{name}: {'' if is_missing(v) else str(v).strip()}")
    return "\n".join(parts)


def make_examples(table: dict, spec: dict, require_target: bool = True) -> list[dict]:
    """行 → {i, text, num, cat, y}。目的変数が欠損/数値化不能な行は除く。"""
    columns = table["columns"]
    idx = {c: i for i, c in enumerate(columns)}
    t_idx = idx.get(spec["target"]) if spec.get("target") in idx else None
    text_idx = [idx[c] for c in spec["text_cols"] if c in idx]
    text_names = [c for c in spec["text_cols"] if c in idx]
    num_idx = [idx.get(c) for c in spec["num_cols"]]
    cat_idx = [idx.get(c) for c in spec["cat_cols"]]
    regression = spec["task"] == "regression"
    out = []
    for row_i, row in enumerate(table["rows"]):
        y = None
        if require_target:
            y_raw = row[t_idx]
            if is_missing(y_raw):
                continue
            if regression:
                y = parse_number(y_raw)
                if y is None:
                    continue
            else:
                y = str(y_raw).strip()
        out.append({
            "i": row_i,
            "text": _join_text(row, text_names, text_idx) if text_idx else "",
            "num": [parse_number(row[i]) if i is not None else None for i in num_idx],
            "cat": [("" if i is None or is_missing(row[i]) else str(row[i]).strip()) for i in cat_idx],
            "y": y,
        })
    if require_target and not out:
        raise PrepError("目的変数が有効な行がありません（欠損や数値化できない値ばかりです）")
    return out


# ---------------------------------------------------------------- 分割

def split_indices(n: int, val: float, test: float, seed: int,
                  labels: list | None = None) -> dict:
    """train / val / test のインデックス。labels があれば層化分割。"""
    rng = random.Random(seed)
    groups: dict = defaultdict(list)
    if labels is None:
        groups["_all"] = list(range(n))
    else:
        for i, lab in enumerate(labels):
            groups[lab].append(i)
    train, va, te = [], [], []
    for _lab, members in sorted(groups.items(), key=lambda kv: str(kv[0])):
        rng.shuffle(members)
        g = len(members)
        n_test = int(round(g * test))
        n_val = int(round(g * val))
        if g - n_test - n_val < 1:          # 各クラス最低 1 件は学習に残す
            n_val = max(0, min(n_val, g - 1 - n_test))
            if g - n_test - n_val < 1:
                n_test = max(0, g - 1 - n_val)
        te.extend(members[:n_test])
        va.extend(members[n_test:n_test + n_val])
        train.extend(members[n_test + n_val:])
    for part in (train, va, te):
        rng.shuffle(part)
    if not train:
        raise PrepError("学習データが 0 件になります。分割比率を見直してください")
    return {"train": train, "val": va, "test": te}


# ---------------------------------------------------------------- エンコーダ

class LabelEncoder:
    def __init__(self, classes: list[str] | None = None):
        self.classes = list(classes or [])
        self._index = {c: i for i, c in enumerate(self.classes)}

    def fit(self, labels: list[str]) -> LabelEncoder:
        cnt = Counter(labels)
        self.classes = [c for c, _ in sorted(cnt.items(), key=lambda kv: (-kv[1], kv[0]))]
        self._index = {c: i for i, c in enumerate(self.classes)}
        return self

    def transform(self, label: str) -> int:
        return self._index.get(label, -1)

    def to_dict(self) -> dict:
        return {"classes": self.classes}

    @classmethod
    def from_dict(cls, d: dict) -> LabelEncoder:
        return cls(d.get("classes", []))


class NumericScaler:
    """列ごとの標準化。欠損は学習データ平均で補完（標準化後 0）。"""

    def __init__(self, means=None, stds=None):
        self.means = list(means or [])
        self.stds = list(stds or [])

    def fit(self, rows: list[list[float | None]]) -> NumericScaler:
        if not rows:
            return self
        k = len(rows[0])
        self.means, self.stds = [], []
        for j in range(k):
            vals = [r[j] for r in rows if r[j] is not None]
            mean = sum(vals) / len(vals) if vals else 0.0
            var = sum((v - mean) ** 2 for v in vals) / max(len(vals) - 1, 1) if vals else 0.0
            std = math.sqrt(var)
            self.means.append(mean)
            self.stds.append(std if std > 1e-12 else 1.0)
        return self

    def transform(self, row: list[float | None]) -> list[float]:
        out = []
        for j, v in enumerate(row):
            if v is None or j >= len(self.means):
                out.append(0.0)
            else:
                z = (v - self.means[j]) / self.stds[j]
                out.append(max(-10.0, min(10.0, z)))   # 外れ値でヘッドが壊れないようクリップ
        return out

    def to_dict(self) -> dict:
        return {"means": self.means, "stds": self.stds}

    @classmethod
    def from_dict(cls, d: dict) -> NumericScaler:
        return cls(d.get("means"), d.get("stds"))


class CategoricalVocab:
    """列ごとの値→ID。0 は未知/欠損。頻度上位 MAX_CAT_LEVELS 件のみ保持。"""

    def __init__(self, vocabs: list[dict] | None = None):
        self.vocabs = vocabs or []

    def fit(self, rows: list[list[str]]) -> CategoricalVocab:
        if not rows:
            return self
        k = len(rows[0])
        self.vocabs = []
        for j in range(k):
            cnt = Counter(r[j] for r in rows if r[j] != "")
            top = [v for v, _ in cnt.most_common(MAX_CAT_LEVELS)]
            self.vocabs.append({v: i + 1 for i, v in enumerate(top)})
        return self

    @property
    def cardinalities(self) -> list[int]:
        return [len(v) + 1 for v in self.vocabs]

    def transform(self, row: list[str]) -> list[int]:
        return [self.vocabs[j].get(v, UNK) if j < len(self.vocabs) else UNK for j, v in enumerate(row)]

    def to_dict(self) -> dict:
        return {"vocabs": self.vocabs}

    @classmethod
    def from_dict(cls, d: dict) -> CategoricalVocab:
        return cls(d.get("vocabs"))


class TargetScaler:
    def __init__(self, mean: float = 0.0, std: float = 1.0):
        self.mean, self.std = mean, std

    def fit(self, ys: list[float]) -> TargetScaler:
        self.mean = sum(ys) / len(ys)
        var = sum((y - self.mean) ** 2 for y in ys) / max(len(ys) - 1, 1)
        self.std = math.sqrt(var) if var > 1e-18 else 1.0
        return self

    def transform(self, y: float) -> float:
        return (y - self.mean) / self.std

    def inverse(self, z: float) -> float:
        return z * self.std + self.mean

    def to_dict(self) -> dict:
        return {"mean": self.mean, "std": self.std}

    @classmethod
    def from_dict(cls, d: dict) -> TargetScaler:
        return cls(d.get("mean", 0.0), d.get("std", 1.0))


def class_weights(y_idx: list[int], n_classes: int) -> list[float]:
    """逆頻度の重み（平均 1 に正規化）。"""
    cnt = Counter(y_idx)
    raw = [len(y_idx) / (n_classes * cnt.get(c, 1)) for c in range(n_classes)]
    mean = sum(raw) / len(raw)
    return [w / mean for w in raw]


class Preproc:
    """spec + 各エンコーダをまとめて保存/復元する。"""

    def __init__(self, spec: dict):
        self.spec = spec
        self.labels = LabelEncoder()
        self.num = NumericScaler()
        self.cat = CategoricalVocab()
        self.target = TargetScaler()

    def fit(self, train_examples: list[dict]) -> Preproc:
        if self.spec["task"] == "classification":
            self.labels.fit([e["y"] for e in train_examples])
        else:
            self.target.fit([e["y"] for e in train_examples])
        if self.spec["num_cols"]:
            self.num.fit([e["num"] for e in train_examples])
        if self.spec["cat_cols"]:
            self.cat.fit([e["cat"] for e in train_examples])
        return self

    def encode_y(self, y):
        if self.spec["task"] == "classification":
            return self.labels.transform(y)
        return self.target.transform(y)

    def to_dict(self) -> dict:
        return {"spec": self.spec, "labels": self.labels.to_dict(), "num": self.num.to_dict(),
                "cat": self.cat.to_dict(), "target": self.target.to_dict()}

    @classmethod
    def from_dict(cls, d: dict) -> Preproc:
        p = cls(d["spec"])
        p.labels = LabelEncoder.from_dict(d.get("labels", {}))
        p.num = NumericScaler.from_dict(d.get("num", {}))
        p.cat = CategoricalVocab.from_dict(d.get("cat", {}))
        p.target = TargetScaler.from_dict(d.get("target", {}))
        return p
