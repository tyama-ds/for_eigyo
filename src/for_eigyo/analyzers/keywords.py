"""キーワード抽出（TF-IDF ベース / 生成AI不要）"""

from __future__ import annotations

import re
from collections import Counter
from typing import Any

import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer


class KeywordExtractor:
    """TF-IDF ベースのキーワード抽出"""

    def __init__(
        self,
        max_features: int = 1000,
        ngram_range: tuple[int, int] = (1, 2),
        stop_words: list[str] | None = None,
    ):
        self.max_features = max_features
        self.ngram_range = ngram_range
        self._default_stop_words = [
            "の", "に", "は", "を", "た", "が", "で", "て", "と", "し", "れ",
            "さ", "ある", "いる", "も", "する", "から", "な", "こと", "として",
            "い", "や", "れる", "など", "なっ", "ない", "この", "ため", "その",
            "あっ", "よう", "また", "もの", "という", "あり", "まで", "られ",
            "なる", "へ", "か", "だ", "これ", "によって", "により", "おり",
            "より", "による", "ます", "です", "それ", "the", "a", "an", "is",
            "are", "was", "were", "be", "been", "being", "have", "has", "had",
            "do", "does", "did", "will", "would", "could", "should", "may",
            "might", "shall", "can", "need", "dare", "ought", "used", "to",
            "of", "in", "for", "on", "with", "at", "by", "from", "as", "into",
            "about", "between", "through", "during", "before", "after", "and",
            "but", "or", "nor", "not", "so", "yet", "both", "either", "neither",
            "each", "every", "all", "any", "few", "more", "most", "other",
            "some", "such", "no", "only", "own", "same", "than", "too", "very",
            "just", "because", "if", "when", "while", "how", "what", "which",
            "who", "whom", "this", "that", "these", "those", "it", "its",
        ]
        self.stop_words = stop_words or self._default_stop_words

    @staticmethod
    def _tokenize_simple(text: str) -> list[str]:
        """簡易トークナイザ（形態素解析なし）"""
        # 日本語: 文字ベースN-gram用にそのまま返す
        # 英語: スペース区切り
        # 記号・数字を除去して小文字化
        text = re.sub(r"[0-9０-９]+", " ", text)
        text = re.sub(r"[^\w\sぁ-んァ-ヶ亜-熙a-zA-Z]", " ", text)
        tokens = text.lower().split()
        return [t for t in tokens if len(t) > 1]

    def extract_tfidf(
        self,
        documents: list[str],
        top_n: int = 20,
    ) -> list[dict[str, Any]]:
        """
        複数文書から TF-IDF 上位キーワードを抽出

        Returns
        -------
        list of {"keyword": str, "score": float, "doc_freq": int}
        """
        if not documents:
            return []

        vectorizer = TfidfVectorizer(
            max_features=self.max_features,
            ngram_range=self.ngram_range,
            stop_words=self.stop_words,
            token_pattern=r"(?u)\b\w[\w\u3040-\u309F\u30A0-\u30FF\u4E00-\u9FFF]+\b",
        )

        try:
            tfidf_matrix = vectorizer.fit_transform(documents)
        except ValueError:
            return []

        feature_names = vectorizer.get_feature_names_out()
        # 文書全体での平均 TF-IDF スコア
        mean_scores = np.asarray(tfidf_matrix.mean(axis=0)).flatten()
        # ドキュメント頻度
        doc_freq = np.asarray((tfidf_matrix > 0).sum(axis=0)).flatten()

        top_indices = mean_scores.argsort()[::-1][:top_n]
        return [
            {
                "keyword": feature_names[i],
                "score": round(float(mean_scores[i]), 4),
                "doc_freq": int(doc_freq[i]),
            }
            for i in top_indices
            if mean_scores[i] > 0
        ]

    def extract_frequency(
        self,
        text: str,
        top_n: int = 20,
    ) -> list[dict[str, Any]]:
        """単純頻度ベースのキーワード抽出"""
        tokens = self._tokenize_simple(text)
        tokens = [t for t in tokens if t not in self.stop_words and len(t) > 1]
        counter = Counter(tokens)
        return [
            {"keyword": word, "count": count}
            for word, count in counter.most_common(top_n)
        ]

    def extract(
        self,
        texts: list[str] | str,
        top_n: int = 20,
        method: str = "tfidf",
    ) -> list[dict[str, Any]]:
        """統一インターフェース"""
        if isinstance(texts, str):
            texts = [texts]

        if method == "tfidf":
            return self.extract_tfidf(texts, top_n=top_n)
        elif method == "frequency":
            combined = " ".join(texts)
            return self.extract_frequency(combined, top_n=top_n)
        else:
            raise ValueError(f"Unknown method: {method}")
