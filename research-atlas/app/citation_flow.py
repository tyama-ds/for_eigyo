"""Observed citing-to-cited links and paired temporal content centroids.

The graph is resolved only inside the saved analysis corpus. References are
streamed one source at a time; only a bounded set of display edges is retained.
All eligible sources and references contribute to the numerical summaries.
"""
from __future__ import annotations

from calendar import monthrange
from collections import Counter, defaultdict
from copy import deepcopy
from datetime import date
from functools import lru_cache
import hashlib
import heapq
import re
import threading
import warnings as python_warnings

import numpy as np
from sklearn.decomposition import PCA

from . import corpus_landscape, large_storage, storage
from .analytics import _normalize_positions
from .bibliography import normalize_doi, normalize_reference_id, normalize_references
from .landscape import INTERVALS, _cosine, _digest, _period, _period_number

NODE_LIMIT = 240
EDGE_LIMIT = 480
EVIDENCE_LIMIT = 6
_LOCK = threading.RLock()


def _list(value):
    return value if isinstance(value, (list, tuple)) else []


def _identifiers(value):
    """No local IDs, titles, fuzzy matches, or list-position inference."""
    if isinstance(value, str):
        value = {"id": value}
    if not isinstance(value, dict):
        return set()
    aliases = value.get("aliases") if isinstance(value.get("aliases"), dict) else {}
    raw = [value.get(field) for field in ("id", "doi", "DOI", "eid", "EID", "url", "URL")]
    for field in ("ids", "dois", "eids"):
        raw.extend(_list(aliases.get(field)))
    for field, prefix in (("pmid", "pmid:"), ("pmcid", "pmcid:"), ("arxiv_id", "arxiv:")):
        if value.get(field):
            raw.append(prefix + str(value[field]))
    result = set()
    for item in raw:
        if doi := normalize_doi(item):
            result.add("doi:" + doi)
        elif identifier := normalize_reference_id(item):
            result.add("id:" + identifier)
    return result


def _family(identifier):
    if identifier.startswith("doi:"):
        return "doi"
    value = identifier[3:]
    return "scopus" if value.startswith("2-s2.0-") else ":".join(value.split(":")[:-1])


def _resolve(reference, identifiers, identities):
    keys = _identifiers(reference)
    if not keys:
        return None, "unrecognized_identifier"
    candidates = set()
    for key in keys:
        found = identifiers.get(key, ())
        if len(found) > 1:
            return None, "ambiguous_identifier"
        candidates.update(found)
    if len(candidates) > 1:
        return None, "conflicting_identifiers"
    if not candidates:
        return None, "unresolved_in_analysis"
    target = next(iter(candidates))
    known = identities[target]
    families = {_family(key) for key in known}
    # A matching PMID cannot override an explicitly different known DOI.
    if any(key not in known and _family(key) in families for key in keys):
        return None, "conflicting_identifiers"
    return target, "matched"


def _reference_records(paper):
    raw = paper.get("references")
    for item in raw if isinstance(raw, (list, tuple)) else [raw] if raw else []:
        if _identifiers(item):
            yield item
            continue
        text = item if isinstance(item, str) else item.get("unstructured") if isinstance(item, dict) else None
        normalized = normalize_references(text) if text else []
        # One supplied reference record remains one identity assertion. In
        # particular, do not split a conflicting DOI + PMID into two arrows.
        if normalized:
            yield {"aliases": {"dois": [row["doi"] for row in normalized if row.get("doi")],
                               "ids": [row["id"] for row in normalized if row.get("id")]}}
        else:
            yield item


