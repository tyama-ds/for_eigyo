"""Whole-corpus temporal centroids with bounded display and permutation work.

All rows contribute to coordinates, counts, term frequencies and evidence
selection. Only expensive nonlinear fitting and hypothesis tests use explicitly
reported reference samples. No paper-count ceiling is imposed here.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from copy import deepcopy
from functools import lru_cache
import hashlib
import math
import threading

import numpy as np
from scipy import sparse
from sklearn.decomposition import PCA, TruncatedSVD
from sklearn.feature_extraction.text import CountVectorizer, TfidfTransformer, TfidfVectorizer

from . import large_storage, storage
from .analytics import MAP_LIMIT, STOP_WORDS, _map_projection, _normalize_positions
from .landscape import (_adjust_q, _cosine, _digest, _dispersion, _document, _period,
                        _period_number, _test_shift, MIN_GROUP, MIN_COSINE_SHIFT, PERMUTATIONS, TEXT_LIMIT)
from .map_terrain import build_terrain

LEXICAL_FEATURES = 6000
TERM_FEATURES = 3000
VOCAB_CANDIDATES = 60000
READ_BATCH = 4096
TERM_COUNT_BATCH_ENTRIES = 200000
PERMUTATION_GROUP_LIMIT = 200
UMAP_REFERENCE_LIMIT = 2000
FULL_TSNE_LIMIT = 4000
_LOCK = threading.RLock()
_ANALYZER = TfidfVectorizer(stop_words=STOP_WORDS, ngram_range=(1, 2),
                           token_pattern=r"(?u)\b\w+(?:-\w+)*\b").build_analyzer()


def _tokens(paper):
    for term in set(_ANALYZER(_document(paper))):
        parts = term.split()
        if all(part.isdecimal() for part in parts):
            continue
        if any(len(part) == 4 and part.isdecimal() and 1500 <= int(part) <= 2100 for part in parts):
            continue
        yield term


def _revision(result_id):
    try:
        path = storage._path("results", result_id)
        stat = path.stat()
        return str(storage.data_root().resolve()), result_id, stat.st_mtime_ns, stat.st_size
    except FileNotFoundError as exc:
        raise KeyError("データが見つかりません。") from exc


def _read_papers(result, root):
    yield from large_storage.iter_papers(result, root)


def _lexical_vectors(result, root, vocabulary):
    if not vocabulary:
        return np.zeros((sum(1 for _ in _read_papers(result, root)), 1), dtype=np.float32), "empty_vocabulary"
    counter = CountVectorizer(vocabulary={word: index for index, word in enumerate(vocabulary)},
                              analyzer=lambda text: list(_ANALYZER(text)), dtype=np.float32)
    blocks, batch = [], []
    for paper in _read_papers(result, root):
        batch.append(_document(paper))
        if len(batch) >= READ_BATCH:
            blocks.append(counter.transform(batch))
            batch = []
    if batch:
        blocks.append(counter.transform(batch))
    if not blocks:
        return np.zeros((0, 1), dtype=np.float32), "empty_corpus"
    matrix = sparse.vstack(blocks, format="csr")
    del blocks
    matrix = TfidfTransformer(sublinear_tf=True).fit_transform(matrix)
    dimensions = min(50, matrix.shape[1] - 1, max(1, matrix.shape[0] - 1))
    if matrix.shape[1] > 50 and dimensions:
        vectors = TruncatedSVD(n_components=dimensions, random_state=42).fit_transform(matrix)
        reduction = f"full_corpus_svd_{dimensions}d"
    else:
        vectors, reduction = matrix.toarray(), "none"
    vectors = np.asarray(vectors, dtype=np.float32)
    vectors /= np.maximum(np.linalg.norm(vectors, axis=1), 1e-12)[:, None]
    return vectors, reduction


@lru_cache(maxsize=2)
def _load(revision):
    root, result_id, _, _ = revision
    result = storage.read("results", result_id, include_papers=False)
    description = result.get("meta", {}).get("landscape_representation") or {}
    mode = ("saved" if description.get("basis_scope") == "full_corpus" else
            "nmf" if result.get("meta", {}).get("topic_model") == "nmf" else "tfidf")
    records, blocks, batch, dimensions, components = [], [], [], None, None
    valid_vectors, omitted_components, candidate_pruned = True, False, False
    frequencies = Counter()
    for paper in _read_papers(result, root):
        records.append({"id": str(paper["id"]), "label": str(paper.get("title") or "")[:350],
                        "topic_id": str(paper.get("topic_id") or "unknown"), "year": paper.get("year"),
                        "publication_date": paper.get("publication_date", ""),
                        "date_precision": paper.get("date_precision"),
                        "is_outlier": bool(paper.get("is_outlier")), "citations": paper.get("citations") or 0})
        frequencies.update(_tokens(paper))
        if len(frequencies) > VOCAB_CANDIDATES:
            frequencies = Counter(dict(sorted(frequencies.items(), key=lambda item: (-item[1], item[0]))[:VOCAB_CANDIDATES // 2]))
            candidate_pruned = True
        if mode == "saved":
            raw = paper.get("landscape_vector")
        elif mode == "nmf":
            weights = paper.get("topic_weights") or {}
            if components is None and weights:
                components = sorted(weights)
            raw = [weights.get(topic, 0) for topic in components] if weights and components and set(weights) <= set(components) else None
            omitted_components |= float(paper.get("topic_weights_unassigned_mass") or 0) > 1e-8
        else:
            continue
        try:
            vector = np.asarray(raw, dtype=np.float32)
            if vector.ndim != 1 or not 1 <= len(vector) <= 50 or not np.isfinite(vector).all():
                raise ValueError
            if dimensions is None:
                dimensions = len(vector)
            if len(vector) != dimensions:
                raise ValueError
            batch.append(vector)
        except (TypeError, ValueError, OverflowError):
            valid_vectors = False
        if len(batch) >= READ_BATCH:
            blocks.append(np.asarray(batch, dtype=np.float32))
            batch = []
    notices = []
    vocabulary = [term for term, _ in sorted(frequencies.items(), key=lambda item: (-item[1], item[0]))[:LEXICAL_FEATURES]]
    term_vocabulary = vocabulary[:TERM_FEATURES]
    if batch:
        blocks.append(np.asarray(batch, dtype=np.float32))
    if valid_vectors and mode in {"saved", "nmf"} and records and blocks:
        vectors = np.vstack(blocks)
        vectors /= np.maximum(np.linalg.norm(vectors, axis=1), 1e-12)[:, None]
        source = "saved_full_corpus_representation" if mode == "saved" else "legacy_full_nmf_topic_weights"
        embedding = description.get("embedding", "nmf") if mode == "saved" else "nmf"
        reduction = description.get("reduction", "none")
        if mode == "nmf":
            notices.append("全論文の保存済み NMF トピック重みを同じ成分順で使用しました。")
            if omitted_components:
                notices.append("旧 NMF の未割当成分は復元できないため、保存されている成分内で全論文を比較しています。")
    else:
        vectors, reduction = _lexical_vectors(result, root, vocabulary)
        source, embedding = "full_corpus_tfidf_reconstruction", "tfidf"
        notices.append("全論文の共通表現が未保存のため、全件を読み込んで共通 TF-IDF と最大 50 次元の SVD を再構築しました。元の SBERT・LDA の表現ではありません。")
    if candidate_pruned:
        notices.append("語彙候補を流し読みで絞った後、選択語の出現件数を全論文から再集計します。稀な語が特徴語の候補から外れることがあります。")
    if len(vectors) != len(records):
        raise ValueError("全件の文書表現と論文件数が一致しません。再分析してください。")
    if not records:
        vectors = np.zeros((0, 1), dtype=np.float32)
    topics = [{"id": topic["id"], "label": topic.get("label", topic["id"]), "color": topic.get("color", "#42e8cf")}
              for topic in result.get("topics", [])]
    unknown = {topic["id"] for topic in result.get("topics", []) if topic.get("is_outlier") or topic.get("status") == "unclassified"}
    unknown.update(record["topic_id"] for record in records if record["is_outlier"])
    retained_result = {"id": result_id, "papers": []}
    if (result.get("_large_store") or {}).get("count", 0) > 0:
        retained_result["_large_store"] = result["_large_store"]
    else:
        # Small legacy JSON files are re-read for term frequencies; cache only
        # compact metadata and vectors, not a second copy of all abstracts.
        retained_result = None
    return {"records": records, "vectors": vectors, "source": source, "embedding": embedding,
            "reduction": reduction, "original_dimensions": description.get("original_dimensions", len(components or vocabulary)),
            "warnings": notices, "topics": topics, "unclassified": unknown,
            "display_ids": [str(node["id"]) for node in (result.get("map") or {}).get("nodes", [])[:MAP_LIMIT]],
            "term_vocabulary": term_vocabulary, "vocabulary_pruned": candidate_pruned,
            "retained_result": retained_result}


def _references(records, count):
    order = sorted(range(len(records)), key=lambda index: (records[index]["topic_id"], records[index].get("year") or 0, records[index]["id"]))
    return np.asarray([order[index] for index in np.linspace(0, len(order) - 1, min(count, len(order)), dtype=int)], dtype=int)


def _has_nonlinear_variety(vectors):
    """Stop after three unique directions; no all-corpus unique matrix."""
    seen = set()
    for start in range(0, len(vectors), READ_BATCH):
        rounded = np.round(vectors[start:start + READ_BATCH], 9)
        rounded[rounded == 0] = 0  # Treat +0 and -0 as the same direction.
        for row in rounded:
            seen.add(row.tobytes())
            if len(seen) >= 3:
                return True
    return False


@lru_cache(maxsize=2)
def _project(revision, requested):
    state = _load(revision)
    vectors, records = state["vectors"], state["records"]
    n = len(records)
    actual = "pca" if requested == "auto" else requested
    details = {"requested_method": requested, "algorithm": actual, "sample_count": n,
               "input_dimensions": vectors.shape[1], "basis_scope": "full_corpus",
               "coordinate_scope": "shared_all_periods", "random_state": 42,
               "selection_reason": "full_corpus_scalable_pca" if requested == "auto" else "user_selected"}
    if actual == "tsne" and n > FULL_TSNE_LIMIT:
        raise ValueError(f"全件 t-SNE の投影対象は {FULL_TSNE_LIMIT:,} 件までです。全件 PCA または全件 UMAP を選択してください。分析対象件数そのものの制限ではありません。")
    if n < 2 or vectors.shape[1] < 2:
        coordinates = np.full((n, 2), .5)
        details.update(algorithm="pca", fallback_reason="tiny_or_one_dimensional")
    elif actual == "pca":
        estimator = PCA(n_components=2, svd_solver="full")
        coordinates = _normalize_positions(estimator.fit_transform(vectors))
        details["explained_variance_ratio"] = np.nan_to_num(estimator.explained_variance_ratio_).tolist()
        details.update(fit_papers=n, transform_papers=n, approximation="none")
    elif actual == "tsne":
        coordinates, _, measured = _map_projection(vectors.copy(), state["embedding"], "tsne")
        details.update(measured, fit_papers=n, transform_papers=n, approximation="none")
    else:
        try:
            from umap import UMAP
        except ImportError as exc:
            raise ValueError("UMAP が未導入です。requirements.txt の umap-learn をインストールしてください。") from exc
        if n < 6 or not _has_nonlinear_variety(vectors):
            coordinates, _, measured = _map_projection(vectors.copy(), state["embedding"], "pca")
            details.update(measured, requested_method=requested,
                           fallback_reason="tiny_sample" if n < 6 else "insufficient_unique_vectors",
                           fit_papers=n, transform_papers=n, approximation="none")
        else:
            reference = _references(records, UMAP_REFERENCE_LIMIT)
            estimator = UMAP(n_components=2, n_neighbors=min(15, len(reference) - 1), metric="cosine",
                             min_dist=.1, random_state=42, transform_seed=42, n_jobs=1, init="random")
            fitted = estimator.fit_transform(vectors[reference])
            if len(reference) == n:
                raw = np.zeros((n, 2), dtype=np.float32)
                raw[reference] = fitted
            else:
                raw = np.empty((n, 2), dtype=np.float32)
                for start in range(0, n, READ_BATCH):
                    raw[start:start + READ_BATCH] = estimator.transform(vectors[start:start + READ_BATCH])
                raw[reference] = fitted
            if not np.isfinite(raw).all():
                raise ValueError("全件 UMAP の座標が非有限値になりました。PCA を選択してください。")
            coordinates = _normalize_positions(raw)
            details.update(fit_papers=len(reference), transform_papers=n,
                           approximation="reference_fit_transform_all" if len(reference) < n else "none",
                           n_neighbors=min(15, len(reference) - 1), min_dist=.1, metric="cosine")
    if not np.isfinite(coordinates).all():
        raise ValueError("全件投影に非有限値が含まれます。PCA を選択して確認してください。")
    digest = hashlib.sha256()
    digest.update(_digest({"revision": revision, "scope": "full", "requested": requested, "details": details, "version": 1}).encode())
    if n:
        digest.update(memoryview(np.ascontiguousarray(vectors)).cast("B"))
        digest.update(memoryview(np.ascontiguousarray(coordinates)).cast("B"))
    return coordinates, details, digest.hexdigest()[:24]


def _nearest(indices, vectors, records, limit=6):
    valid = indices[np.linalg.norm(vectors[indices], axis=1) > 1e-12]
    if not len(valid):
        return []
    center = vectors[valid].mean(axis=0, dtype=np.float64)
    norm = float(np.linalg.norm(center))
    if norm < 1e-12:
        return []
    distances = np.clip(1 - vectors[valid] @ center / (np.linalg.norm(vectors[valid], axis=1) * norm), 0, 2)
    # argpartition narrows the candidates; stable ID ordering resolves ties.
    take = min(limit, len(valid))
    threshold = np.partition(distances, take - 1)[take - 1]
    candidates = np.flatnonzero(distances <= threshold)
    ranked = sorted(candidates, key=lambda index: (float(distances[index]), records[valid[index]]["id"]))[:take]
    return [records[valid[index]]["id"] for index in ranked]


def _permutation(first_indices, second_indices, vectors, seed):
    first = first_indices[np.linalg.norm(vectors[first_indices], axis=1) > 1e-12]
    second = second_indices[np.linalg.norm(vectors[second_indices], axis=1) > 1e-12]
    n_first, n_second = len(first), len(second)
    cosine = (_cosine(vectors[first].mean(axis=0, dtype=np.float64), vectors[second].mean(axis=0, dtype=np.float64))
              if n_first and n_second else None)
    random = np.random.default_rng(seed)
    if n_first > PERMUTATION_GROUP_LIMIT:
        first = first[np.sort(random.choice(n_first, PERMUTATION_GROUP_LIMIT, replace=False))]
    if n_second > PERMUTATION_GROUP_LIMIT:
        second = second[np.sort(random.choice(n_second, PERMUTATION_GROUP_LIMIT, replace=False))]
    if cosine is None or min(n_first, n_second) < MIN_GROUP:
        test_cosine, p_value = cosine, None
    else:
        test_cosine, p_value = _test_shift(vectors[first], vectors[second], seed)
    return cosine, p_value, test_cosine, n_first, n_second, len(first), len(second)


def _top_terms(counts, vocabulary, other=None, count=1, other_count=1):
    if sparse.issparse(counts):
        counts = counts.toarray().ravel()
    if sparse.issparse(other):
        other = other.toarray().ravel()
    selected = np.flatnonzero(counts)
    if other is None:
        order = sorted(selected, key=lambda i: (-int(counts[i]), vocabulary[i]))
    else:
        order = sorted(selected, key=lambda i: (-(int(counts[i]) / count - int(other[i]) / other_count), -int(counts[i]), vocabulary[i]))
    return [{"term": vocabulary[i], "count": int(counts[i])} for i in order[:8]]


@lru_cache(maxsize=6)
def _build(revision, projection, interval):
    state = _load(revision)
    records, vectors = state["records"], state["vectors"]
    coordinates, details, projection_id = _project(revision, projection)
    n = len(records)
    topic_lookup = {topic["id"]: topic for topic in state["topics"]}
    indices_by_id = {record["id"]: i for i, record in enumerate(records)}
    shown = list(dict.fromkeys(indices_by_id[identifier] for identifier in state["display_ids"] if identifier in indices_by_id))
    if not shown and n:
        shown = _references(records, MAP_LIMIT).tolist()
    shown = shown[:MAP_LIMIT]
    by_period, by_group = defaultdict(list), defaultdict(list)
    period_for_row, excluded = [], Counter()
    for index, record in enumerate(records):
        period, reason = _period_number(record, interval)
        period_for_row.append(period)
        if period is None:
            excluded[reason] += 1
        else:
            by_period[period].append(index)
            by_group[(record["topic_id"], period)].append(index)
    group_keys = sorted(by_group)
    group_index = {key: index for index, key in enumerate(group_keys)}
    # Accumulate sparse document-frequency counts in bounded entry buffers.
    # Dense groups x vocabulary can exceed 700 MB for 100 topics x 600 months.
    vocabulary = state["term_vocabulary"]
    word_index = {word: index for index, word in enumerate(vocabulary)}
    term_counts = sparse.csr_matrix((len(group_keys), len(vocabulary)), dtype=np.uint32)
    count_rows, count_columns = [], []
    def flush_counts():
        nonlocal term_counts
        if not count_rows:
            return
        batch = sparse.coo_matrix((np.ones(len(count_rows), dtype=np.uint32),
            (np.asarray(count_rows, dtype=np.int32), np.asarray(count_columns, dtype=np.int32))),
            shape=term_counts.shape).tocsr()
        term_counts = term_counts + batch
        count_rows.clear()
        count_columns.clear()
    result = state["retained_result"] or storage.read("results", revision[1], include_papers=False)
    for index, paper in enumerate(_read_papers(result, revision[0])):
        period = period_for_row[index]
        if period is None:
            continue
        group = group_index[(records[index]["topic_id"], period)]
        terms = [word_index[word] for word in _tokens(paper) if word in word_index]
        if terms:
            count_rows.extend([group] * len(terms))
            count_columns.extend(terms)
            if len(count_rows) >= TERM_COUNT_BATCH_ENTRIES:
                flush_counts()
    flush_counts()
    period_numbers = list(range(min(by_period), max(by_period) + 1)) if by_period else []
    nodes = [{**records[index], "x": float(coordinates[index, 0]), "y": float(coordinates[index, 1]),
              "period_id": _period(period_for_row[index], interval) if period_for_row[index] is not None else None}
             for index in shown]
    periods = [{"id": _period(number, interval), "label": _period(number, interval), "index": index,
                "count": len(by_period[number]), "node_ids": [node["id"] for node in nodes if node["period_id"] == _period(number, interval)],
                "observed": bool(by_period[number]), "count_scope": "full_corpus"}
               for index, number in enumerate(period_numbers)]
    centroids, by_topic = [], defaultdict(dict)
    for (topic, period), raw_indices in sorted(by_group.items()):
        indices = np.asarray(raw_indices, dtype=int)
        by_topic[topic][period] = indices
        xy = coordinates[indices].mean(axis=0, dtype=np.float64)
        evidence = _nearest(indices, vectors, records)
        centroids.append({"topic_id": topic, "topic_label": topic_lookup.get(topic, {}).get("label", topic),
            "period_id": _period(period, interval), "count": len(indices), "x": float(xy[0]), "y": float(xy[1]),
            "count_scope": "full_corpus", "scope": "full", "paper_ids": evidence,
            "paper_ids_total": len(indices), "paper_ids_truncated": len(evidence) < len(indices), "evidence_ids": evidence,
            "terms": _top_terms(term_counts[group_index[(topic, period)]], vocabulary),
            "period_count": len(by_period[period]), "share_of_period": len(indices) / len(by_period[period]),
            "valid_vector_count": int(np.count_nonzero(np.linalg.norm(vectors[indices], axis=1) > 1e-12)),
            "dispersion": _dispersion(vectors[indices])})
    centroid_lookup = {(row["topic_id"], row["period_id"]): row for row in centroids}
    movements = []
    for topic, history in sorted(by_topic.items()):
        ordered = sorted(history)
        for previous, following in zip(ordered, ordered[1:]):
            before, after = history[previous], history[following]
            from_period, to_period = _period(previous, interval), _period(following, interval)
            old, new = centroid_lookup[(topic, from_period)], centroid_lookup[(topic, to_period)]
            seed = int(_digest([topic, from_period, to_period, "full", 42])[:8], 16)
            cosine, p_value, test_cosine, n_first, n_second, tested_first, tested_second = _permutation(before, after, vectors, seed)
            gap = following - previous - 1
            if gap or topic in state["unclassified"]:
                p_value = None
            movement = {"id": _digest([projection_id, topic, from_period, to_period])[:24],
                "topic_id": topic, "topic_label": topic_lookup.get(topic, {}).get("label", topic),
                "from_period": from_period, "to_period": to_period,
                "from": {"x": old["x"], "y": old["y"]}, "to": {"x": new["x"], "y": new["y"]},
                "distance_2d": math.hypot(new["x"] - old["x"], new["y"] - old["y"]),
                "cosine_distance": cosine, "p_value": p_value, "q_value": None, "status": "insufficient",
                "from_count": len(before), "to_count": len(after), "from_valid_count": n_first, "to_valid_count": n_second,
                "count_scope": "full_corpus", "scope": "full", "gap_periods": gap,
                "test_from_count": tested_first, "test_to_count": tested_second, "test_cosine_distance": test_cosine,
                "p_value_scope": "bounded_permutation_sample" if max(n_first, n_second) > PERMUTATION_GROUP_LIMIT else "all_group_vectors",
                "insufficient_reason": "unclassified_topic" if topic in state["unclassified"] else "gap" if gap else "sample_or_representation" if p_value is None else None,
                "from_terms": _top_terms(term_counts[group_index[(topic, previous)]], vocabulary,
                    term_counts[group_index[(topic, following)]], len(before), len(after)),
                "to_terms": _top_terms(term_counts[group_index[(topic, following)]], vocabulary,
                    term_counts[group_index[(topic, previous)]], len(after), len(before)),
                "evidence_before": old["evidence_ids"], "evidence_after": new["evidence_ids"]}
            movements.append(movement)
    _adjust_q(movements)
    for movement in movements:
        if movement["q_value"] is not None:
            movement["status"] = "shift" if movement["q_value"] <= .05 and movement["cosine_distance"] >= MIN_COSINE_SHIFT else "stable"
        before = "・".join(term["term"] for term in movement["from_terms"][:3]) or "特徴語なし"
        after = "・".join(term["term"] for term in movement["to_terms"][:3]) or "特徴語なし"
        status = {"shift": "内容構成の変化候補を検出しました。", "stable": "設定基準では移動を検出していません。変化がないことの証明ではありません。",
                  "insufficient": "有効件数・期間の連続性・話題の分類状況から、移動の判定を保留します。"}[movement["status"]]
        movement["explanation"] = (f"{movement['from_period']} → {movement['to_period']}：取り込み済み全論文のうち、前期 {movement['from_count']} 件・後期 {movement['to_count']} 件の等重み重心です。"
            f"{status}前期の特徴語は「{before}」、後期は「{after}」です。内容差・特徴語の件数・代表論文は全件から計算し、"
            f"置換検定は有効な前期 {movement['test_from_count']} 件・後期 {movement['test_to_count']} 件で実施しています。"
            "矢印は同じ座標での内容構成の平均位置の差で、研究者の移動・因果関係・将来の成功を意味しません。")
    notices = list(state["warnings"])
    if projection == "umap" and details.get("algorithm") != "umap":
        notices.append("論文数・異なる文書表現が少ないため、全件 UMAP の代わりに共通 PCA の線形配置を使用しています。")
    if excluded:
        notices.append(f"全論文中 {sum(excluded.values())} 件を、出版{'年' if interval == 'year' else '月'}の不足・矛盾から層別集計から除外しました。年のみの記録を 1 月へ配分しません。")
    if any(not row["observed"] for row in periods):
        notices.append("空の層は取り込み集合に該当論文がない期間です。世界全体に論文がないことは意味しません。")
    if details.get("approximation") == "reference_fit_transform_all":
        notices.append(f"UMAP は {details['fit_papers']:,} 件の参照点で学習し、全 {n:,} 件を同じモデルで配置しています。非線形座標は近似ですが、重心は配置された全件から計算します。")
    limitations = [
        "全件とは、この保存済み分析結果に含まれる論文全部です。外部の未取得論文や年範囲外の論文は含みません。",
        "図の点と密度等高線は最大 400 件の表示標本です。期間別件数・重心・内容差・特徴語の件数・代表論文の選択は全件です。",
        "PCA は全論文の共通表現で一度だけ学習し、全座標の重心を集計します。年・四半期・月で再学習しません。UMAP の参照学習時は近似配置と明記します。",
        "内容重心の cosine 距離は全ての有効ベクトルから厳密に計算します。p 値・q 値は各群最大 200 件の乱数固定標本による 199 回の置換検定で、大きい群では近似です。",
        "各群の有効ベクトル 5 件以上、BH 補正 q≤0.05、全件 cosine 距離≥0.03 を探索基準にします。有望性・因果関係・将来予測の認定ではありません。",
        "零ベクトルは cosine 比較・有効件数・置換検定から除外し、表示件数と 2D 重心には保持します。",
        f"特徴語は共通候補最大 {TERM_FEATURES:,} 語について、全件のタイトル・抄録・キーワード先頭 {TEXT_LIMIT:,} 字内の論文単位出現数を数えます。候補外の語や長文の後半は含みません。",
        "同じ著者の連続論文、検索条件、収録範囲、期間間の件数差による影響を確認してください。赤い矢印の方向に普遍的な技術的意味はありません。"]
    shifted = sum(row["status"] == "shift" for row in movements)
    return {"result_id": revision[1], "projection_id": projection_id,
            "map": {"nodes": nodes, "edges": [], "method": f"全 {n:,} 件の共通表現 → {details['algorithm'].upper()} / 表示 {len(nodes):,} 件",
                    "projection": {**details, "representation_source": state["source"]}, "truncated": len(nodes) < n},
            "terrain": build_terrain(nodes), "topics": state["topics"], "periods": periods,
            "centroids": centroids, "movements": movements, "warnings": notices,
            "meta": {"scope": "full", "interval": interval, "count_scope": "full_corpus", "analysis_papers": n,
                     "map_displayed_papers": len(nodes), "displayed_papers": len(nodes), "corpus_papers": n,
                     "eligible_papers": n - sum(excluded.values()), "excluded_date_count": sum(excluded.values()),
                     "excluded_date_reasons": dict(excluded), "representation_source": state["source"],
                     "representation_dimensions": vectors.shape[1], "representation_reduction": state["reduction"],
                     "representation_original_dimensions": state["original_dimensions"], "coordinate_scope": "shared_all_periods",
                     "centroid_weighting": "equal_per_corpus_paper", "permutations": PERMUTATIONS,
                     "minimum_group_count": MIN_GROUP, "minimum_cosine_shift": MIN_COSINE_SHIFT,
                     "q_threshold": .05, "test_scope": "bounded_permutation_sample", "permutation_group_limit": PERMUTATION_GROUP_LIMIT,
                     "term_text_character_limit": TEXT_LIMIT, "term_vocabulary_limit": TERM_FEATURES,
                     "term_count_unit": "document_frequency", "projection_id": projection_id},
            "interpretation": {"summary": f"取り込み済み全 {n:,} 件から {len(periods)} 層の重心を計算し、内容変化候補を {shifted} 件検出しました。図には {len(nodes):,} 件を表示しています。",
                "methodology": "全件の共通表現 → 同一座標への配置 → 全件の期間・話題別重心。統計検定の近似範囲と図の表示件数を別記します。",
                "limitations": limitations, "topic_summaries": [{"topic_id": topic, "label": topic_lookup.get(topic, {}).get("label", topic),
                    "text": " ".join(row["explanation"] for row in movements if row["topic_id"] == topic)} for topic in sorted(by_topic)]}}


def build_full_landscape(result_id, projection="auto", interval="year"):
    revision = _revision(result_id)
    # Coalesce simultaneous period-switch requests so they share one full
    # vector build instead of duplicating a large working set in memory.
    with _LOCK:
        return deepcopy(_build(revision, projection, interval))
