"""固有表現抽出（パターンマッチベース / 生成AI不要）

GiNZA がインストールされている場合はそちらを優先利用。
ない場合は正規表現ベースのフォールバック。
"""

from __future__ import annotations

import re
from typing import Any


def _try_ginza() -> bool:
    """GiNZA が利用可能かチェック"""
    try:
        import spacy  # noqa: F401
        spacy.load("ja_ginza")
        return True
    except Exception:
        return False


_GINZA_AVAILABLE = None  # lazy check


class NamedEntityRecognizer:
    """固有表現抽出"""

    # 正規表現パターン（フォールバック用）
    _PATTERNS: dict[str, re.Pattern[str]] = {
        "company": re.compile(
            r"(?:株式会社|有限会社|合同会社|合資会社|合名会社)[\w\u3040-\u309F\u30A0-\u30FF\u4E00-\u9FFF]+"
            r"|[\w\u3040-\u309F\u30A0-\u30FF\u4E00-\u9FFF]+(?:株式会社|有限会社|合同会社|Co\.,?\s*Ltd\.?|Inc\.?|Corp\.?|LLC)"
        ),
        "money": re.compile(
            r"[0-9０-９,，]+(?:円|万円|百万円|億円|兆円|千万円)"
            r"|¥[0-9,]+|￥[0-9,]+"
            r"|\$[0-9,]+(?:\.[0-9]+)?"
        ),
        "date": re.compile(
            r"(?:令和|平成|昭和)?[0-9０-９]{1,4}年(?:[0-9０-９]{1,2}月)?(?:[0-9０-９]{1,2}日)?"
            r"|20[0-9]{2}[-/][0-9]{1,2}[-/][0-9]{1,2}"
            r"|20[0-9]{2}年[0-9]{1,2}月[0-9]{1,2}日"
        ),
        "phone": re.compile(
            r"0\d{1,4}-\d{1,4}-\d{3,4}"
            r"|0\d{9,10}"
        ),
        "email": re.compile(
            r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}"
        ),
        "url": re.compile(
            r"https?://[^\s<>\"'）】」）\]]*"
        ),
        "percentage": re.compile(
            r"[0-9０-９]+(?:\.[0-9０-９]+)?[%％]"
        ),
    }

    def __init__(self, use_ginza: bool = True):
        global _GINZA_AVAILABLE
        if _GINZA_AVAILABLE is None:
            _GINZA_AVAILABLE = _try_ginza()
        self.use_ginza = use_ginza and _GINZA_AVAILABLE

    def extract(self, text: str) -> dict[str, list[str]]:
        """
        テキストから固有表現を抽出

        Returns
        -------
        {"company": [...], "money": [...], "date": [...], ...}
        """
        if self.use_ginza:
            return self._extract_ginza(text)
        return self._extract_regex(text)

    def _extract_regex(self, text: str) -> dict[str, list[str]]:
        """正規表現ベースの抽出"""
        results: dict[str, list[str]] = {}
        for entity_type, pattern in self._PATTERNS.items():
            matches = pattern.findall(text)
            if matches:
                # 重複除去しつつ順序維持
                seen: set[str] = set()
                unique: list[str] = []
                for m in matches:
                    m = m.strip()
                    if m and m not in seen:
                        seen.add(m)
                        unique.append(m)
                results[entity_type] = unique
        return results

    def _extract_ginza(self, text: str) -> dict[str, list[str]]:
        """GiNZA ベースの抽出"""
        import spacy
        nlp = spacy.load("ja_ginza")
        doc = nlp(text)

        results: dict[str, list[str]] = {}
        label_map = {
            "ORG": "company",
            "PERSON": "person",
            "GPE": "location",
            "LOC": "location",
            "DATE": "date",
            "MONEY": "money",
            "PERCENT": "percentage",
            "PRODUCT": "product",
        }

        for ent in doc.ents:
            entity_type = label_map.get(ent.label_, ent.label_.lower())
            if entity_type not in results:
                results[entity_type] = []
            if ent.text not in results[entity_type]:
                results[entity_type].append(ent.text)

        # 正規表現でメール・電話・URLも補完
        for key in ("email", "phone", "url"):
            regex_results = self._PATTERNS[key].findall(text)
            if regex_results:
                results[key] = list(dict.fromkeys(r.strip() for r in regex_results))

        return results

    def extract_batch(self, texts: list[str]) -> list[dict[str, list[str]]]:
        """複数テキストのバッチ抽出"""
        return [self.extract(t) for t in texts]

    def flatten(self, entities: dict[str, list[str]]) -> list[dict[str, str]]:
        """フラットなリストに変換"""
        flat: list[dict[str, str]] = []
        for entity_type, values in entities.items():
            for value in values:
                flat.append({"type": entity_type, "value": value})
        return flat
