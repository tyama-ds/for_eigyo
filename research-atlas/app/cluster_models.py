"""Document clustering with explicit algorithms, all-row fitting and memory guards.

Every method receives the same whole-corpus, <= 50-dimensional representation.
Only its columns are reduced; papers are never sampled or silently reassigned.
Zero vectors and density/graph noise remain in a separate unclassified group.

References:
https://scikit-learn.org/stable/modules/clustering.html
https://scikit-learn.org/stable/modules/generated/sklearn.cluster.DBSCAN.html
https://scikit-learn.org/stable/modules/generated/sklearn.mixture.GaussianMixture.html
https://www.cs.cmu.edu/~dpelleg/download/xmeans.pdf
"""

from __future__ import annotations

from collections import Counter
import warnings as python_warnings

import numpy as np
from scipy import sparse
from scipy.sparse.csgraph import connected_components
from sklearn.cluster import AgglomerativeClustering, Birch, KMeans, MiniBatchKMeans
from sklearn.decomposition import TruncatedSVD
from sklearn.exceptions import ConvergenceWarning
from sklearn.mixture import GaussianMixture
from sklearn.neighbors import BallTree


RANDOM_STATE = 42
MAX_DIMENSIONS = 50
MAX_VECTOR_BYTES = 512 * 1024 * 1024
MAX_GRAPH_EDGES = 8_000_000
QUERY_PAIR_BUDGET = 1_000_000
MAX_AGGLOMERATIVE_PAPERS = 6000
MAX_BIRCH_SUBCLUSTERS = 4000
MAX_GMM_RESPONSIBILITY_BYTES = 256 * 1024 * 1024
BATCH_SIZE = 2048

CLUSTER_MODELS = {
    "kmeans": {"label": "K-means（ランダム初期化）", "uses_n_clusters": True,
               "description": "ランダムな初期中心から全論文のクラスタ内平方和を最小化します。", "scalability": "large"},
    "kmeans_pp": {"label": "K-means++", "uses_n_clusters": True,
                  "description": "K-means の初期中心を k-means++ で選択します。", "scalability": "large"},
    "minibatch_kmeans": {"label": "MiniBatch K-means", "uses_n_clusters": True,
                         "description": "全論文を分割して3周学習し、全件の所属を計算します。", "scalability": "large"},
    "xmeans": {"label": "X-means（BIC分割）", "uses_n_clusters": True,
               "description": "指定した初期数から BIC で分割を選びます。原論文の木構造による高速化は使いません。", "scalability": "large_compute_intensive"},
    "knn_graph": {"label": "k-NNグラフ", "uses_n_clusters": False,
                  "description": "相互 k 近傍の連結成分で分類します。教師あり k-NN 分類器ではありません。", "scalability": "bounded_graph"},
    "dbscan": {"label": "DBSCAN", "uses_n_clusters": False,
               "description": "近傍の密度から分類し、ノイズを未分類として保持します。", "scalability": "bounded_neighborhoods"},
    "gmm": {"label": "GMM（対角共分散）", "uses_n_clusters": True,
            "description": "ガウス混合モデルで所属確率を計算します。対角共分散を使用します。", "scalability": "large_compute_intensive"},
    "birch": {"label": "BIRCH", "uses_n_clusters": True,
              "description": "全件を CF 木へ集約し、その部分クラスタを Ward 法で統合します。", "scalability": "bounded_cf_tree"},
    "agglomerative": {"label": "階層クラスタリング（Ward）", "uses_n_clusters": True,
                      "description": "全論文を Ward 法で順次統合します。二乗メモリを避けるため6,000件までです。", "scalability": "small"},
}


class ClusterModelError(ValueError):
    """Readable failure rather than an unrequested algorithm or sample change."""


def cluster_model_catalog():
    return [{"id": key, **value} for key, value in CLUSTER_MODELS.items()]


def _integer(value, name, minimum=1, maximum=100):
    try:
        parsed = int(value)
        if isinstance(value, bool) or float(value) != parsed or not minimum <= parsed <= maximum:
            raise ValueError
        return parsed
    except (ValueError, TypeError, OverflowError):
        raise ClusterModelError(f"{name}は {minimum}〜{maximum} の整数で指定してください。") from None


