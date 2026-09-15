"""Shared-coordinate temporal maps from at most 400 saved display documents.

Coordinates aid exploration; inference uses the bounded document representation,
not a nonlinear map distance. No paper set is independently fitted by period.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from copy import deepcopy
from datetime import date
from functools import lru_cache
import hashlib
import json
import math
import re

import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer

from . import large_storage, storage
from .analytics import MAP_LIMIT, STOP_WORDS, _map_projection, _projection_inputs
from .frontiers import _publication_month
from .map_terrain import build_terrain
from .text_metadata import analysis_abstract

PROJECTIONS = {"auto", "tsne", "pca", "umap"}
INTERVALS = {"year", "quarter", "month"}
PERMUTATIONS = 199
MIN_GROUP = 5
MIN_COSINE_SHIFT = 0.03
TEXT_LIMIT = 16000


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")).encode()).hexdigest()


def _representation(result, nodes, papers):
    """Prefer saved latent inputs. Legacy reads have already selected map IDs."""
    saved = (result.get("map") or {}).get("projection_inputs") or {}
    ids = [str(node["id"]) for node in nodes]
    if saved.get("paper_ids") == ids:
        try:
            values = np.asarray(saved["vectors"], dtype=float)
            if (values.ndim == 2 and values.shape[0] == len(ids) and 1 <= values.shape[1] <= 50
                    and np.isfinite(values).all()):
                return values, saved.get("embedding", "tfidf"), saved.get("source", "saved_analysis_representation"), []
        except (KeyError, TypeError, ValueError):
            pass
    lookup = {str(p["id"]): p for p in papers}
    selected = [lookup.get(identifier, {}) for identifier in ids]
    if result.get("meta", {}).get("topic_model") == "nmf" and all(p.get("topic_weights") for p in selected):
        topics = sorted({key for p in selected for key in p["topic_weights"]})
        values = np.asarray([[float(p["topic_weights"].get(topic, 0)) for topic in topics] for p in selected])
        # Older geometry can omit unassigned latent components. Do not claim
        # their aggregated mass is an original component or recoverable basis.
        has_omitted = any(float(p.get("topic_weights_unassigned_mass", 0)) > 1e-8 for p in selected)
        if np.isfinite(values).all() and (values >= 0).all() and len(topics) <= 50:
            values, _ = _projection_inputs(values)
            warning = ["旧 NMF の保存済みトピック重みから再投影しました。未割当成分は復元できないため、保存済み成分内の比較です。"] if has_omitted else []
            return values, "nmf", "legacy_saved_nmf_topic_weights", warning
    texts = [_document(p or {"title": node.get("label", "")}) for p, node in zip(selected, nodes)]
    vectorizer = TfidfVectorizer(stop_words=STOP_WORDS, max_features=3000, ngram_range=(1, 2))
    try:
        matrix = vectorizer.fit_transform(texts)
        values, _ = _projection_inputs(matrix)
    except ValueError:
        values = np.zeros((len(nodes), 1))
    return values, "tfidf", "legacy_sample_tfidf_reconstruction", [
        "旧結果に再投影用の文書表現がないため、表示論文だけの TF-IDF を再構築しました。元の SBERT・LDA 等の空間ではありません。元の表現で比較する場合は再分析してください。"]


def _document(paper):
    return " ".join([str(paper.get("title") or ""), analysis_abstract(str(paper.get("abstract") or "")),
                     " ".join(map(str, paper.get("keywords") or []))])[:TEXT_LIMIT]


@lru_cache(maxsize=12)
def _geometry(vectors, embedding, projection):
    values = np.asarray(vectors, dtype=float)
    if not len(values):
        return [], "表示論文なし", {"algorithm": "none", "requested_method": projection}
    coordinates, method, details = _map_projection(values, embedding, projection)
    return np.round(coordinates, 7).tolist(), method, details


def _period(number, interval):
    if interval == "year":
        return str(number)
    if interval == "quarter":
        year, quarter = divmod(number, 4)
        return f"{year:04d}-Q{quarter + 1}"
    year, month = divmod(number, 12)
    return f"{year:04d}-{month + 1:02d}"


def _period_number(paper, interval):
    if interval == "year":
        year = paper.get("year")
        if isinstance(year, bool):
            return None, "invalid_year"
        try:
            if int(year) == float(year) and 1500 <= int(year) <= date.today().year:
                return int(year), "valid"
        except (ValueError, TypeError, OverflowError):
            pass
        return None, "invalid_year"
    month, reason = _publication_month(paper, date.today())
    return (month // 3 if interval == "quarter" and month is not None else month), reason


def _cosine(first, second):
    denominator = float(np.linalg.norm(first) * np.linalg.norm(second))
    if denominator < 1e-12:
        return None
    return float(np.clip(1 - float(np.dot(first, second)) / denominator, 0, 2))


def _dispersion(values):
    values = values[np.linalg.norm(values, axis=1) > 1e-12]
    if not len(values):
        return None
    center = values.mean(axis=0)
    length = float(np.linalg.norm(center))
    if length < 1e-12:
        return None
    similarity = values @ center / (np.linalg.norm(values, axis=1) * length)
    return float(np.mean(np.clip(1 - similarity, 0, 2)))


def _test_shift(first, second, seed):
    # Empty lexical vectors have no cosine direction. They remain visible and
    # counted on the map but cannot increase the statistical sample size.
    first = first[np.isfinite(first).all(axis=1) & (np.linalg.norm(first, axis=1) > 1e-12)]
    second = second[np.isfinite(second).all(axis=1) & (np.linalg.norm(second, axis=1) > 1e-12)]
    if not len(first) or not len(second):
        return None, None
    observed = _cosine(first.mean(axis=0), second.mean(axis=0))
    if observed is None or min(len(first), len(second)) < MIN_GROUP:
        return observed, None
    combined = np.vstack((first, second))
    random = np.random.default_rng(seed)
    exceeds = 0
    for _ in range(PERMUTATIONS):
        order = random.permutation(len(combined))
        distance = _cosine(combined[order[:len(first)]].mean(axis=0), combined[order[len(first):]].mean(axis=0))
        exceeds += distance is not None and distance >= observed - 1e-12
    return observed, (exceeds + 1) / (PERMUTATIONS + 1)


def _adjust_q(movements):
    tested = sorted((row for row in movements if row["p_value"] is not None), key=lambda row: (row["p_value"], row["id"]))
    upper = 1.0
    for index in range(len(tested) - 1, -1, -1):
        upper = min(upper, tested[index]["p_value"] * len(tested) / (index + 1))
        tested[index]["q_value"] = round(upper, 8)


def _term_counts(papers):
    analyzer = TfidfVectorizer(stop_words=STOP_WORDS, ngram_range=(1, 2),
                              token_pattern=r"(?u)\b\w+(?:-\w+)*\b").build_analyzer()
    def meaningful(term):
        parts = term.split()
        # Publication years and numeric IDs are not emerging technologies.
        # Keep mixed material identifiers such as 316L and 17-4PH intact.
        return (not all(part.isdecimal() for part in parts)
                and not any(re.fullmatch(r"\d{4}", part) and 1500 <= int(part) <= 2100 for part in parts))
    return Counter(term for paper in papers for term in set(analyzer(_document(paper))) if meaningful(term))


def _terms(current, other, n_current, n_other):
    def score(term):
        return (-(current[term] / n_current - other[term] / n_other), -current[term], term)
    return [{"term": term, "count": current[term]} for term in sorted(current, key=score)[:8]]


def _evidence(indices, vectors, nodes):
    centroid = vectors[indices].mean(axis=0)
    def rank(index):
        distance = _cosine(vectors[index], centroid)
        return (distance if distance is not None else math.inf, str(nodes[index]["id"]))
    ranked = sorted(indices, key=rank)
    return [nodes[index]["id"] for index in ranked[:6]]


def _explain(row):
    earlier = "・".join(term["term"] for term in row["from_terms"][:3]) or "特徴語なし"
    later = "・".join(term["term"] for term in row["to_terms"][:3]) or "特徴語なし"
    prefix = f"{row['from_period']} → {row['to_period']}：表示標本 {row['from_count']} 件と {row['to_count']} 件の比較です。"
    if row.get("from_valid_count", row["from_count"]) != row["from_count"] or row.get("to_valid_count", row["to_count"]) != row["to_count"]:
        prefix += f"cosine 比較に使える非零の文書表現は、それぞれ {row['from_valid_count']} 件・{row['to_valid_count']} 件です。"
    if row.get("insufficient_reason") == "unclassified_topic":
        result = "未分類・情報不足の群なので、まとまった話題としての内容移動は判定しません。"
    elif row["gap_periods"]:
        result = f"間に {row['gap_periods']} 期間の観測がないため、継続した移動は判断できません。"
    elif row["status"] == "insufficient":
        result = "各期間 5 件以上の有効な表現が必要なため、移動の統計判定はできません。"
    elif row["status"] == "shift":
        result = "高次元の文書表現で重心変化を検出しました。期間ごとに集まる研究内容の比重が変化した可能性を示します。"
    else:
        result = "設定した検定・変化量の基準では移動を検出していません。変化がないことの証明ではありません。"
    return prefix + result + f"前期の特徴語は「{earlier}」、後期は「{later}」です。赤い矢印は表示上の平均位置の差で、研究者の移動・因果関係・実用化や将来の成功を意味しません。"


def build_landscape(result_id, projection="auto", interval="year", scope="sample"):
    if scope not in {"sample", "full"}:
        raise ValueError("重心の分析対象は sample・full を指定してください。")
    if projection not in PROJECTIONS or interval not in INTERVALS:
        raise ValueError("投影法は auto・tsne・pca・umap、期間は year・quarter・month を指定してください。")
    if scope == "full":
        from .corpus_landscape import build_full_landscape
        return build_full_landscape(result_id, projection, interval)
    result = storage.read("results", result_id, include_papers=False)
    original_map = result.get("map") or {}
    nodes = deepcopy(original_map.get("nodes", [])[:MAP_LIMIT])
    nodes = [node for node in nodes if isinstance(node, dict) and node.get("id")]
    papers = large_storage.papers_by_ids(result, storage.data_root(), [node["id"] for node in nodes])
    lookup = {str(p["id"]): p for p in papers}
    for node in nodes:
        paper = lookup.get(str(node["id"]), {})
        for key in ("year", "publication_date", "date_precision"):
            if key in paper:
                node[key] = paper[key]
    if nodes:
        values, embedding, source, notices = _representation(result, nodes, papers)
    else:
        values, embedding, source, notices = np.zeros((0, 1)), "tfidf", "empty", []
    vector_key = tuple(tuple(float(value) for value in row) for row in values)
    saved_details = original_map.get("projection") or {}
    reuse_saved = (source == "saved_analysis_representation"
                  and saved_details.get("requested_method", "auto") == projection
                  and all(isinstance(node.get(axis), (int, float)) and math.isfinite(node[axis])
                          for node in nodes for axis in ("x", "y")))
    if reuse_saved:
        coordinates = [[node["x"], node["y"]] for node in nodes]
        method, details = original_map.get("method", "保存済み共通投影"), deepcopy(saved_details)
    else:
        coordinates, method, details = _geometry(vector_key, embedding, projection)
    projection_id = _digest({"ids": [node["id"] for node in nodes], "vectors": vector_key,
                             "embedding": embedding, "projection": projection,
                             "coordinates": coordinates, "algorithm": details.get("algorithm"),
                             "version": 2, "scope": "sample"})[:24]
    for node, coordinate in zip(nodes, coordinates):
        node.update(x=coordinate[0], y=coordinate[1])
    groups = defaultdict(list)
    excluded = Counter()
    for index, node in enumerate(nodes):
        number, reason = _period_number(node, interval)
        node["period_id"] = _period(number, interval) if number is not None else None
        if number is None:
            excluded[reason] += 1
        else:
            groups[number].append(index)
    first = min(groups) if groups else None
    last = max(groups) if groups else None
    # Include interior gaps without inventing counts outside observed coverage.
    period_numbers = list(range(first, last + 1)) if first is not None else []
    periods = [{"id": _period(number, interval), "label": _period(number, interval), "index": index,
                "count": len(groups[number]), "node_ids": [nodes[i]["id"] for i in groups[number]],
                "observed": bool(groups[number]), "count_scope": "display_sample"}
               for index, number in enumerate(period_numbers)]
    topics = [{"id": topic["id"], "label": topic.get("label", topic["id"]), "color": topic.get("color", "#42e8cf")}
              for topic in result.get("topics", [])]
    topic_lookup = {topic["id"]: topic for topic in topics}
    unclassified_topics = {topic["id"] for topic in result.get("topics", [])
                           if topic.get("is_outlier") or topic.get("status") == "unclassified"}
    unclassified_topics.update(str(paper.get("topic_id", "unknown")) for paper in papers if paper.get("is_outlier"))
    by_topic = defaultdict(dict)
    centroids = []
    for number in period_numbers:
        period_topics = defaultdict(list)
        for index in groups[number]:
            period_topics[str(nodes[index].get("topic_id", "unknown"))].append(index)
        for topic, indices in sorted(period_topics.items()):
            by_topic[topic][number] = indices
            xy = np.mean([[nodes[i]["x"], nodes[i]["y"]] for i in indices], axis=0)
            centroids.append({"topic_id": topic, "period_id": _period(number, interval), "count": len(indices),
                              "x": float(xy[0]), "y": float(xy[1]), "paper_ids": [nodes[i]["id"] for i in indices],
                              "count_scope": "display_sample", "scope": "sample",
                              "topic_label": topic_lookup.get(topic, {}).get("label", topic),
                              "evidence_ids": _evidence(indices, values, nodes),
                              "terms": [{"term": term, "count": count} for term, count in
                                        sorted(_term_counts([lookup.get(str(nodes[i]["id"]), nodes[i]) for i in indices]).items(),
                                               key=lambda item: (-item[1], item[0]))[:8]],
                              "period_count": len(groups[number]), "share_of_period": len(indices) / len(groups[number]),
                              "valid_vector_count": int(np.count_nonzero(np.linalg.norm(values[indices], axis=1) > 1e-12)),
                              "dispersion": _dispersion(values[indices])})
    centroid_lookup = {(row["topic_id"], row["period_id"]): row for row in centroids}
    movements = []
    for topic, history in sorted(by_topic.items()):
        ordered = sorted(history)
        for previous, following in zip(ordered, ordered[1:]):
            before, after = history[previous], history[following]
            old_period, new_period = _period(previous, interval), _period(following, interval)
            identifier = _digest([projection_id, topic, old_period, new_period])[:24]
            # Reprojection changes only display coordinates, never the test's
            # random permutations or the inferred high-dimensional shift.
            test_seed = int(_digest([topic, old_period, new_period,
                [nodes[i]["id"] for i in before + after]])[:8], 16)
            distance, p_value = _test_shift(values[before], values[after], test_seed)
            gap = following - previous - 1
            if gap or topic in unclassified_topics:
                p_value = None
            old, new = centroid_lookup[(topic, old_period)], centroid_lookup[(topic, new_period)]
            old_terms = _term_counts([lookup.get(str(nodes[i]["id"]), nodes[i]) for i in before])
            new_terms = _term_counts([lookup.get(str(nodes[i]["id"]), nodes[i]) for i in after])
            movements.append({"id": identifier, "topic_id": topic,
                "topic_label": topic_lookup.get(topic, {}).get("label", topic),
                "from_period": old_period, "to_period": new_period,
                "from": {"x": old["x"], "y": old["y"]}, "to": {"x": new["x"], "y": new["y"]},
                "distance_2d": round(math.hypot(new["x"] - old["x"], new["y"] - old["y"]), 8),
                "cosine_distance": round(distance, 8) if distance is not None else None,
                "p_value": p_value, "q_value": None, "status": "insufficient", "gap_periods": gap,
                "insufficient_reason": "unclassified_topic" if topic in unclassified_topics else "gap" if gap else "sample_or_representation" if p_value is None else None,
                "from_count": len(before), "to_count": len(after), "count_scope": "display_sample",
                "from_valid_count": int(np.count_nonzero(np.linalg.norm(values[before], axis=1) > 1e-12)),
                "to_valid_count": int(np.count_nonzero(np.linalg.norm(values[after], axis=1) > 1e-12)),
                "from_terms": _terms(old_terms, new_terms, len(before), len(after)),
                "to_terms": _terms(new_terms, old_terms, len(after), len(before)),
                "evidence_before": _evidence(before, values, nodes), "evidence_after": _evidence(after, values, nodes)})
    _adjust_q(movements)
    for movement in movements:
        if movement["q_value"] is not None:
            movement["status"] = ("shift" if movement["q_value"] <= .05 and
                                  movement["cosine_distance"] >= MIN_COSINE_SHIFT else "stable")
        movement["explanation"] = _explain(movement)
    if excluded:
        notices.append(f"表示標本の {sum(excluded.values())} 件は有効な出版{'年' if interval == 'year' else '月'}がなく層別集計から除外しました。年のみの記録を 1 月へ配分しません。")
    if any(not period["observed"] for period in periods):
        notices.append("空の層は表示標本に観測がない期間です。分野全体に論文がないことや、取得範囲が完全であることは示しません。")
    limitations = [
        "重心・件数・検定は最大 400 件の表示標本内の探索的比較で、全論文や分野全体の推測統計ではありません。",
        "全期間の同一文書表現を一度だけ投影し、期間の切替で座標を再学習しません。投影法を変更すると座標系自体は変わります。",
        "重心の移動は各期間に集まる内容の平均的な構成差です。同じ論文や研究者が時空間を移動したという意味ではありません。",
        "t-SNE・UMAP の距離・方向・密度は歪み得ます。判定は投影前の保存表現で行い、因果関係や将来の流行は判定しません。",
        "各期 5 件以上、199 回のラベル置換検定、画面内の比較に BH 補正 q≤0.05、cosine 距離≥0.03 を検出基準にします。固定閾値による探索判定です。",
        "零ベクトルは方向が未定義なので、cosine 比較・最小件数・置換検定から除外します。表示件数と 2D 重心には保持し、有効件数を別記します。",
        "同じ著者の連続論文等は独立でない場合があります。収録範囲・標本選択・検出力・語彙変化による影響を確認してください。",
        "等高線と山の高さは全表示文書の共通 2D 相対密度です。層は各期間の散布点で、論文件数や将来性の高さではありません。"]
    shifted = sum(row["status"] == "shift" for row in movements)
    interpretation = {"summary": f"{len(nodes)} 件の共通座標を {len(periods)} 層で比較し、内容重心の変化候補を {shifted} 件検出しました。",
        "methodology": "全期間で固定した表示文書表現 → 共通 2D 投影 → 期間・話題別の等重み重心。2D の矢印と高次元の変化判定を併記します。",
        "limitations": limitations, "topic_summaries": [{"topic_id": topic, "label": topic_lookup.get(topic, {}).get("label", topic),
            "text": " ".join(row["explanation"] for row in movements if row["topic_id"] == topic)} for topic in sorted(by_topic)]}
    public_map = {"nodes": nodes, "edges": deepcopy(original_map.get("edges", [])), "method": method,
                  "projection": {**details, "representation_source": source},
                  "truncated": bool(original_map.get("truncated"))}
    # Legacy edges may have used a different input representation. Drop rather
    # than relabel those similarities as reconstructed TF-IDF neighbors.
    if source == "legacy_sample_tfidf_reconstruction":
        public_map["edges"] = []
    return {"result_id": result_id, "projection_id": projection_id, "map": public_map,
            "terrain": build_terrain(nodes), "topics": topics, "periods": periods,
            "centroids": centroids, "movements": movements, "warnings": notices,
            "meta": {"interval": interval, "scope": "sample", "count_scope": "display_sample", "displayed_papers": len(nodes),
                     "map_displayed_papers": len(nodes), "analysis_papers": len(nodes),
                     "corpus_papers": result.get("summary", {}).get("papers", result.get("meta", {}).get("paper_count", len(nodes))),
                     "eligible_papers": len(nodes) - sum(excluded.values()), "excluded_date_count": sum(excluded.values()),
                     "excluded_date_reasons": dict(excluded), "representation_source": source,
                     "representation_dimensions": values.shape[1], "coordinate_scope": "shared_all_periods",
                     "centroid_weighting": "equal_per_displayed_paper", "permutations": PERMUTATIONS,
                     "minimum_group_count": MIN_GROUP, "minimum_cosine_shift": MIN_COSINE_SHIFT,
                     "q_threshold": .05, "term_text_character_limit": TEXT_LIMIT,
                     "term_count_unit": "document_frequency", "projection_id": projection_id},
            "interpretation": interpretation}
