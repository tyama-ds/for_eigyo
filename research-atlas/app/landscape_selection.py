"""Auditable report evidence selection in the original document space.

Selection changes only the papers supplied to commentary. A period's center
always uses all of its valid vectors before the optional abstract filter.
No projected coordinates or pairwise corpus-size similarity matrix are used.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date
import math
import re

import numpy as np

from . import corpus_landscape, landscape, large_storage, storage
from .analytics import MAP_LIMIT
from .text_metadata import analysis_abstract

METHODS = {"centroid", "diverse", "cited", "recent"}
METHOD_LABELS = {"centroid": "重心に近い論文", "diverse": "重心との近さと内容の多様性",
                 "cited": "累積被引用数の多い論文", "recent": "出版日の新しい論文"}


def validate_options(papers_per_period=6, selection_method="centroid", abstract_only=False):
    if isinstance(papers_per_period, bool) or not isinstance(papers_per_period, int) or not 1 <= papers_per_period <= 20:
        raise ValueError("評論に使う論文数は各期間 1〜20 件の整数で指定してください。")
    if not isinstance(selection_method, str) or selection_method not in METHODS:
        raise ValueError("論文の選択方法は centroid・diverse・cited・recent から指定してください。")
    if not isinstance(abstract_only, bool):
        raise ValueError("抄録のある論文に限定する設定は真偽値で指定してください。")
    return {"papers_per_period": papers_per_period, "selection_method": selection_method,
            "abstract_only": abstract_only}


def _citations(value):
    # Unknown is not zero. The compact corpus map previously converted missing
    # counts to zero, so these values must come from the source paper records.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value if math.isfinite(value) and value >= 0 else None


def _metadata(paper, fallback=None):
    base = fallback or {}
    return {"id": str(paper.get("id", base.get("id", ""))),
            "topic_id": str(paper.get("topic_id", base.get("topic_id")) or "unknown"),
            **{key: paper.get(key, base.get(key)) for key in ("year", "publication_date", "date_precision")},
            "citations": _citations(paper.get("citations")),
            "abstract_available": bool(analysis_abstract(str(paper.get("abstract") or "")).strip())}


def _publication_order(record):
    """Known dates within a year precede year-only records; no month is invented."""
    year, _ = landscape._period_number(record, "year")
    raw = record.get("publication_date")
    precision = record.get("date_precision")
    month, reason = landscape._period_number(record, "month")
    if month is None:
        return (-(year or 0), 0, 0), str(year or ""), "year" if year else "unknown", reason
    year, month_offset = divmod(month, 12)
    month_number = month_offset + 1
    if precision != "month" and isinstance(raw, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        try:
            parsed = date.fromisoformat(raw)
            if parsed <= date.today():
                return (-year, -month_number, -parsed.day), raw, "day", "valid"
        except ValueError:
            pass
    return (-year, -month_number, 0), f"{year:04d}-{month_number:02d}", "month", "valid"


class SelectionContext:
    """Cache compact metadata/vectors and period membership for one report job.

    ``select(topic_id, period_id, interval, **options)`` returns ordered IDs and
    JSON-safe metadata. Reuse this object for both movement sides and all annual
    chapters. Abstract strings are inspected once and never retained here.
    """

    def __init__(self, result_id: str, scope: str = "sample"):
        if scope not in {"sample", "full"}:
            raise ValueError("重心の分析対象は sample・full を指定してください。")
        self.result_id, self.scope = result_id, scope
        self._groups = {}
        result = storage.read("results", result_id, include_papers=False)
        if scope == "full":
            with corpus_landscape._LOCK:
                state = corpus_landscape._load(corpus_landscape._revision(result_id))
                self.records = [_metadata({}, row) for row in state["records"]]
                self.vectors = state["vectors"]
                self.representation_source = state["source"]
            lookup = {row["id"]: row for row in self.records}
            seen = set()
            for paper in large_storage.iter_papers(result, storage.data_root()):
                identifier = str(paper.get("id", ""))
                if identifier in lookup:
                    # Retain the same topic/date membership as the cached
                    # corpus analysis; recover only missingness-sensitive data.
                    lookup[identifier].update(citations=_citations(paper.get("citations")),
                        abstract_available=bool(analysis_abstract(str(paper.get("abstract") or "")).strip()))
                    seen.add(identifier)
            if seen != set(lookup):
                raise ValueError("全件分析の文書表現と保存論文の ID が一致しません。再分析してください。")
        else:
            nodes = [node for node in (result.get("map") or {}).get("nodes", [])[:MAP_LIMIT]
                     if isinstance(node, dict) and node.get("id")]
            papers = large_storage.papers_by_ids(result, storage.data_root(), [node["id"] for node in nodes])
            lookup = {str(p["id"]): p for p in papers}
            if set(lookup) != {str(node["id"]) for node in nodes}:
                raise ValueError("表示標本の文書表現と保存論文の ID が一致しません。再分析してください。")
            # Match map analysis membership; a saved map's topic assignments
            # define the sample, while publication precision comes from papers.
            self.records = [_metadata(lookup[str(node["id"])], node) for node in nodes]
            for record, node in zip(self.records, nodes):
                record["topic_id"] = str(node.get("topic_id", "unknown"))
            if nodes:
                self.vectors, _, self.representation_source, _ = landscape._representation(result, nodes, papers)
            else:
                self.vectors, self.representation_source = np.zeros((0, 1)), "empty"
        self.vectors = np.asarray(self.vectors)
        if self.vectors.ndim != 2 or len(self.vectors) != len(self.records):
            raise ValueError("重心分析の文書表現と論文件数が一致しません。")
        with np.errstate(over="ignore", invalid="ignore"):
            self._norms = np.linalg.norm(self.vectors, axis=1)
        self._valid = np.isfinite(self.vectors).all(axis=1) & np.isfinite(self._norms) & (self._norms > 1e-12)

    def _period_groups(self, interval):
        if interval not in landscape.INTERVALS:
            raise ValueError("期間は year・quarter・month を指定してください。")
        if interval not in self._groups:
            groups = defaultdict(list)
            for index, record in enumerate(self.records):
                number, _ = landscape._period_number(record, interval)
                if number is not None:
                    groups[(record["topic_id"], landscape._period(number, interval))].append(index)
            self._groups[interval] = dict(groups)
        return self._groups[interval]

    def select(self, topic_id, period_id, interval, *, papers_per_period=6,
               selection_method="centroid", abstract_only=False):
        options = validate_options(papers_per_period, selection_method, abstract_only)
        indices = np.asarray(self._period_groups(interval).get((str(topic_id), str(period_id)), []), dtype=int)
        valid = indices[self._valid[indices]]
        # Filtering abstracts changes the evidence list, never its reference center.
        center = self.vectors[valid].mean(axis=0, dtype=np.float64) if len(valid) else None
        center_norm = float(np.linalg.norm(center)) if center is not None else 0.
        center_defined = math.isfinite(center_norm) and center_norm > 1e-12
        eligible = [int(index) for index in indices
                    if not abstract_only or self.records[index]["abstract_available"]]
        abstract_excluded = len(indices) - len(eligible)
        vector_excluded = 0
        if selection_method in {"centroid", "diverse"}:
            selected_vectors = [index for index in eligible if self._valid[index] and center_defined]
            vector_excluded = len(eligible) - len(selected_vectors)
            eligible = selected_vectors
        # IDs provide a deterministic tie break independent of storage iteration.
        eligible.sort(key=lambda index: self.records[index]["id"])
        eligible_array = np.asarray(eligible, dtype=int)
        similarity = np.full(len(eligible), np.nan, dtype=float)
        if center_defined and eligible:
            good = self._valid[eligible_array]
            similarity[good] = np.clip(self.vectors[eligible_array[good]] @ center /
                                      (self._norms[eligible_array[good]] * center_norm), -1, 1)
        distances = {index: float(1 - value) if np.isfinite(value) else None
                     for index, value in zip(eligible, similarity)}
        chosen_scores = {}
        if selection_method == "centroid":
            chosen = sorted(eligible, key=lambda index: (round(distances[index], 12), self.records[index]["id"]))[:papers_per_period]
        elif selection_method == "diverse":
            chosen = []
            if eligible:
                # O(N*d + N*k*d) work and O(N*d + N) memory, never N-by-N.
                unit = self.vectors[eligible_array] / self._norms[eligible_array, None]
                redundancy = np.zeros(len(eligible), dtype=float)
                available = np.ones(len(eligible), dtype=bool)
                for rank in range(min(papers_per_period, len(eligible))):
                    scores = similarity if rank == 0 else .65 * similarity - .35 * redundancy
                    scores = np.where(available, np.round(scores, 12), -np.inf)
                    position = int(np.argmax(scores))
                    index = eligible[position]
                    chosen.append(index)
                    chosen_scores[index] = float(scores[position])
                    available[position] = False
                    redundancy = np.maximum(redundancy, np.clip(unit @ unit[position], 0, 1))
        elif selection_method == "cited":
            chosen = sorted(eligible, key=lambda index: (self.records[index]["citations"] is None,
                -(self.records[index]["citations"] or 0), self.records[index]["id"]))[:papers_per_period]
        else:
            chosen = sorted(eligible, key=lambda index: (*_publication_order(self.records[index])[0],
                                                         self.records[index]["id"]))[:papers_per_period]
        selected_records = []
        for rank, index in enumerate(chosen, 1):
            record = self.records[index]
            _, publication_date, precision, _ = _publication_order(record)
            selected = {"id": record["id"], "selection_rank": rank, "cosine_distance": distances[index],
                        "citations": record["citations"], "publication_date": publication_date,
                        "date_precision": precision, "abstract_available": record["abstract_available"],
                        "reason": METHOD_LABELS[selection_method]}
            if selection_method == "diverse":
                selected["selection_score"] = chosen_scores[index]
            selected_records.append(selected)
        warnings = []
        if len(chosen) < papers_per_period:
            warnings.append(f"選択条件に合う論文は {len(chosen)} 件のため、希望の {papers_per_period} 件より少なくなっています。")
        if not center_defined:
            warnings.append("有効な文書表現から方向を持つ重心を計算できないため、重心からの距離は未定義です。")
        if vector_excluded:
            warnings.append(f"コサイン距離を定義できない {vector_excluded} 件を内容による論文選択から除外しました。")
        if selection_method == "cited":
            warnings.append("累積被引用数による順序です。出版年による補正や、その期間内の引用増加の評価ではありません。被引用数が不明の論文は既知値の後に置きます。")
        if selection_method == "recent":
            warnings.append("出版年の降順で選び、同年では既知の月・日を比較します。年のみの論文はその年の詳細日付がある論文の後に置き、月・日を補完しません。")
        ids = [record["id"] for record in selected_records]
        return ids, {**options, "method_label": METHOD_LABELS[selection_method], "topic_id": str(topic_id),
                     "period_id": str(period_id), "scope": self.scope,
                     "candidate_count": len(indices), "eligible_count": len(eligible),
                     "valid_vector_count": len(valid), "excluded_abstract_count": abstract_excluded,
                     "excluded_vector_count": vector_excluded, "selected_count": len(chosen),
                     "selected_ids": ids, "selected_records": selected_records,
                     "representation_source": self.representation_source,
                     "center_population": "all_valid_vectors_before_abstract_filter",
                     "diversity_weight": .35 if selection_method == "diverse" else None,
                     "warnings": warnings}
