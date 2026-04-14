"""類似度分析（TF-IDF コサイン類似度 / 生成AI不要）"""

from __future__ import annotations

from typing import Any

import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity


class SimilarityAnalyzer:
    """テキスト間の類似度計算"""

    def __init__(self, max_features: int = 1000):
        self.max_features = max_features
        self.vectorizer = TfidfVectorizer(max_features=max_features)

    def compute_similarity_matrix(
        self,
        texts: list[str],
        labels: list[str] | None = None,
    ) -> dict[str, Any]:
        """
        全テキスト間のコサイン類似度行列を計算

        Returns
        -------
        {
            "matrix": [[float, ...], ...],
            "labels": [str, ...],
        }
        """
        if len(texts) < 2:
            return {"matrix": [], "labels": labels or []}

        tfidf_matrix = self.vectorizer.fit_transform(texts)
        sim_matrix = cosine_similarity(tfidf_matrix)

        return {
            "matrix": [[round(float(v), 4) for v in row] for row in sim_matrix],
            "labels": labels or [f"doc_{i}" for i in range(len(texts))],
        }

    def find_similar(
        self,
        query_text: str,
        corpus: list[str],
        labels: list[str] | None = None,
        top_n: int = 10,
    ) -> list[dict[str, Any]]:
        """
        クエリテキストに類似するテキストを検索

        Returns
        -------
        [{"label": str, "score": float, "index": int}, ...]
        """
        if not corpus:
            return []

        all_texts = [query_text] + corpus
        tfidf_matrix = self.vectorizer.fit_transform(all_texts)

        query_vec = tfidf_matrix[0:1]
        corpus_vecs = tfidf_matrix[1:]
        scores = cosine_similarity(query_vec, corpus_vecs).flatten()

        top_indices = scores.argsort()[::-1][:top_n]
        results: list[dict[str, Any]] = []
        for idx in top_indices:
            if scores[idx] > 0:
                results.append({
                    "label": labels[idx] if labels else f"doc_{idx}",
                    "score": round(float(scores[idx]), 4),
                    "index": int(idx),
                })
        return results

    def find_similar_companies(
        self,
        target_company_text: str,
        company_texts: list[str],
        company_names: list[str],
        top_n: int = 10,
    ) -> list[dict[str, Any]]:
        """企業テキスト（説明文等）から類似企業を検索"""
        return self.find_similar(
            query_text=target_company_text,
            corpus=company_texts,
            labels=company_names,
            top_n=top_n,
        )