def _real(value, name, maximum=2.0):
    try:
        parsed = float(value)
        if isinstance(value, bool) or not np.isfinite(parsed) or not 0 < parsed <= maximum:
            raise ValueError
        return parsed
    except (ValueError, TypeError, OverflowError):
        raise ClusterModelError(f"{name}は 0 より大きく {maximum:g} 以下の数値で指定してください。") from None


def _progress(callback, message):
    if callback:
        callback(message)


def _prepare(matrix, progress_callback):
    if getattr(matrix, "ndim", None) != 2 or not matrix.shape[0] or not matrix.shape[1]:
        raise ClusterModelError("クラスタリングには1件以上・1次元以上の文書表現が必要です。")
    values = matrix.data if sparse.issparse(matrix) else np.asarray(matrix)
    try:
        if (not np.issubdtype(values.dtype, np.number) or np.issubdtype(values.dtype, np.complexfloating)
                or not np.isfinite(values).all()):
            raise ValueError
    except (ValueError, TypeError):
        raise ClusterModelError("文書表現には有限の数値を指定してください。") from None
    n, original_dimensions = matrix.shape
    dimensions = min(MAX_DIMENSIONS, original_dimensions)
    if n * dimensions * np.dtype(np.float32).itemsize > MAX_VECTOR_BYTES:
        raise ClusterModelError("文書表現の作業メモリ上限を超えます。論文数または特徴次元を減らして再分析してください。全件からの自動間引きは行いません。")
    _progress(progress_callback, f"全 {n:,} 件のクラスタリング表現を準備しています。")
    data = (sparse.csr_matrix(matrix, dtype=np.float32, copy=False) if sparse.issparse(matrix)
            else np.array(matrix, dtype=np.float32, order="C", copy=True))
    if not np.isfinite(data.data if sparse.issparse(data) else data).all():
        raise ClusterModelError("文書表現の値が大きすぎます。正規化した有限の数値を指定してください。")
    # SVD is fit on every row, not on the map's display sample.
    reduction = "none"
    if original_dimensions > MAX_DIMENSIONS and n > 1:
        dimensions = min(MAX_DIMENSIONS, original_dimensions - 1, n - 1)
        if (data.nnz if sparse.issparse(data) else np.count_nonzero(data)):
            reducer = TruncatedSVD(n_components=dimensions, random_state=RANDOM_STATE, n_iter=5)
            with python_warnings.catch_warnings():
                # Constant inputs may have undefined explained variance; it is not
                # used or persisted. The finite projected coordinates are checked.
                python_warnings.filterwarnings("ignore", category=RuntimeWarning,
                                               message=".*invalid value encountered in divide.*")
                vectors = reducer.fit_transform(data)
        else:
            vectors = np.zeros((n, dimensions), dtype=np.float32)
        reduction = "TruncatedSVD (fit on all papers)"
    elif original_dimensions > MAX_DIMENSIONS:
        # A lone document needs no feature reduction for fitting; a one-coordinate
        # norm carries its nonzero information without slicing arbitrary words.
        norm = np.sqrt(float(data.multiply(data).sum()) if sparse.issparse(data) else float(np.square(data).sum()))
        vectors = np.array([[norm]], dtype=np.float32)
        reduction = "single_document_norm"
    else:
        vectors = data.toarray() if sparse.issparse(data) else data
    if not np.isfinite(vectors).all():
        raise ClusterModelError("文書表現の次元削減で有限でない値が生じました。入力値を確認してください。")
    vectors = np.asarray(vectors, dtype=np.float32)
    # Float64 accumulation avoids overflow when finite float32 inputs have a
    # large norm. Never turn such a row into a zero-vector information group.
    norms = np.sqrt(np.einsum("ij,ij->i", vectors, vectors, dtype=np.float64))
    informative = norms > 0
    np.divide(vectors, norms[:, None], out=vectors, where=informative[:, None])
    return vectors, informative, {"original_dimensions": int(original_dimensions),
        "dimensions": int(vectors.shape[1]), "reduction": reduction, "normalization": "l2",
        "representation_fit_papers": int(n), "sampling": "none"}