def _date_bounds(record):
    """Closed uncertainty interval; only non-overlapping dates establish order."""
    year, reason = _period_number(record, "year")
    if year is None:
        return None, reason
    raw, precision = record.get("publication_date"), record.get("date_precision")
    if not raw or precision in {"year", "unknown"}:
        if raw and (not isinstance(raw, str) or not re.match(rf"^{year}(?:-|$)", raw)):
            return None, "year_conflict"
        return (date(year, 1, 1), date(year, 12, 31)), "valid"
    month, reason = _period_number(record, "month")
    if month is None:
        return None, reason
    year, offset = divmod(month, 12)
    if len(raw) == 10 and precision != "month":
        parsed = date.fromisoformat(raw)
        return (parsed, parsed), "valid"
    return (date(year, offset + 1, 1), date(year, offset + 1, monthrange(year, offset + 1)[1])), "valid"


def _rank(value):
    return int.from_bytes(hashlib.sha256(str(value).encode()).digest()[:8], "big")


def _evidence_add(values, identifier):
    values.add(identifier)
    if len(values) > EVIDENCE_LIMIT:
        values.remove(max(values, key=lambda item: (_rank(item), item)))


def _evidence(values):
    return sorted(values, key=lambda item: (_rank(item), item))


def _offer_edge(heap, source, target, limit):
    if not limit:
        return
    key = f"{source}\0{target}"
    entry = (-_rank(key), source, target)
    if len(heap) < limit:
        heapq.heappush(heap, entry)
    elif entry > heap[0]:
        heapq.heapreplace(heap, entry)


@lru_cache(maxsize=2)
def _projection(revision):
    state = corpus_landscape._load(revision)
    vectors = state["vectors"]
    count, dimensions = vectors.shape
    components = min(2, count, dimensions)
    if count < 2:
        coordinates, ratio = np.full((count, 2), .5), []
    else:
        # Full SVD is exact, fitted once over the common space. No sampled or
        # independently refitted period layouts enter centroid calculations.
        estimator = PCA(n_components=components, svd_solver="full")
        with python_warnings.catch_warnings():
            python_warnings.simplefilter("ignore", RuntimeWarning)
            coordinates = _normalize_positions(estimator.fit_transform(vectors))
        ratio = np.nan_to_num(estimator.explained_variance_ratio_).tolist()
    return coordinates, {"algorithm": "pca", "fit_papers": count, "transform_papers": count,
                         "basis_scope": "full_corpus", "coordinate_scope": "shared_all_periods",
                         "input_dimensions": dimensions, "approximation": "none",
                         "explained_variance_ratio": ratio, "normalization": "fixed_affine_per_axis_0.05_to_0.95",
                         "projection_id": _digest(["citation-flow-pca-v1", revision])[:24]}


def _coverage():
    return {"papers_total": 0, "papers_with_references": 0, "papers_with_valid_vectors": 0,
            "references_total": 0, "matched_references": 0, "matched_citing_papers": 0,
            "matched_target_papers": 0, "valid_edges": 0, "semantic_citing_papers": 0,
            "semantic_edges": 0, "exclusions": Counter()}


def _finish_coverage(value):
    value["exclusions"] = dict(sorted(value["exclusions"].items()))
    count, available = value["papers_total"], value["papers_with_references"]
    value["references_coverage_pct"] = round(100 * available / count, 2) if count else None
    value["matched_citing_coverage_pct"] = round(100 * value["matched_citing_papers"] / count, 2) if count else None
    value["semantic_citing_coverage_pct"] = round(100 * value["semantic_citing_papers"] / count, 2) if count else None
    value["available"] = available > 0
    value["reason"] = ("observed_older_in_corpus_references" if value["semantic_citing_papers"] else
                       "no_eligible_semantic_references" if available else "references_not_provided")
    return value


def _center(xy_sum, count, evidence):
    if not count:
        return None
    xy = xy_sum / count
    return {"x": float(xy[0]), "y": float(xy[1]), "count": count, "evidence_ids": _evidence(evidence)}


