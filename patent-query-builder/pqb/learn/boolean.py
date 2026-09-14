"""決定木からのブール式候補生成（DNF → 観点 CNF）。企画書 §10.5 (a)。

- 特徴: 語（簡易トークナイザ由来、TF ≥ min_tf）とコード（全粒度）の二値素性
- ラベル: 適合ラベル
- 決定木（Gini、max_depth=4、min_samples_leaf=3。初期値（仮）は config）を純 Python で学習し、
  適合側の葉に至る経路を AND 節、経路の集合を OR として DNF を得る
- 決定木は「候補の生成器」であり、そのまま式にはしない。節は変換候補として流す
- リテラルの観点割当は既存 Block の語との重なりで行い、迷うものは未割当（人／P5 が決める）
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from ..core.document import Document
from ..core.dsl import Query
from ..knowledge import codes as codelib
from ..stats.rsj import doc_features
from ..stats.tokenize import candidate_terms
from ..util import norm_text
from .transforms import Transform, assign_axis


def gini(pos: int, total: int) -> float:
    if total == 0:
        return 0.0
    p = pos / total
    return 2 * p * (1 - p)


@dataclass
class Node:
    n: int
    n_pos: int
    feature: tuple | None = None
    left: "Node | None" = None       # 素性なし
    right: "Node | None" = None      # 素性あり
    depth: int = 0

    @property
    def is_leaf(self) -> bool:
        return self.feature is None

    @property
    def positive(self) -> bool:
        return self.n_pos * 2 > self.n


class DecisionTree:
    def __init__(self, max_depth: int = 4, min_samples_leaf: int = 3):
        self.max_depth = max_depth
        self.min_samples_leaf = min_samples_leaf
        self.root: Node | None = None
        self.features: list[tuple] = []

    def fit(self, X: list[set], y: list[bool], features: list[tuple]) -> "DecisionTree":
        self.features = features
        idx = list(range(len(X)))
        self.root = self._grow(X, y, idx, 0)
        return self

    def _grow(self, X, y, idx, depth) -> Node:
        n = len(idx)
        n_pos = sum(1 for i in idx if y[i])
        node = Node(n=n, n_pos=n_pos, depth=depth)
        if depth >= self.max_depth or n_pos == 0 or n_pos == n or n < 2 * self.min_samples_leaf:
            return node
        base = gini(n_pos, n)
        best, best_gain, best_split = None, 1e-9, None
        for f in self.features:
            right = [i for i in idx if f in X[i]]
            left = [i for i in idx if f not in X[i]]
            if len(right) < self.min_samples_leaf or len(left) < self.min_samples_leaf:
                continue
            rp = sum(1 for i in right if y[i])
            lp = n_pos - rp
            g = base - (len(right) / n) * gini(rp, len(right)) - (len(left) / n) * gini(lp, len(left))
            if g > best_gain + 1e-12 or (abs(g - best_gain) <= 1e-12 and best is not None and str(f) < str(best)):
                best, best_gain, best_split = f, g, (left, right)
        if best is None:
            return node
        node.feature = best
        node.left = self._grow(X, y, best_split[0], depth + 1)
        node.right = self._grow(X, y, best_split[1], depth + 1)
        return node

    def positive_paths(self) -> list[dict]:
        """適合側の葉に至る経路 [{literals: [(feature, present)], n, n_pos}]。"""
        out: list[dict] = []

        def walk(node: Node, path: list) -> None:
            if node is None:
                return
            if node.is_leaf:
                if node.positive and path:
                    out.append({"literals": list(path), "n": node.n, "n_pos": node.n_pos})
                return
            walk(node.left, path + [(node.feature, False)])
            walk(node.right, path + [(node.feature, True)])
        if self.root:
            walk(self.root, [])
        return out

    def predict(self, feats: set) -> bool:
        node = self.root
        while node and not node.is_leaf:
            node = node.right if node.feature in feats else node.left
        return bool(node and node.positive)


def build_features(judged: list[tuple[Document, bool]], stopwords: set[str] | None = None,
                   min_tf: int = 2) -> tuple[list[set], list[bool], list[tuple]]:
    docs = [d for d, _ in judged]
    vocab = {t for t, _ in candidate_terms(docs, stopwords, min_tf=min_tf)}
    X = [doc_features(d, vocab, stopwords) for d in docs]
    y = [rel for _, rel in judged]
    counts: Counter = Counter()
    for feats in X:
        counts.update(feats)
    features = sorted((f for f, c in counts.items() if c >= min_tf and c < len(X)), key=str)
    return X, y, features


def literal_label(f: tuple) -> str:
    return f[1] if f[0] == "term" else f"{f[1]} {f[3]}"


def dnf_summary(paths: list[dict]) -> list[dict]:
    return [{"literals": [{"feature": literal_label(f), "present": p} for f, p in path["literals"]],
             "n": path["n"], "n_pos": path["n_pos"]} for path in paths]


def tree_transforms(judged: list[tuple[Document, bool]], query: Query, *, stopwords: set[str] | None = None,
                    max_depth: int = 4, min_samples_leaf: int = 3, min_tf: int = 2,
                    exclusions_enabled: bool = False, axis_terms: dict[str, list[str]] | None = None) -> tuple[list[Transform], list[dict]]:
    """決定木の適合経路から変換候補を作る。→ (transforms, dnf)"""
    if len(judged) < 2 * min_samples_leaf:
        return [], []
    X, y, features = build_features(judged, stopwords, min_tf)
    if not features:
        return [], []
    tree = DecisionTree(max_depth, min_samples_leaf).fit(X, y, features)
    paths = tree.positive_paths()
    in_terms = {norm_text(t.text) for b in query.active_blocks() for t in b.adopted_terms()}
    in_codes = {(c.scheme, c.code) for b in query.active_blocks() for c in b.adopted_codes()}
    out: list[Transform] = []
    seen: set[str] = set()
    for path in paths:
        support = f"決定木: 経路の {path['n']} 件中 {path['n_pos']} 件が適合"
        for f, present in path["literals"]:
            if f[0] == "term":
                if present:
                    if norm_text(f[1]) in in_terms:
                        continue
                    key = f"ADD_TERM:{f[1]}"
                    if key in seen:
                        continue
                    seen.add(key)
                    axis, matched = assign_axis(query, f[1], axis_terms)
                    if axis not in {b.axis_id for b in query.active_blocks()}:
                        continue
                    target = {"axis_id": axis, "text": f[1]}
                    if not matched:
                        target["assigned"] = "auto"
                    out.append(Transform("ADD_TERM", target, source="tree",
                                         reason=support + ("／観点は自動割当（要確認）" if not matched else ""), direction="widen"))
                elif exclusions_enabled:
                    key = f"EXCL:{f[1]}"
                    if key not in seen:
                        seen.add(key)
                        out.append(Transform("ADD_EXCLUSION", {"terms": [{"text": f[1]}]}, source="tree",
                                             reason=support + "（不在が適合に寄与）", direction="narrow"))
            else:
                scheme, code = f[1], f[3]
                if present:
                    if (scheme, code) in in_codes:
                        continue
                    key = f"ADD_CODE:{scheme}:{code}"
                    if key in seen:
                        continue
                    seen.add(key)
                    related = [(sc, c) for (sc, c) in in_codes if sc == scheme and
                               (codelib.matches(sc, c, code) or codelib.matches(sc, code, c))]
                    if related:
                        sc, c = related[0]
                        finer = len(codelib.levels(sc, code)) > len(codelib.levels(sc, c))
                        axis = next(b.axis_id for b in query.active_blocks()
                                    if any(x.scheme == sc and x.code == c for x in b.adopted_codes()))
                        out.append(Transform("CODE_LEVEL_DOWN" if finer else "CODE_LEVEL_UP",
                                             {"axis_id": axis, "scheme": sc, "code": c, "new_code": code},
                                             source="tree", reason=support, direction="narrow" if finer else "widen"))
                    else:
                        axis = query.active_blocks()[0].axis_id if query.active_blocks() else "A"
                        out.append(Transform("ADD_CODE", {"axis_id": axis, "scheme": scheme, "code": code, "assigned": "auto"},
                                             source="tree", reason=support, direction="widen"))
                elif exclusions_enabled:
                    key = f"EXCL:{scheme}:{code}"
                    if key not in seen:
                        seen.add(key)
                        out.append(Transform("ADD_EXCLUSION", {"codes": [{"scheme": scheme, "code": code}]}, source="tree",
                                             reason=support + "（不在が適合に寄与）", direction="narrow"))
    return out, dnf_summary(paths)
