"""継続的能動学習（CAL）。企画書 §10.7。

- 特徴: 文字 2〜3 gram の TF-IDF（名称＋要約＋請求の範囲）＋分類コード（全粒度）の二値素性。
  ハッシュ化（crc32）で固定次元にし、標準ライブラリのみで動かす
- 分類器: ロジスティック回帰（SGD、L2、クラス重み balanced）。シード固定で再現可能
- 初期ラベル: 既知文献とプールを正例。負例が無い間は無作為抽出の文献を暫定負例にする
- 出力: 分類器スコア（安い適合度）、係数上位の語・コード、次バッチ、不確実域
"""
from __future__ import annotations

import math
import random
import zlib
from collections import defaultdict

from ..core.document import Document
from ..knowledge import codes as codelib
from ..util import norm_text


def _h(name: str, n: int) -> int:
    return zlib.crc32(name.encode("utf-8")) % n


class CALModel:
    def __init__(self, cfg: dict | None = None, seed: int = 0):
        cfg = cfg or {}
        self.ngram_range = tuple(cfg.get("ngram_range") or (2, 3))
        self.n_features = int(cfg.get("n_features") or 262144)
        self.epochs = int(cfg.get("epochs") or 12)
        self.lr = float(cfg.get("learning_rate") or 0.1)
        self.l2 = float(cfg.get("l2") or 1e-4)
        self.seed = seed
        self.idf: dict[int, float] = {}
        self.w: dict[int, float] = {}
        self.b = 0.0
        self.names: dict[int, str] = {}
        self.fitted = False

    # -------------------------------------------------------------- 特徴
    def _raw(self, doc: Document) -> dict[int, float]:
        text = norm_text(doc.full_text())
        tf: dict[int, float] = defaultdict(float)
        lo, hi = self.ngram_range
        for n in range(lo, hi + 1):
            for i in range(max(0, len(text) - n + 1)):
                g = text[i:i + n]
                key = _h("g:" + g, self.n_features)
                tf[key] += 1.0
                self.names.setdefault(key, g)
        for scheme, code in doc.all_codes():
            for level, value in codelib.levels(scheme, code):
                if level == "raw":
                    continue
                key = _h(f"c:{scheme}:{value}", self.n_features)
                tf[key] += 1.0
                self.names.setdefault(key, f"{scheme} {value}")
        return tf

    def _vec(self, doc: Document) -> dict[int, float]:
        tf = self._raw(doc)
        vec = {k: (1 + math.log(v)) * self.idf.get(k, 1.0) for k, v in tf.items()}
        norm = math.sqrt(sum(x * x for x in vec.values())) or 1.0
        return {k: v / norm for k, v in vec.items()}

    # -------------------------------------------------------------- 学習
    def fit(self, docs: list[Document], labels: list[bool]) -> "CALModel":
        if not docs:
            raise ValueError("学習データが空です")
        raws = [self._raw(d) for d in docs]
        df: dict[int, int] = defaultdict(int)
        for r in raws:
            for k in r:
                df[k] += 1
        N = len(docs)
        self.idf = {k: math.log((N + 1) / (c + 1)) + 1.0 for k, c in df.items()}
        vecs = []
        for r in raws:
            vec = {k: (1 + math.log(v)) * self.idf[k] for k, v in r.items()}
            norm = math.sqrt(sum(x * x for x in vec.values())) or 1.0
            vecs.append({k: v / norm for k, v in vec.items()})
        n_pos = sum(1 for y in labels if y)
        n_neg = N - n_pos
        w_pos = N / (2 * n_pos) if n_pos else 1.0
        w_neg = N / (2 * n_neg) if n_neg else 1.0
        self.w, self.b = {}, 0.0
        rng = random.Random(self.seed)
        order = list(range(N))
        for epoch in range(self.epochs):
            rng.shuffle(order)
            lr = self.lr / (1 + 0.3 * epoch)
            for i in order:
                x, y = vecs[i], 1.0 if labels[i] else 0.0
                z = self.b + sum(self.w.get(k, 0.0) * v for k, v in x.items())
                p = 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, z))))
                g = (p - y) * (w_pos if labels[i] else w_neg)
                for k, v in x.items():
                    self.w[k] = self.w.get(k, 0.0) * (1 - lr * self.l2) - lr * g * v
                self.b -= lr * g
        self.fitted = True
        return self

    # -------------------------------------------------------------- 推論
    def score(self, doc: Document) -> float:
        if not self.fitted:
            raise ValueError("未学習です")
        x = self._vec(doc)
        z = self.b + sum(self.w.get(k, 0.0) * v for k, v in x.items())
        return 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, z))))

    def rank(self, docs: list[Document]) -> list[tuple[str, float]]:
        scored = [(d.doc_id, self.score(d)) for d in docs]
        scored.sort(key=lambda x: (-x[1], x[0]))
        return scored

    def next_batch(self, docs: list[Document], k: int, exclude: set[str] | None = None) -> list[str]:
        exclude = exclude or set()
        return [d for d, _ in self.rank(docs) if d not in exclude][:k]

    def uncertain(self, docs: list[Document], low: float = 0.4, high: float = 0.6) -> list[str]:
        return [d for d, s in self.rank(docs) if low <= s <= high]

    def top_features(self, n: int = 20) -> list[dict]:
        items = sorted(self.w.items(), key=lambda kv: -kv[1])[:n]
        return [{"feature": self.names.get(k, str(k)), "weight": round(v, 4),
                 "kind": "code" if self.names.get(k, "").split(" ")[0] in ("FI", "FT", "IPC") else "ngram"}
                for k, v in items if v > 0]


def cal_round(population: list[Document], labels: dict[str, bool], cfg: dict | None = None, *,
              seed: int = 0, k: int = 20, provisional_negatives: int = 100) -> dict:
    """1 ラウンド: 学習 → 未判定文献をスコア順に → 上位 k 件を次バッチとして返す。"""
    cfg = cfg or {}
    pos_ids = {d for d, y in labels.items() if y}
    neg_ids = {d for d, y in labels.items() if not y}
    by_id = {d.doc_id: d for d in population}
    provisional = False
    if not neg_ids:
        rng = random.Random(seed)
        unl = [d for d in by_id if d not in pos_ids]
        rng.shuffle(unl)
        neg_ids = set(unl[:provisional_negatives])
        provisional = True
    train_ids = [d for d in list(pos_ids) + list(neg_ids) if d in by_id]
    if not any(d in pos_ids for d in train_ids) or not any(d in neg_ids for d in train_ids):
        return {"ok": False, "reason": "正例または負例がありません", "scores": {}, "batch": [], "top_features": []}
    model = CALModel(cfg, seed).fit([by_id[d] for d in train_ids], [d in pos_ids for d in train_ids])
    scores = {d: round(s, 4) for d, s in model.rank(population)}
    labeled = set(labels)
    batch = model.next_batch(population, k, exclude=labeled)
    return {"ok": True, "scores": scores, "batch": batch,
            "uncertain": model.uncertain(population, float(cfg.get("uncertain_low", 0.4)), float(cfg.get("uncertain_high", 0.6))),
            "top_features": model.top_features(20), "n_pos": len(pos_ids & set(by_id)),
            "n_neg": len(neg_ids & set(by_id)), "provisional_negatives": provisional, "seed": seed}