def _distinct_count(values, limit):
    fingerprints = set()
    for row in values:
        fingerprints.add(np.round(row, 7).tobytes())
        if len(fingerprints) >= limit:
            break
    return len(fingerprints)


def _compact(raw):
    counts = Counter(int(label) for label in raw if label >= 0)
    first = {}
    for index, label in enumerate(raw):
        first.setdefault(int(label), index)
    ordered = sorted(counts, key=lambda label: (-counts[label], first[label]))
    mapping = {old: new for new, old in enumerate(ordered)}
    outlier = len(ordered) if np.any(raw < 0) else None
    labels = np.array([mapping[int(label)] if label >= 0 else outlier for label in raw], dtype=int)
    return labels, mapping, outlier


def _kmeans(values, effective, init="k-means++", n_init=10):
    return KMeans(n_clusters=effective, init=init, n_init=n_init, max_iter=200,
                  algorithm="lloyd", random_state=RANDOM_STATE).fit(values)


def _minibatch(values, effective, callback):
    estimator = MiniBatchKMeans(n_clusters=effective, init="k-means++", random_state=RANDOM_STATE,
                               batch_size=BATCH_SIZE, n_init=3, reassignment_ratio=0.01)
    rng = np.random.default_rng(RANDOM_STATE)
    batches = 0
    for epoch in range(3):
        order = rng.permutation(len(values))
        for start in range(0, len(values), BATCH_SIZE):
            estimator.partial_fit(values[order[start:start + BATCH_SIZE]])
            batches += 1
            _progress(callback, f"MiniBatch K-means 全件学習 {epoch + 1}/3 周・{min(start + BATCH_SIZE, len(values)):,}/{len(values):,} 件")
    labels = np.concatenate([estimator.predict(values[start:start + BATCH_SIZE])
                             for start in range(0, len(values), BATCH_SIZE)])
    return labels, {"estimator": "MiniBatchKMeans", "initialization": "k-means++",
                    "training_mode": "full_corpus_minibatch", "training_passes": 3,
                    "training_batches": batches, "batch_size": BATCH_SIZE}


def _bic(values, labels, centers):
    """Hard-assignment spherical-Gaussian BIC; higher is better.

    Variant uses the maximum-likelihood shared per-coordinate variance. It is
    intentionally documented separately from Pelleg/Moore's bias correction.
    """
    n, dimensions = values.shape
    k = len(centers)
    counts = np.bincount(labels, minlength=k)
    if np.any(counts == 0) or n <= k:
        return -np.inf
    residual = values - centers[labels]
    sse = float(np.einsum("ij,ij->", residual, residual, dtype=np.float64))
    variance = max(sse / (n * dimensions), 1e-10)
    log_likelihood = (float(np.dot(counts, np.log(counts / n)))
                      - 0.5 * n * dimensions * np.log(2 * np.pi * variance)
                      - 0.5 * sse / variance)
    parameter_count = k * dimensions + (k - 1) + 1
    return float(log_likelihood - 0.5 * parameter_count * np.log(n))


def _xmeans(values, initial, maximum, minimum_size, callback):
    fitted = _kmeans(values, initial)
    rounds = 0
    accepted = 0
    while len(fitted.cluster_centers_) < maximum:
        centers = []
        splits = 0
        current_count = len(fitted.cluster_centers_)
        for label, center in enumerate(fitted.cluster_centers_):
            part = values[fitted.labels_ == label]
            if (current_count + splits >= maximum or len(part) < max(4, 2 * minimum_size)
                    or _distinct_count(part, 2) < 2):
                centers.append(center)
                continue
            children = _kmeans(part, 2, n_init=3)
            sizes = np.bincount(children.labels_, minlength=2)
            parent_bic = _bic(part, np.zeros(len(part), dtype=int), np.asarray([part.mean(axis=0)]))
            child_bic = _bic(part, children.labels_, children.cluster_centers_)
            if sizes.min() >= minimum_size and child_bic > parent_bic:
                centers.extend(children.cluster_centers_)
                splits += 1
            else:
                centers.append(center)
        rounds += 1
        _progress(callback, f"X-means BIC 分割 {rounds} 周・{current_count + splits} クラスタ")
        if not splits:
            break
        accepted += splits
        # Whole-corpus optimization after the local split decisions.
        fitted = _kmeans(values, len(centers), init=np.asarray(centers), n_init=1)
    return fitted.labels_, {"estimator": "XMeansBICSplit", "initialization": "k-means++",
        "initial_clusters": initial, "max_clusters": maximum, "split_rounds": rounds,
        "accepted_splits": accepted, "selection_criterion": "hard-assignment spherical Gaussian BIC (shared ML variance)",
        "bic": _bic(values, fitted.labels_, fitted.cluster_centers_),
        "implementation": "local BIC splits and global KMeans; original kd-tree acceleration and bias-corrected variance are not implemented"}


