"""感情分析（辞書ベース / 生成AI不要）"""

from __future__ import annotations

import re
from typing import Any


# 簡易的な日本語極性辞書（コアワード）
# 実運用では oseti や より大きな辞書を使う
_POSITIVE_WORDS = {
    "成長", "拡大", "増収", "増益", "好調", "改善", "向上", "強化", "推進",
    "革新", "先進", "優れ", "優秀", "達成", "突破", "加速", "貢献", "期待",
    "新規", "開拓", "提携", "協業", "連携", "飛躍", "躍進", "活性", "繁栄",
    "回復", "安定", "信頼", "実績", "受注", "黒字", "上昇", "高評価",
    "growth", "increase", "improve", "strong", "success", "innovative",
    "leading", "excellent", "expand", "partnership", "profitable",
}

_NEGATIVE_WORDS = {
    "減収", "減益", "低迷", "悪化", "縮小", "撤退", "閉鎖", "倒産", "破産",
    "赤字", "損失", "不振", "停滞", "後退", "下落", "低下", "困難", "問題",
    "リスク", "懸念", "不安", "遅延", "中止", "失敗", "違反", "訴訟",
    "不正", "流出", "漏洩", "削減", "リストラ", "解雇", "債務",
    "decline", "decrease", "loss", "risk", "failure", "concern",
    "bankruptcy", "lawsuit", "layoff", "deficit", "shutdown",
}


class SentimentAnalyzer:
    """辞書ベース感情分析（生成AI不要）"""

    def __init__(
        self,
        positive_words: set[str] | None = None,
        negative_words: set[str] | None = None,
    ):
        self.positive_words = positive_words or _POSITIVE_WORDS
        self.negative_words = negative_words or _NEGATIVE_WORDS

    def analyze(self, text: str) -> dict[str, Any]:
        """
        テキストの感情分析

        Returns
        -------
        {
            "polarity": float,        # -1.0 ~ 1.0
            "label": str,             # "positive" | "negative" | "neutral"
            "positive_count": int,
            "negative_count": int,
            "positive_words_found": list[str],
            "negative_words_found": list[str],
        }
        """
        text_lower = text.lower()

        pos_found = [w for w in self.positive_words if w.lower() in text_lower]
        neg_found = [w for w in self.negative_words if w.lower() in text_lower]

        pos_count = len(pos_found)
        neg_count = len(neg_found)
        total = pos_count + neg_count

        if total == 0:
            polarity = 0.0
        else:
            polarity = (pos_count - neg_count) / total

        if polarity > 0.1:
            label = "positive"
        elif polarity < -0.1:
            label = "negative"
        else:
            label = "neutral"

        return {
            "polarity": round(polarity, 3),
            "label": label,
            "positive_count": pos_count,
            "negative_count": neg_count,
            "positive_words_found": sorted(pos_found),
            "negative_words_found": sorted(neg_found),
        }

    def analyze_batch(self, texts: list[str]) -> list[dict[str, Any]]:
        """複数テキストのバッチ分析"""
        return [self.analyze(t) for t in texts]

    def aggregate(self, results: list[dict[str, Any]]) -> dict[str, Any]:
        """複数分析結果の集約"""
        if not results:
            return {"avg_polarity": 0.0, "label": "neutral", "count": 0}

        polarities = [r["polarity"] for r in results]
        avg = sum(polarities) / len(polarities)
        pos = sum(1 for r in results if r["label"] == "positive")
        neg = sum(1 for r in results if r["label"] == "negative")
        neu = sum(1 for r in results if r["label"] == "neutral")

        if avg > 0.1:
            label = "positive"
        elif avg < -0.1:
            label = "negative"
        else:
            label = "neutral"

        return {
            "avg_polarity": round(avg, 3),
            "label": label,
            "count": len(results),
            "positive_ratio": round(pos / len(results), 3),
            "negative_ratio": round(neg / len(results), 3),
            "neutral_ratio": round(neu / len(results), 3),
        }
