"""リードスコアリング（ルールベース + ロジスティック回帰 / 生成AI不要）"""

from __future__ import annotations

from typing import Any

import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import StandardScaler

from for_eigyo.storage.models import LeadScore


# デフォルトのスコアリングルール
DEFAULT_RULES: dict[str, dict[str, Any]] = {
    "has_website": {"weight": 0.10, "description": "Webサイトあり"},
    "has_email": {"weight": 0.10, "description": "メールアドレスあり"},
    "has_phone": {"weight": 0.05, "description": "電話番号あり"},
    "employee_range": {
        "weight": 0.15,
        "description": "従業員数",
        "ranges": {
            (0, 10): 0.3,
            (10, 50): 0.5,
            (50, 300): 0.8,
            (300, 1000): 1.0,
            (1000, float("inf")): 0.7,
        },
    },
    "capital_range": {
        "weight": 0.10,
        "description": "資本金",
        "ranges": {
            (0, 1_000_000): 0.2,
            (1_000_000, 10_000_000): 0.4,
            (10_000_000, 100_000_000): 0.7,
            (100_000_000, 1_000_000_000): 1.0,
            (1_000_000_000, float("inf")): 0.8,
        },
    },
    "news_sentiment": {"weight": 0.15, "description": "ニュース感情"},
    "keyword_match": {"weight": 0.20, "description": "キーワードマッチ"},
    "recent_activity": {"weight": 0.15, "description": "最近の活動"},
}


class LeadScorer:
    """ルールベース + 学習ベースのリードスコアリング"""

    def __init__(self, rules: dict[str, dict[str, Any]] | None = None):
        self.rules = rules or DEFAULT_RULES
        self._model: LogisticRegression | None = None
        self._scaler: StandardScaler | None = None

    def score_rule_based(self, features: dict[str, Any]) -> LeadScore:
        """
        ルールベースのスコアリング

        Parameters
        ----------
        features : {
            "company_name": str,
            "has_website": bool,
            "has_email": bool,
            "has_phone": bool,
            "employee_count": int | None,
            "capital": int | None,
            "news_sentiment": float,  # -1.0 ~ 1.0
            "keyword_match_ratio": float,  # 0.0 ~ 1.0
            "days_since_activity": int | None,
        }
        """
        company_name = features.get("company_name", "Unknown")
        factors: dict[str, float] = {}
        total_score = 0.0
        total_weight = 0.0

        # Bool features
        for key in ("has_website", "has_email", "has_phone"):
            if key in self.rules:
                w = self.rules[key]["weight"]
                val = 1.0 if features.get(key, False) else 0.0
                factors[key] = val
                total_score += w * val
                total_weight += w

        # Range features
        for key in ("employee_range", "capital_range"):
            if key in self.rules:
                w = self.rules[key]["weight"]
                raw_key = "employee_count" if "employee" in key else "capital"
                raw_val = features.get(raw_key)
                val = 0.0
                if raw_val is not None:
                    for (lo, hi), score in self.rules[key]["ranges"].items():
                        if lo <= raw_val < hi:
                            val = score
                            break
                factors[key] = val
                total_score += w * val
                total_weight += w

        # Sentiment
        if "news_sentiment" in self.rules:
            w = self.rules["news_sentiment"]["weight"]
            sentiment = features.get("news_sentiment", 0.0)
            val = (sentiment + 1.0) / 2.0  # -1~1 → 0~1
            factors["news_sentiment"] = round(val, 3)
            total_score += w * val
            total_weight += w

        # Keyword match
        if "keyword_match" in self.rules:
            w = self.rules["keyword_match"]["weight"]
            val = features.get("keyword_match_ratio", 0.0)
            factors["keyword_match"] = round(val, 3)
            total_score += w * val
            total_weight += w

        # Recent activity
        if "recent_activity" in self.rules:
            w = self.rules["recent_activity"]["weight"]
            days = features.get("days_since_activity")
            if days is not None:
                val = max(0, 1.0 - days / 365.0)  # 1年以上前は0
            else:
                val = 0.3  # 不明は中間値寄り
            factors["recent_activity"] = round(val, 3)
            total_score += w * val
            total_weight += w

        final_score = total_score / total_weight if total_weight > 0 else 0.0
        return LeadScore(
            company_name=company_name,
            score=round(final_score, 3),
            factors=factors,
        )

    def score_batch(self, features_list: list[dict[str, Any]]) -> list[LeadScore]:
        """バッチスコアリング"""
        return [self.score_rule_based(f) for f in features_list]

    def train_model(
        self,
        feature_matrix: np.ndarray,
        labels: np.ndarray,
    ) -> dict[str, float]:
        """
        学習データからロジスティック回帰モデルを訓練

        Parameters
        ----------
        feature_matrix : (n_samples, n_features) の特徴量行列
        labels : (n_samples,) の二値ラベル (0/1 = 非見込み/見込み)

        Returns
        -------
        {"accuracy": float}
        """
        self._scaler = StandardScaler()
        X_scaled = self._scaler.fit_transform(feature_matrix)

        self._model = LogisticRegression(random_state=42, max_iter=1000)
        self._model.fit(X_scaled, labels)

        accuracy = float(self._model.score(X_scaled, labels))
        return {"accuracy": round(accuracy, 4)}

    def predict(self, feature_matrix: np.ndarray) -> np.ndarray:
        """学習済みモデルで予測"""
        if self._model is None or self._scaler is None:
            raise RuntimeError("Model not trained. Call train_model first.")
        X_scaled = self._scaler.transform(feature_matrix)
        return self._model.predict_proba(X_scaled)[:, 1]
