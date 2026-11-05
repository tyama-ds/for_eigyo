"""Lightweight term / classification / patent co-occurrence graph (no LLM).

Nodes: feature terms from title+abstract, IPC main groups from the row's own
IPC field, and patents with their human labels. Edges: term–patent occurrence,
classification–patent assignment. The graph answers three questions for the
"次の式へ" stage with explicit evidence paths:

* which terms separate keep rows from exclude rows (include / exclude candidates)
* which terms co-occur on the same keep rows so strongly that they look like
  alternative expressions of one search axis (OR-group hypotheses)
* which terms characterise the keep rows of a selected classification

Everything is a hypothesis computed from the imported CSV and the user's
labels. Nothing here is a synonym dictionary or an assertion about the real
search population.
"""

from __future__ import annotations

import re

import numpy as np
from sklearn.feature_extraction.text import CountVectorizer

from analysis_engine import texts
from common import keep_rows, text_contains
from query_formats import terms_of
from query_scoring import row_codes, score_query, with_terms, score_tree
from query_formats import build_tree

TOKEN_PATTERN = r"(?u)[一-龯ァ-ヶー]{2,20}|[a-zA-Z][a-zA-Z0-9-]{2,30}"
_STOP = re.compile(r"[\s。、，,（）()]")


class TermGraph:
    def __init__(self, rows):
        self.rows = list(rows)
        self.ids = [str(row.get("id") or f"行{index + 1}") for index, row in enumerate(self.rows)]
        self.labels = np.array([row.get("label") if row.get("label") in ("keep", "exclude") else "" for row in self.rows])
        self.keep_mask = self.labels == "keep"
        self.exclude_mask = self.labels == "exclude"
        docs = [text.casefold() for text in texts(self.rows)]
        self.vectorizer = CountVectorizer(token_pattern=TOKEN_PATTERN, binary=True, max_features=16000, min_df=1)
        try:
            self.matrix = self.vectorizer.fit_transform(docs).tocsc()
            self.terms = list(self.vectorizer.get_feature_names_out())
        except ValueError:
            self.matrix = None
            self.terms = []
        # IPC main groups (H01M10/0562 -> H01M10/00) from the row's own field only.
        self.groups = []
        for row in self.rows:
            own = row_codes(row)["IPC"]
            self.groups.append({_main_group(code) for code in own})
        self.group_docs = {}
        for index, groups in enumerate(self.groups):
            for key in groups:
                self.group_docs.setdefault(key, set()).add(index)

    # ----- term statistics -------------------------------------------------
    def term_docs(self, term):
        if self.matrix is None or term not in self.vectorizer.vocabulary_:
            return np.zeros(len(self.rows), dtype=bool)
        column = self.matrix.getcol(self.vectorizer.vocabulary_[term]).toarray().ravel()
        return column > 0

    def _term_stats(self):
        if self.matrix is None:
            return []
        keep_hits = np.asarray(self.matrix[self.keep_mask].sum(axis=0)).ravel()
        exclude_hits = np.asarray(self.matrix[self.exclude_mask].sum(axis=0)).ravel()
        total = np.asarray(self.matrix.sum(axis=0)).ravel()
        return list(zip(self.terms, keep_hits, exclude_hits, total))

    def evidence(self, term, *, limit=3):
        """Top co-occurring IPC main group among keep rows, plus example keep IDs."""
        docs = self.term_docs(term)
        keep_docs = np.flatnonzero(docs & self.keep_mask)
        counts = {}
        for index in keep_docs:
            for key in self.groups[index]:
                counts[key] = counts.get(key, 0) + 1
        best = max(counts.items(), key=lambda item: (item[1], item[0]))[0] if counts else None
        path = [{"type": "term", "value": term}]
        if best:
            path.append({"type": "ipc", "value": best, "keep_count": counts[best], "keep_with_term": int(len(keep_docs))})
        path.append({"type": "patents", "ids": [self.ids[i] for i in keep_docs[:limit]], "total": int(len(keep_docs))})
        return path

    # ----- suggestions ------------------------------------------------------
    def suggest(self, query=None, *, limit=8, min_support=2):
        keep_total = int(self.keep_mask.sum())
        exclude_total = int(self.exclude_mask.sum())
        existing = [t.casefold() for t in (terms_of(query) if isinstance(query, dict) else [])]
        result = dict(
            include=[],
            exclude=[],
            or_groups=[],
            keep_total=keep_total,
            exclude_total=exclude_total,
            note=(
                "取り込み済み特許のタイトル・要約と人の必要・不要判定から作った共起グラフです。"
                "代替表現の候補は同じ必要特許に共起する語で、同義語辞書ではありません。"
            ),
        )
        if keep_total < min_support or self.matrix is None:
            result["note"] += " 必要判定が2件以上になると候補を提示します。"
            return result

        def usable(term):
            if len(term) < 3 or _STOP.search(term):
                return False
            return not any(term in old or old in term for old in existing)

        stats = [(t, int(k), int(e), int(n)) for t, k, e, n in self._term_stats() if usable(t)]
        include, exclude = [], []
        for term, k, e, n in stats:
            if k >= min_support:
                keep_rate = k / keep_total
                leak = (e / exclude_total) if exclude_total else 0.0
                include.append((keep_rate - leak, k, -e, term))
            if e >= min_support and k == 0 and exclude_total:
                exclude.append((e / exclude_total, e, term))
        include.sort(reverse=True)
        exclude.sort(reverse=True)
        chosen_include = _dedupe([t for _, _, _, t in include], limit)
        chosen_exclude = _dedupe([t for _, _, t in exclude], limit)
        by_term = {t: (k, e, n) for t, k, e, n in stats}
        for term in chosen_include:
            k, e, n = by_term[term]
            result["include"].append(
                dict(term=term, keep_hit=k, keep_total=keep_total, exclude_hit=e, exclude_total=exclude_total,
                     unlabeled_hit=int(n - k - e), path=self.evidence(term))
            )
        for term in chosen_exclude:
            k, e, n = by_term[term]
            result["exclude"].append(
                dict(term=term, keep_hit=k, keep_total=keep_total, exclude_hit=e, exclude_total=exclude_total,
                     unlabeled_hit=int(n - k - e), path=self.evidence(term))
            )
        result["or_groups"] = self._or_groups(chosen_include, existing, query)
        return result

    def _or_groups(self, candidates, existing_terms, query, *, threshold=0.6):
        """Pairs whose keep-row sets overlap strongly: alternative-expression hypotheses."""
        pool = list(dict.fromkeys(candidates + [t for t in existing_terms if t in self.vectorizer.vocabulary_]))
        vectors = {t: (self.term_docs(t) & self.keep_mask) for t in pool}
        groups, used = [], set()
        for i, a in enumerate(pool):
            if a in used or not vectors[a].any():
                continue
            members = [a]
            for b in pool[i + 1 :]:
                if b in used or a in b or b in a:
                    continue
                union = (vectors[a] | vectors[b]).sum()
                inter = (vectors[a] & vectors[b]).sum()
                if union and inter / union >= threshold and inter >= 2:
                    members.append(b)
            if len(members) > 1:
                used.update(members)
                shared = int(np.logical_and.reduce([vectors[m] for m in members]).sum())
                groups.append(
                    dict(terms=members, shared_keep=shared,
                         reason=f"必要特許{shared}件に共起します。同じ探索軸の代替表現ならORでまとめる候補です（同義語の確定ではありません）。")
                )
        return groups[:5]

    def class_terms(self, key, *, limit=6):
        """Terms characteristic of keep rows that carry the given IPC main group."""
        docs = self.group_docs.get(_main_group(key.split(":")[-1]), set())
        if not docs or self.matrix is None:
            return []
        mask = np.zeros(len(self.rows), dtype=bool)
        mask[list(docs)] = True
        inside = np.asarray(self.matrix[mask & self.keep_mask].sum(axis=0)).ravel()
        outside = np.asarray(self.matrix[~mask].sum(axis=0)).ravel()
        order = np.argsort(-(inside - outside))
        out = []
        for index in order:
            term = self.terms[index]
            if inside[index] < 2 or len(term) < 3 or _STOP.search(term):
                continue
            out.append(dict(term=term, keep_hit=int(inside[index]), outside_hit=int(outside[index])))
            if len(out) == limit:
                break
        return out


def _main_group(code):
    code = str(code or "")
    return code.split("/")[0] + "/00" if "/" in code else code


def _dedupe(terms, limit):
    out = []
    for term in terms:
        if any(term in old or old in term for old in out):
            continue
        out.append(term)
        if len(out) == limit:
            break
    return out


def graph_suggestions(rows, query=None, *, limit=8):
    """Suggestions plus the retrospective score of the query and of each candidate."""
    graph = TermGraph(rows)
    result = graph.suggest(query, limit=limit)
    base = score_query(query, rows) if isinstance(query, dict) else None
    result["base_score"] = base
    if base and "error" not in base:
        tree = build_tree(query)
        for item in result["include"]:
            item["projected"] = _brief(score_tree(with_terms(tree, include=[item["term"]]), rows))
        for item in result["exclude"]:
            item["projected"] = _brief(score_tree(with_terms(tree, exclude=[item["term"]]), rows))
    return result


def _brief(score):
    return {k: score[k] for k in ("keep_hit", "keep_total", "exclude_hit", "exclude_total", "unlabeled_hit")}