def _graph_guard(pairs, method):
    if pairs > MAX_GRAPH_EDGES:
        hint = "eps を小さく" if method == "DBSCAN" else "近傍数 k を小さく"
        raise ClusterModelError(f"{method} の近傍関係が作業上限 {MAX_GRAPH_EDGES:,} 本を超えます。{hint}するか MiniBatch K-means / BIRCH を選択してください。論文の自動間引きは行いません。")


class _DisjointSet:
    def __init__(self, n):
        self.parent = np.arange(n)
        self.rank = np.zeros(n, dtype=np.int8)

    def find(self, index):
        while self.parent[index] != index:
            self.parent[index] = self.parent[self.parent[index]]
            index = self.parent[index]
        return int(index)

    def union(self, left, right):
        left, right = self.find(left), self.find(right)
        if left == right:
            return
        if self.rank[left] < self.rank[right]:
            left, right = right, left
        self.parent[right] = left
        if self.rank[left] == self.rank[right]:
            self.rank[left] += 1


def _dbscan(values, eps, min_samples, callback):
    n = len(values)
    tree = BallTree(values, leaf_size=40, metric="euclidean")
    counts = np.empty(n, dtype=np.int64)
    pairs = 0
    # Count-only queries check dense neighborhoods without constructing an N*N
    # distance array or retaining all neighbor lists in memory.
    for start in range(0, n, 512):
        counts[start:start + 512] = tree.query_radius(values[start:start + 512], r=eps, count_only=True)
        pairs += int(counts[start:start + 512].sum())
        _graph_guard(pairs, "DBSCAN")
        _progress(callback, f"DBSCAN 近傍数を確認 {min(start + 512, n):,}/{n:,} 件")
    core = counts >= min_samples
    sets = _DisjointSet(n)
    chunk_size = max(1, min(256, QUERY_PAIR_BUDGET // n))
    for start in range(0, n, chunk_size):
        indices = np.arange(start, min(start + chunk_size, n))
        indices = indices[core[indices]]
        if not len(indices):
            continue
        neighbors = tree.query_radius(values[indices], r=eps)
        for index, adjacent in zip(indices, neighbors):
            for target in adjacent[(adjacent > index) & core[adjacent]]:
                sets.union(int(index), int(target))
        _progress(callback, f"DBSCAN 密度クラスタを構成 {min(start + chunk_size, n):,}/{n:,} 件")
    labels = np.full(n, -1, dtype=int)
    roots = {}
    for index in np.flatnonzero(core):
        labels[index] = roots.setdefault(sets.find(int(index)), len(roots))
    # Border points adjacent to multiple components join the component whose
    # first core point occurs first, matching input-order DBSCAN expansion.
    for start in range(0, n, chunk_size):
        indices = np.arange(start, min(start + chunk_size, n))
        indices = indices[~core[indices]]
        if not len(indices):
            continue
        for index, adjacent in zip(indices, tree.query_radius(values[indices], r=eps)):
            connected = labels[adjacent[core[adjacent]]]
            if len(connected):
                labels[index] = int(connected.min())
    return labels, {"estimator": "DBSCAN (streamed exact BallTree neighborhoods)",
        "eps": eps, "min_samples": min_samples, "metric": "euclidean",
        "neighbor_pairs": pairs, "max_neighbor_pairs": MAX_GRAPH_EDGES,
        "query_batch_size": chunk_size, "core_papers": int(core.sum())}


def _knn_graph(values, neighbors, minimum_size, callback):
    n = len(values)
    effective = min(neighbors, max(0, n - 1))
    _graph_guard(n * effective, "k-NN")
    if effective == 0:
        return np.array([0 if minimum_size <= 1 else -1]), {"estimator": "MutualKNNConnectedComponents",
            "n_neighbors": 0, "requested_neighbors": neighbors, "graph_edges": 0,
            "graph_type": "mutual k-nearest-neighbor connected components",
            "min_cluster_size": minimum_size, "metric": "euclidean", "supervised": False}
    tree = BallTree(values, leaf_size=40, metric="euclidean")
    columns = np.empty((n, effective), dtype=np.int32)
    for start in range(0, n, 512):
        stop = min(start + 512, n)
        adjacent = tree.query(values[start:stop], k=effective + 1, return_distance=False)
        for offset, row in enumerate(adjacent):
            index = start + offset
            # Self may be absent among tied duplicate points; always take k
            # others after filtering, instead of blindly deleting column zero.
            columns[index] = row[row != index][:effective]
        _progress(callback, f"相互 k-NN グラフを計算 {stop:,}/{n:,} 件")
    graph = sparse.csr_matrix((np.ones(n * effective, dtype=np.uint8), columns.ravel(),
                               np.arange(0, (n + 1) * effective, effective)), shape=(n, n))
    mutual = graph.minimum(graph.T)
    count, labels = connected_components(mutual, directed=False)
    sizes = np.bincount(labels, minlength=count)
    labels[sizes[labels] < minimum_size] = -1
    return labels, {"estimator": "MutualKNNConnectedComponents", "n_neighbors": effective,
        "requested_neighbors": neighbors, "graph_type": "mutual k-nearest-neighbor connected components",
        "graph_edges": int(mutual.nnz // 2), "min_cluster_size": minimum_size,
        "metric": "euclidean", "supervised": False}


def _birch(values, effective, threshold, callback):
    estimator = Birch(threshold=threshold, branching_factor=50, n_clusters=None, compute_labels=False)
    for start in range(0, len(values), 512):
        estimator.partial_fit(values[start:start + 512])
        if len(estimator.subcluster_centers_) > MAX_BIRCH_SUBCLUSTERS:
            raise ClusterModelError(f"BIRCH の部分クラスタが {MAX_BIRCH_SUBCLUSTERS:,} 個を超えました。しきい値を大きくするか MiniBatch K-means を選択してください。全件からの自動間引きは行いません。")
        _progress(callback, f"BIRCH 全件の CF 木を構成 {min(start + 512, len(values)):,}/{len(values):,} 件")
    subclusters = len(estimator.subcluster_centers_)
    estimator.set_params(n_clusters=min(effective, subclusters) if subclusters > 1 else None)
    estimator.partial_fit(None)
    labels = np.concatenate([estimator.predict(values[start:start + 256])
                             for start in range(0, len(values), 256)])
    return labels, {"estimator": "Birch", "threshold": threshold, "branching_factor": 50,
        "subclusters": subclusters, "global_clustering": ("Ward agglomerative on CF subcluster centers"
                                                           if subclusters > 1 else "single CF subcluster"),
        "training_mode": "full_corpus_incremental", "training_passes": 1}


def fit_cluster_model(matrix, method, n_clusters, min_cluster_size=5, options=None, progress_callback=None):
    """Return the analytics-compatible model result without sampling any rows."""
    method = {"kmeans_plus_plus": "kmeans_pp", "kmeans++": "kmeans_pp"}.get(method, method)
    if method not in CLUSTER_MODELS:
        raise ClusterModelError("未対応のクラスタリング手法です。分析設定から選択してください。")
    requested = _integer(n_clusters, "クラスタ数")
    minimum_size = _integer(min_cluster_size, "最小クラスタサイズ", maximum=10000)
    if options is not None and not isinstance(options, dict):
        raise ClusterModelError("クラスタリング設定はオブジェクトで指定してください。")
    options = options or {}
    allowed = {"eps", "min_samples", "n_neighbors", "max_clusters", "min_clusters", "birch_threshold"}
    if set(options) - allowed:
        raise ClusterModelError("未対応のクラスタリング設定が含まれています。")
    # Validate before even an all-zero/singleton input can take a shortcut.
    eps = _real(options.get("eps", .35), "DBSCAN eps")
    min_samples = _integer(options.get("min_samples", minimum_size), "DBSCAN min_samples", maximum=10000)
    neighbors = _integer(options.get("n_neighbors", 15), "k-NN の近傍数")
    threshold = _real(options.get("birch_threshold", .35), "BIRCH のしきい値")
    maximum = _integer(options.get("max_clusters", max(20, requested)), "X-means 最大クラスタ数")
    initial = _integer(options.get("min_clusters", requested), "X-means 初期クラスタ数")
    if method == "xmeans" and maximum < initial:
        raise ClusterModelError("X-means の最大クラスタ数は初期クラスタ数以上にしてください。")
    vectors, informative, representation = _prepare(matrix, progress_callback)
    n = len(vectors)
    usable = vectors if informative.all() else vectors[informative]
    notices = []
    raw = np.full(n, -1, dtype=int)
    membership = None
    details = {"method": method, "random_state": RANDOM_STATE, "requested_topics": requested,
        "requested_clusters": requested, "training_papers": int(len(usable)), "transform_papers": n,
        "training_mode": "full_corpus", "representation": representation, "sampling": "none",
        "input": "whole_corpus_l2_document_vectors", "membership_description": "所属は単一クラスタです。所属確率は推定しません。"}
    fitted_components = 0
    if len(usable):
        effective = min(initial if method == "xmeans" else requested, len(usable),
                        _distinct_count(usable, initial if method == "xmeans" else requested))
        if CLUSTER_MODELS[method]["uses_n_clusters"] and effective < (initial if method == "xmeans" else requested):
            notices.append(f"異なる有効な文書表現数に合わせ、初期クラスタ数を {effective} に調整しました。")
        _progress(progress_callback, f"{CLUSTER_MODELS[method]['label']} で全 {len(usable):,} 件を分類しています。")
        with python_warnings.catch_warnings(record=True) as caught:
            python_warnings.simplefilter("always", ConvergenceWarning)
            if method in {"kmeans", "kmeans_pp"}:
                initialization = "random" if method == "kmeans" else "k-means++"
                estimator = _kmeans(usable, effective, initialization)
                model_labels = estimator.labels_
                algorithm_details = {"estimator": "KMeans", "initialization": initialization,
                    "n_init": 10, "iterations": int(estimator.n_iter_), "inertia": float(estimator.inertia_)}
            elif method == "minibatch_kmeans":
                model_labels, algorithm_details = _minibatch(usable, effective, progress_callback)
            elif method == "xmeans":
                maximum = min(maximum, len(usable), _distinct_count(usable, maximum))
                model_labels, algorithm_details = _xmeans(usable, effective, maximum, minimum_size, progress_callback)
                # BIC is undefined for a singleton; persist JSON null instead.
                if not np.isfinite(algorithm_details["bic"]):
                    algorithm_details["bic"] = None
                notices.append("X-means は球状ガウス分布の BIC による分割版です。共有分散は最尤推定を使い、原論文の木構造による高速化・分散補正は実装していません。")
            elif method == "dbscan":
                model_labels, algorithm_details = _dbscan(usable, eps, min_samples, progress_callback)
            elif method == "knn_graph":
                model_labels, algorithm_details = _knn_graph(usable, neighbors, minimum_size, progress_callback)
                notices.append("k-NN は相互近傍グラフの連結成分を使います。教師あり分類器ではなく、近傍数により1つの大きな集団になる場合があります。")
            elif method == "gmm":
                if len(usable) * effective * 8 > MAX_GMM_RESPONSIBILITY_BYTES:
                    raise ClusterModelError("GMM の所属確率行列が作業メモリ上限を超えます。クラスタ数を減らすか MiniBatch K-means を選択してください。自動間引きは行いません。")
                if len(usable) == 1:
                    model_labels = np.zeros(1, dtype=int)
                    probabilities = np.ones((1, 1), dtype=np.float32)
                    algorithm_details = {"estimator": "GaussianMixture", "covariance_type": "diag", "status": "single_document", "converged": None}
                    notices.append("有効な論文が1件のため、GMM は分布を推定せず1群として保持しました。")
                else:
                    estimator = GaussianMixture(n_components=effective, covariance_type="diag", reg_covar=1e-5,
                        random_state=RANDOM_STATE, n_init=2, max_iter=100, init_params="kmeans")
                    estimator.fit(usable)
                    probabilities = np.empty((len(usable), effective), dtype=np.float32)
                    for start in range(0, len(usable), BATCH_SIZE):
                        probabilities[start:start + BATCH_SIZE] = estimator.predict_proba(usable[start:start + BATCH_SIZE])
                    model_labels = probabilities.argmax(axis=1)
                    algorithm_details = {"estimator": "GaussianMixture", "covariance_type": "diag", "reg_covar": 1e-5,
                        "iterations": int(estimator.n_iter_), "converged": bool(estimator.converged_), "n_init": 2}
                membership = np.zeros((n, probabilities.shape[1]), dtype=np.float32)
                membership[informative] = probabilities
                fitted_components = membership.shape[1]
                details["membership_description"] = "対角共分散 GMM の所属確率。列は推定成分、label_to_component が表示クラスタとの対応です。情報不足の行は0です。"
            elif method == "birch":
                model_labels, algorithm_details = _birch(usable, effective, threshold, progress_callback)
            else:
                if len(usable) > MAX_AGGLOMERATIVE_PAPERS:
                    raise ClusterModelError(f"Ward 階層クラスタリングは二乗メモリを使うため、有効な論文 {MAX_AGGLOMERATIVE_PAPERS:,} 件までです。MiniBatch K-means / BIRCH を選択してください。全件を自動で間引くことはありません。")
                model_labels = (AgglomerativeClustering(n_clusters=effective, linkage="ward").fit_predict(usable)
                                if len(usable) > 1 else np.zeros(1, dtype=int))
                algorithm_details = {"estimator": "AgglomerativeClustering", "linkage": "ward", "metric": "euclidean"}
            if any(issubclass(w.category, ConvergenceWarning) for w in caught):
                notices.append("クラスタリングが収束条件を満たさない、または指定数のクラスタを作れない可能性があります。結果を確認し設定を調整してください。")
        if len(model_labels) != len(usable):
            raise ClusterModelError("クラスタリング結果と論文数が一致しません。")
        raw[informative] = model_labels
        details.update(algorithm_details)
    labels, mapping, outlier = _compact(raw)
    if len(mapping) > 100:
        raise ClusterModelError("クラスタ数がレポートの作業上限 100 個を超えます。DBSCAN の eps、k-NN の近傍数・最小クラスタサイズを大きくするか、クラスタ数を指定する手法を選択してください。小集団を自動で削除・統合することはありません。")
    missing = int((~informative).sum())
    noise = int(np.count_nonzero(raw[informative] < 0))
    if missing:
        notices.append(f"有効な文書表現がない {missing:,} 件は情報不足として保持しました。")
    if noise:
        notices.append(f"密度・近傍条件でクラスタに属さない {noise:,} 件を未分類として保持しました。")
    if method == "birch" and len(mapping) < requested and len(usable):
        notices.append(f"BIRCH の部分クラスタ数により、実際のクラスタ数は {len(mapping)} です。")
    details.update({"fitted_components": int(fitted_components or len(mapping)), "effective_topics": len(mapping),
        "effective_clusters": len(mapping), "outlier_count": missing + noise, "noise_count": noise,
        "information_insufficient_count": missing, "information_insufficient_label": outlier if missing else None,
        "label_to_component": {str(new): old for old, new in mapping.items()},
        "status": details.get("status", "fitted" if len(usable) else "information_insufficient")})
    return {"labels": labels, "map_matrix": vectors, "topic_keywords": None,
            "outlier_label": outlier, "membership": membership, "details": details, "warnings": notices}