@lru_cache(maxsize=3)
def _build(revision, interval, topic_id, node_limit, edge_limit):
    state = corpus_landscape._load(revision)
    records, vectors = state["records"], state["vectors"]
    coordinates, projection = _projection(revision)
    root, result_id, _, _ = revision
    # A large result read returns its compact manifest, including provenance;
    # it does not restore its papers or reference lists into memory.
    result = storage.read("results", result_id, include_papers=False)
    result_meta = result.get("meta", {})
    topics = {str(item["id"]): dict(item) for item in state["topics"]}
    for record in records:
        topics.setdefault(record["topic_id"], {"id": record["topic_id"], "label": record["topic_id"], "color": "#8791a6"})
    if topic_id != "all" and topic_id not in topics:
        raise ValueError("指定したトピックが見つかりません。")
    index = {record["id"]: i for i, record in enumerate(records)}
    if len(index) != len(records):
        raise ValueError("論文 ID が重複しています。引用先を一意に決められません。")
    valid_vectors = np.linalg.norm(vectors, axis=1) > 1e-12
    periods, bounds = [], []
    groups, coverage = {}, _coverage()
    selected = set()
    for i, record in enumerate(records):
        number, _ = _period_number(record, interval)
        interval_bounds, _ = _date_bounds(record)
        if interval_bounds is None:
            number = None
        periods.append(number)
        bounds.append(interval_bounds)
        if topic_id != "all" and record["topic_id"] != topic_id:
            continue
        selected.add(i)
        coverage["papers_total"] += 1
        coverage["papers_with_valid_vectors"] += int(valid_vectors[i])
        if number is None:
            coverage["exclusions"]["source_period_unknown"] += 1
            continue
        if number not in groups:
            groups[number] = {"coverage": _coverage(), "current_xy": np.zeros(2), "current_count": 0,
                              "paired_xy": np.zeros(2), "paired_vector": np.zeros(vectors.shape[1]),
                              "foundation_xy": np.zeros(2), "foundation_vector": np.zeros(vectors.shape[1]),
                              "current_evidence": set(), "paired_evidence": set(), "foundation_evidence": set(),
                              "targets": set()}
        group = groups[number]
        group["coverage"]["papers_total"] += 1
        group["coverage"]["papers_with_valid_vectors"] += int(valid_vectors[i])
        if valid_vectors[i]:
            group["current_xy"] += coordinates[i]
            group["current_count"] += 1
            _evidence_add(group["current_evidence"], record["id"])

    identifiers, identities = defaultdict(set), {}
    for paper in large_storage.iter_papers(result, root):
        i = index[str(paper["id"])]
        identities[i] = _identifiers(paper)
        for identifier in identities[i]:
            identifiers[identifier].add(i)
    candidates, all_targets = [], set()
    for paper in large_storage.iter_papers(result, root):
        source = index[str(paper["id"])]
        if source not in selected:
            continue
        group = groups.get(periods[source])
        measures = [coverage] + ([group["coverage"]] if group else [])
        provided = bool(paper.get("references")) or paper.get("references_status") == "provided"
        for measure in measures:
            measure["papers_with_references"] += int(provided)
        seen, matched_count, semantic_count = set(), 0, 0
        reference_sum = np.zeros(vectors.shape[1])
        reference_xy = np.zeros(2)
        for reference in _reference_records(paper):
            for measure in measures:
                measure["references_total"] += 1
            target, reason = _resolve(reference, identifiers, identities)
            if target is not None:
                for measure in measures:
                    measure["matched_references"] += 1
                if target in seen:
                    reason = "duplicate_source_target"
                else:
                    seen.add(target)
                    if target == source:
                        reason = "self_reference"
                    elif periods[source] is None or periods[target] is None or bounds[source] is None or bounds[target] is None:
                        reason = "unknown_or_invalid_date"
                    elif bounds[target][1] >= bounds[source][0]:
                        reason = "future_target" if bounds[target][0] > bounds[source][1] else "chronology_not_strictly_older"
                    else:
                        reason = "valid"
            if reason != "valid":
                for measure in measures:
                    measure["exclusions"][reason] += 1
                continue
            matched_count += 1
            all_targets.add(target)
            group["targets"].add(target)
            for measure in measures:
                measure["valid_edges"] += 1
            if not valid_vectors[source] or not valid_vectors[target]:
                for measure in measures:
                    measure["exclusions"]["zero_source_vector" if not valid_vectors[source] else "zero_target_vector"] += 1
                continue
            semantic_count += 1
            reference_sum += vectors[target]
            reference_xy += coordinates[target]
            _evidence_add(group["foundation_evidence"], records[target]["id"])
            _offer_edge(candidates, records[source]["id"], records[target]["id"], edge_limit)
            for measure in measures:
                measure["semantic_edges"] += 1
        if matched_count:
            for measure in measures:
                measure["matched_citing_papers"] += 1
        if semantic_count:
            for measure in measures:
                measure["semantic_citing_papers"] += 1
            # Every citing paper has one vote, independent of reference count.
            group["foundation_vector"] += reference_sum / semantic_count
            group["foundation_xy"] += reference_xy / semantic_count
            group["paired_vector"] += vectors[source]
            group["paired_xy"] += coordinates[source]
            _evidence_add(group["paired_evidence"], records[source]["id"])
    coverage["matched_target_papers"] = len(all_targets)
    centroid_rows, period_rows = [], []
    for period_index, (number, group) in enumerate(sorted(groups.items())):
        group_coverage = group["coverage"]
        group_coverage["matched_target_papers"] = len(group["targets"])
        count = group_coverage["semantic_citing_papers"]
        distance = _cosine(group["paired_vector"], group["foundation_vector"]) if count else None
        row = {"period_id": _period(number, interval), "topic_id": topic_id,
               "current": _center(group["current_xy"], group["current_count"], group["current_evidence"]),
               "foundation": _center(group["foundation_xy"], count, group["foundation_evidence"]),
               "paired_current": _center(group["paired_xy"], count, group["paired_evidence"]),
               "cosine_distance": distance, "coverage": _finish_coverage(group_coverage),
               "comparison_reason": "paired_observed_references" if distance is not None else
                                    "zero_centroid_norm" if count else "no_eligible_semantic_references"}
        centroid_rows.append(row)
        period_rows.append({"id": row["period_id"], "label": row["period_id"], "index": period_index,
                            "count": group_coverage["papers_total"]})
    # Targets can belong to other source topics/periods. Keep their true period.
    display_ids, edges = set(), []
    for _, source_id, target_id in sorted(candidates, key=lambda item: (-item[0], item[1], item[2])):
        endpoints = {source_id, target_id}
        if len(display_ids | endpoints) > node_limit:
            continue
        display_ids.update(endpoints)
        source, target = index[source_id], index[target_id]
        edges.append({"id": "citation-" + _digest([source_id, target_id])[:20], "source_id": source_id,
                      "target_id": target_id, "source_period": _period(periods[source], interval),
                      "target_period": _period(periods[target], interval),
                      "source_topic_id": records[source]["topic_id"], "target_topic_id": records[target]["topic_id"]})
    filler = sorted((i for i in selected if periods[i] is not None and valid_vectors[i]),
                    key=lambda i: (_rank(records[i]["id"]), records[i]["id"]))
    for i in filler:
        if len(display_ids) >= node_limit:
            break
        display_ids.add(records[i]["id"])
    nodes = []
    for identifier in sorted(display_ids):
        i, record = index[identifier], records[index[identifier]]
        nodes.append({"id": identifier, "title": record["label"], "year": record["year"],
                      "publication_date": record["publication_date"], "date_precision": record["date_precision"],
                      "period_id": _period(periods[i], interval), "topic_id": record["topic_id"],
                      "x": float(coordinates[i, 0]), "y": float(coordinates[i, 1])})
    # Include target-only periods so every real arrow has a visible time plane.
    source_counts = {row["id"]: row["count"] for row in period_rows}
    period_ids = sorted(set(source_counts) | {node["period_id"] for node in nodes})
    period_rows = [{"id": identifier, "label": identifier, "index": i, "count": source_counts.get(identifier, 0)}
                   for i, identifier in enumerate(period_ids)]
    coverage.update(displayed_nodes=len(nodes), displayed_edges=len(edges), analysis_papers=len(records))
    notices = list(dict.fromkeys(_list(result_meta.get("warnings")) + list(state["warnings"]))) + [
        "読み込んだ分析結果内の論文だけを参照先として照合します。分析期間・取得範囲の外にある文献は未解決のままで、追加取得しません。",
        "矢印は正規化した DOI・標準識別子と保存済み別名が一意に一致する引用元（新）→引用先（旧）です。日付の不明・矛盾・前後関係不明、自己参照、重複は除外します。",
        "参照先重心は引用元論文ごとに有効な参照先ベクトルを平均し、その重心を引用元論文間で等重みに平均します。参照件数が多い論文の重みは増やしません。",
        "内容重心は当期の非ゼロベクトル全件です。cosine 距離は参照先を確認できた同じ引用元論文群の内容重心との比較で、2 次元の見た目の距離ではありません。",
        "参照情報の欠測は引用ゼロではありません。引用・内容の類似性だけから因果的影響、知識移転、重要度は判断できません。",
    ]
    if coverage["valid_edges"] > len(edges):
        notices.append(f"描画は最大 {node_limit} 論文・{edge_limit} 引用です。全 {coverage['valid_edges']} 件の時系列が有効な引用で集計し、非ゼロベクトルのある端点だけを描画します。")
    return {"version": 1, "result_id": result_id, "flow_id": _digest(["citation-flow-v1", revision, interval, topic_id])[:24],
            "interval": interval, "topic_id": topic_id, "topics": list(topics.values()), "periods": period_rows,
            "nodes": nodes, "edges": edges, "centroids": centroid_rows, "coverage": _finish_coverage(coverage),
            "meta": {"analysis_papers": len(records), "projection": projection,
                     "dataset_id": result.get("dataset_id"), "dataset_name": result.get("dataset_name"),
                     "is_demo": bool(result_meta.get("is_demo")),
                     "provenance": {"result_created_at": result.get("created_at"),
                                    "providers": deepcopy(_list(result_meta.get("providers"))),
                                    "sampled": bool(result_meta.get("sampled")),
                                    "synthetic_count": result_meta.get("synthetic_count"),
                                    "test_summary_count": result_meta.get("test_summary_count")},
                     "representation_source": state["source"], "embedding": state["embedding"],
                     "vector_dimensions": vectors.shape[1], "vector_normalization": "L2_per_document",
                     "basis_scope": "full_corpus", "count_scope": "full_selected_source_corpus",
                     "reference_scope": "loaded_analysis_corpus_only", "edge_direction": "citing_to_cited_new_to_old",
                     "chronology": "target_latest_possible_date_before_source_earliest_possible_date",
                     "centroid_weighting": "equal_citing_papers_after_within_paper_reference_mean",
                     "distance": "cosine_paired_current_to_foundation_in_common_vector_space",
                     "zero_vectors": "excluded_from_content_centers_and_semantic_edge_endpoints",
                     "period_assignment": "source_publication_period_with_no_date_imputation",
                     "node_limit": node_limit, "edge_limit": edge_limit,
                     "display_selection": "deterministic_hash_of_actual_edges_then_endpoint_budget",
                     "evidence_selection": "deterministic_hash_examples_not_centroid_nearest",
                     "external_fetches": 0, "causal_interpretation": False}, "warnings": notices}


def build_citation_flow(result_id, interval="year", topic_id=None):
    """Return an isolated response; cached values contain bounded evidence only."""
    if interval not in INTERVALS:
        raise ValueError("集計単位は year・quarter・month のいずれかを指定してください。")
    selected_topic = str(topic_id or "all")
    with _LOCK:
        revision = corpus_landscape._revision(result_id)
        return deepcopy(_build(revision, interval, selected_topic, NODE_LIMIT, EDGE_LIMIT))
