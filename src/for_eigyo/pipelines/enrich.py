"""営業情報エンリッチメントパイプライン"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

import pandas as pd

from for_eigyo.analyzers.keywords import KeywordExtractor
from for_eigyo.analyzers.sentiment import SentimentAnalyzer
from for_eigyo.analyzers.ner import NamedEntityRecognizer
from for_eigyo.analyzers.cluster import ClusterAnalyzer
from for_eigyo.analyzers.similarity import SimilarityAnalyzer
from for_eigyo.analyzers.scoring import LeadScorer
from for_eigyo.collectors.duckduckgo import DuckDuckGoCollector
from for_eigyo.collectors.web import WebCollector
from for_eigyo.storage.database import Database
from for_eigyo.storage.models import AnalysisResult

logger = logging.getLogger(__name__)


class EnrichPipeline:
    """営業情報エンリッチメントワークフロー"""

    def __init__(self, db: Database | None = None):
        self.db = db or Database()
        self.ddg = DuckDuckGoCollector()
        self.web = WebCollector()
        self.keyword_extractor = KeywordExtractor()
        self.sentiment_analyzer = SentimentAnalyzer()
        self.ner = NamedEntityRecognizer()
        self.cluster_analyzer = ClusterAnalyzer()
        self.similarity_analyzer = SimilarityAnalyzer()
        self.lead_scorer = LeadScorer()

    def enrich_company(
        self,
        company_name: str,
        *,
        analyzers: list[str] | None = None,
        website: str | None = None,
    ) -> dict[str, Any]:
        """
        企業情報をエンリッチ

        Parameters
        ----------
        company_name : 企業名
        analyzers : 使用する分析 ["keywords", "sentiment", "ner", "scoring"]
        website : 企業Webサイト URL

        Returns
        -------
        {"company_name": str, "news": [...], "analyses": {...}}
        """
        analyzers = analyzers or ["keywords", "sentiment", "ner", "scoring"]
        result: dict[str, Any] = {"company_name": company_name, "analyses": {}}

        # ニュース検索
        news_results = self.ddg.search_news(company_name, max_results=10)
        result["news"] = [r.to_dict() for r in news_results]
        news_texts = [r.snippet for r in news_results if r.snippet]

        # Web サイトからテキスト取得
        web_text = ""
        if website:
            try:
                page_data = self.web.scrape_company_page(website)
                web_text = page_data.get("text", "")
                result["web_metadata"] = page_data.get("metadata", {})
            except Exception:
                logger.warning("Failed to scrape: %s", website)

        all_texts = news_texts + ([web_text] if web_text else [])

        # 各分析を実行
        if "keywords" in analyzers and all_texts:
            kw_result = self.keyword_extractor.extract(all_texts, top_n=20)
            result["analyses"]["keywords"] = kw_result
            self.db.save_analysis(AnalysisResult(
                analysis_type="keywords",
                target=company_name,
                result={"keywords": kw_result},
            ))

        if "sentiment" in analyzers and all_texts:
            sentiments = self.sentiment_analyzer.analyze_batch(all_texts)
            agg = self.sentiment_analyzer.aggregate(sentiments)
            result["analyses"]["sentiment"] = {
                "aggregate": agg,
                "details": sentiments,
            }
            self.db.save_analysis(AnalysisResult(
                analysis_type="sentiment",
                target=company_name,
                result=agg,
            ))

        if "ner" in analyzers and all_texts:
            combined_text = "\n".join(all_texts)
            entities = self.ner.extract(combined_text)
            result["analyses"]["ner"] = entities
            self.db.save_analysis(AnalysisResult(
                analysis_type="ner",
                target=company_name,
                result=entities,
            ))

        if "scoring" in analyzers:
            has_website = bool(website or web_text)
            entities = result["analyses"].get("ner", {})
            sentiment_agg = result["analyses"].get("sentiment", {}).get("aggregate", {})

            features = {
                "company_name": company_name,
                "has_website": has_website,
                "has_email": bool(entities.get("email")),
                "has_phone": bool(entities.get("phone")),
                "news_sentiment": sentiment_agg.get("avg_polarity", 0.0),
                "keyword_match_ratio": 0.5,  # デフォルト
                "days_since_activity": 0 if news_results else None,
            }
            score = self.lead_scorer.score_rule_based(features)
            result["analyses"]["scoring"] = {
                "score": score.score,
                "rank": score.rank,
                "factors": score.factors,
            }
            self.db.save_analysis(AnalysisResult(
                analysis_type="scoring",
                target=company_name,
                result={"score": score.score, "rank": score.rank, "factors": score.factors},
            ))

        return result

    def enrich_from_csv(
        self,
        csv_path: str,
        *,
        name_column: str = "name",
        website_column: str = "website",
        analyzers: list[str] | None = None,
    ) -> pd.DataFrame:
        """CSV ファイルの企業リストをエンリッチ"""
        df = pd.read_csv(csv_path)
        results: list[dict[str, Any]] = []

        for _, row in df.iterrows():
            name = str(row.get(name_column, ""))
            website = str(row.get(website_column, "")) if website_column in df.columns else None
            if not name:
                continue

            try:
                enriched = self.enrich_company(
                    name,
                    analyzers=analyzers,
                    website=website if website and website != "nan" else None,
                )
                flat = {"company_name": name}
                analyses = enriched.get("analyses", {})

                # スコア
                if "scoring" in analyses:
                    flat["score"] = analyses["scoring"]["score"]
                    flat["rank"] = analyses["scoring"]["rank"]

                # 感情
                if "sentiment" in analyses:
                    flat["sentiment"] = analyses["sentiment"]["aggregate"]["label"]
                    flat["polarity"] = analyses["sentiment"]["aggregate"]["avg_polarity"]

                # キーワード上位3
                if "keywords" in analyses:
                    top_kw = analyses["keywords"][:3]
                    flat["top_keywords"] = ", ".join(k["keyword"] for k in top_kw)

                # ニュース件数
                flat["news_count"] = len(enriched.get("news", []))

                results.append(flat)
            except Exception:
                logger.exception("Failed to enrich: %s", name)
                results.append({"company_name": name, "error": True})

        return pd.DataFrame(results)

    def cluster_companies(
        self,
        company_texts: list[str],
        company_names: list[str],
        n_clusters: int = 5,
    ) -> dict[str, Any]:
        """企業群をクラスタリング"""
        result = self.cluster_analyzer.cluster_texts(
            company_texts,
            n_clusters=n_clusters,
            labels=company_names,
        )
        self.db.save_analysis(AnalysisResult(
            analysis_type="cluster",
            target="batch",
            result=result,
        ))
        return result

    def find_similar(
        self,
        target_text: str,
        corpus_texts: list[str],
        corpus_names: list[str],
        top_n: int = 10,
    ) -> list[dict[str, Any]]:
        """類似企業検索"""
        return self.similarity_analyzer.find_similar_companies(
            target_text, corpus_texts, corpus_names, top_n=top_n
        )

    def enrich_with_llm(
        self,
        company_name: str,
        provider_name: str = "openai",
        task: str = "summarize",
    ) -> dict[str, Any]:
        """LLM を使ったエンリッチメント（APIキー必要）"""
        from for_eigyo.llm.base import get_provider, is_llm_available

        if not is_llm_available(provider_name):
            return {"error": f"{provider_name} API key not set"}

        provider = get_provider(provider_name)

        # まずコンベンショナルなエンリッチ結果を取得
        enriched = self.enrich_company(company_name)

        result: dict[str, Any] = {"company_name": company_name, "llm_provider": provider_name}

        if task == "summarize":
            news_text = "\n".join(
                f"- {n['title']}: {n['snippet']}" for n in enriched.get("news", [])
            )
            if news_text:
                result["summary"] = provider.summarize(news_text)

        elif task == "report":
            analyses_str = json.dumps(enriched.get("analyses", {}), ensure_ascii=False, indent=2)
            result["report"] = provider.report(analyses_str)

        elif task == "draft":
            company_info = json.dumps(enriched, ensure_ascii=False, indent=2, default=str)[:3000]
            result["draft"] = provider.generate_sales_draft(company_info)

        return result
