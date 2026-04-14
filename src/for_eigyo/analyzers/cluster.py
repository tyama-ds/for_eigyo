"""クラスタリング分析（scikit-learn / 生成AI不要）"""

from __future__ import annotations

from typing import Any

import numpy as np
from sklearn.cluster import KMeans, DBSCAN
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.decomposition import PCA
from sklearn.preprocessing import StandardScaler


class ClusterAnalyzer:
    """テキストクラスタリング"""

    def __init__(
        self,
        max_features: int = 500,
        ngram_range: tuple[int, int] = (1, 2),
    ):
        self.max_features = max_features
        self.ngram_range = ngram_range
        self.vectorizer = TfidfVectorizer(
            max_features=max_features,
            ngram_range=ngram_range,
        )

    def cluster_texts(
        self,
        texts: list[str],
        n_clusters: int = 5,
        method: str = "kmeans",
        labels: list[str] | None = None,
    ) -> dict[str, Any]:
        """
        テキスト群をクラスタリング

        Parameters
        ----------
        texts : クラスタリング対象テキスト
        n_clusters : クラスタ数 (kmeans)
        method : "kmeans" | "dbscan"
        labels : 各テキストのラベル（企業名等）

        Returns
        -------
        {
            "clusters": {cluster_id: [index, ...]},
            "cluster_labels": {cluster_id: [label, ...]},
            "cluster_keywords": {cluster_id: [keyword, ...]},
            "n_clusters": int,
            "coordinates_2d": [[x, y], ...],
        }
        """
        if len(texts) < 2:
            return {"clusters": {}, "n_clusters": 0, "coordinates_2d": []}

        n_clusters = min(n_clusters, len(texts))

        tfidf_matrix = self.vectorizer.fit_transform(texts)

        if method == "dbscan":
            model = DBSCAN(eps=0.5, min_samples=2, metric="cosine")
            cluster_ids = model.fit_predict(tfidf_matrix)
        else:
            model = KMeans(n_clusters=n_clusters, random_state=42, n_init=10)
            cluster_ids = model.fit_predict(tfidf_matrix)

        # クラスタごとのインデックスとラベルをまとめる
        clusters: dict[int, list[int]] = {}
        cluster_labels_map: dict[int, list[str]] = {}
        for idx, cid in enumerate(cluster_ids):
            cid = int(cid)
            if cid not in clusters:
                clusters[cid] = []
                cluster_labels_map[cid] = []
            clusters[cid].append(idx)
            if labels:
                cluster_labels_map[cid].append(labels[idx])

        # クラスタごとのキーワード
        feature_names = self.vectorizer.get_feature_names_out()
        cluster_keywords: dict[int, list[str]] = {}
        for cid, indices in clusters.items():
            if cid == -1:
                continue
            sub_matrix = tfidf_matrix[indices]
            mean_tfidf = np.asarray(sub_matrix.mean(axis=0)).flatten()
            top_indices = mean_tfidf.argsort()[::-1][:10]
            cluster_keywords[cid] = [feature_names[i] for i in top_indices if mean_tfidf[i] > 0]

        # 2D 座標 (PCA)
        coords_2d: list[list[float]] = []
        if tfidf_matrix.shape[0] >= 2:
            n_components = min(2, tfidf_matrix.shape[1])
            pca = PCA(n_components=n_components)
            coords = pca.fit_transform(tfidf_matrix.toarray())
            coords_2d = coords.tolist()

        return {
            "clusters": {str(k): v for k, v in clusters.items()},
            "cluster_labels": {str(k): v for k, v in cluster_labels_map.items()},
            "cluster_keywords": {str(k): v for k, v in cluster_keywords.items()},
            "n_clusters": len(set(cluster_ids) - {-1}),
            "coordinates_2d": coords_2d,
            "cluster_ids": [int(c) for c in cluster_ids],
        }
