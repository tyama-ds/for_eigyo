"""営業先発掘パイプライン"""

from __future__ import annotations

import logging
import os
from typing import Any

import pandas as pd

from for_eigyo.collectors.duckduckgo import DuckDuckGoCollector
from for_eigyo.collectors.gbizinfo import GBizInfoCollector
from for_eigyo.storage.database import Database
from for_eigyo.storage.models import Company, SearchResult

logger = logging.getLogger(__name__)


class ProspectPipeline:
    """営業先発掘ワークフロー"""

    def __init__(self, db: Database | None = None):
        self.db = db or Database()
        self.ddg = DuckDuckGoCollector()
        self.gbiz = GBizInfoCollector(api_token=os.environ.get("GBIZINFO_API_TOKEN"))

    def search(
        self,
        query: str,
        *,
        industry: str | None = None,
        region: str | None = None,
        max_results: int = 20,
        sources: list[str] | None = None,
    ) -> dict[str, Any]:
        """
        複数ソースから営業先を検索

        Parameters
        ----------
        query : 検索キーワード
        industry : 業種フィルタ
        region : 地域フィルタ
        max_results : ソースごとの最大件数
        sources : 使用するソース ["duckduckgo", "gbizinfo"]

        Returns
        -------
        {"companies": list[Company], "search_results": list[SearchResult], "summary": dict}
        """
        sources = sources or ["duckduckgo", "gbizinfo"]

        # 検索クエリを構築
        search_query = query
        if industry:
            search_query += f" {industry}"
        if region:
            search_query += f" {region}"

        all_results: list[SearchResult] = []
        all_companies: list[Company] = []

        # DuckDuckGo
        if "duckduckgo" in sources:
            logger.info("Searching DuckDuckGo: %s", search_query)
            ddg_results = self.ddg.search(search_query, max_results=max_results)
            all_results.extend(ddg_results)
            all_companies.extend(self.ddg.to_companies(ddg_results))

            # ニュース検索も追加
            news_results = self.ddg.search_news(search_query, max_results=max_results)
            all_results.extend(news_results)

        # gBizINFO
        if "gbizinfo" in sources and self.gbiz.api_token:
            logger.info("Searching gBizINFO: %s", query)
            gbiz_results = self.gbiz.search(
                query,
                max_results=max_results,
                prefecture=region,
            )
            all_results.extend(gbiz_results)
            all_companies.extend(self.gbiz.to_companies(gbiz_results))

        # DB に保存
        self.db.save_search_results(all_results)
        self.db.upsert_companies(all_companies)

        logger.info(
            "Prospect search complete: %d results, %d companies",
            len(all_results),
            len(all_companies),
        )

        return {
            "companies": all_companies,
            "search_results": all_results,
            "summary": {
                "total_results": len(all_results),
                "total_companies": len(all_companies),
                "sources_used": sources,
                "query": search_query,
            },
        }

    def search_to_dataframe(self, **kwargs: Any) -> pd.DataFrame:
        """search() の結果を DataFrame で返す"""
        result = self.search(**kwargs)
        companies = result["companies"]
        if not companies:
            return pd.DataFrame()
        return pd.DataFrame([c.to_dict() for c in companies])

    def export_csv(self, path: str, **kwargs: Any) -> int:
        """検索結果を CSV に出力"""
        df = self.search_to_dataframe(**kwargs)
        if df.empty:
            return 0
        df.to_csv(path, index=False, encoding="utf-8-sig")
        return len(df)
