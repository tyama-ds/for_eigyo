"""gBizINFO API コレクター (経済産業省 法人情報API)"""

from __future__ import annotations

import logging
from typing import Any

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from for_eigyo.collectors.base import BaseCollector
from for_eigyo.storage.models import Company, SearchResult

logger = logging.getLogger(__name__)

GBIZINFO_BASE_URL = "https://info.gbiz.go.jp/hojin/v1"


class GBizInfoCollector(BaseCollector):
    """
    gBizINFO API を用いた法人情報取得

    API トークンが必要（https://info.gbiz.go.jp/ で取得可能・無料）
    トークンが無い場合は空結果を返す（エラーにはしない）
    """

    name = "gbizinfo"

    def __init__(self, api_token: str | None = None, timeout: float = 30.0):
        self.api_token = api_token
        self.timeout = timeout

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self.api_token:
            headers["X-hojinInfo-api-token"] = self.api_token
        return headers

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, max=10))
    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        url = f"{GBIZINFO_BASE_URL}{path}"
        with httpx.Client(timeout=self.timeout) as client:
            resp = client.get(url, headers=self._headers(), params=params)
            resp.raise_for_status()
            return resp.json()

    def search(
        self,
        query: str,
        *,
        max_results: int = 20,
        prefecture: str | None = None,
        **kwargs: Any,
    ) -> list[SearchResult]:
        """法人名キーワード検索"""
        if not self.api_token:
            logger.warning("gBizINFO API token not set. Skipping.")
            return []

        params: dict[str, Any] = {
            "name": query,
            "page": 1,
            "limit": min(max_results, 100),
        }
        if prefecture:
            params["prefecture"] = prefecture

        results: list[SearchResult] = []
        try:
            data = self._get("/hojin", params=params)
            for item in data.get("hojin-infos", []):
                results.append(
                    SearchResult(
                        query=query,
                        title=item.get("name", ""),
                        url=f"https://info.gbiz.go.jp/hojin/ichiran?hojinBango={item.get('corporate_number', '')}",
                        snippet=f"{item.get('location', '')} / {item.get('business_summary', '')}",
                        source="gbizinfo",
                        raw_data=item,
                    )
                )
        except Exception:
            logger.exception("gBizINFO search failed for: %s", query)

        return results

    def get_company_detail(self, corporate_number: str) -> Company | None:
        """法人番号から詳細を取得"""
        if not self.api_token:
            return None

        try:
            data = self._get(f"/hojin/{corporate_number}")
            info = data.get("hojin-infos", [{}])[0] if data.get("hojin-infos") else {}
            if not info:
                return None
            return Company(
                name=info.get("name", ""),
                corporate_number=info.get("corporate_number"),
                address=info.get("location"),
                industry=info.get("business_summary"),
                employee_count=info.get("employee_number"),
                capital=info.get("capital_stock"),
                founded=info.get("date_of_establishment"),
                website=info.get("company_url"),
                source="gbizinfo",
                raw_data=info,
            )
        except Exception:
            logger.exception("gBizINFO detail failed for: %s", corporate_number)
            return None

    def to_companies(self, results: list[SearchResult]) -> list[Company]:
        companies: list[Company] = []
        for r in results:
            info = r.raw_data
            companies.append(
                Company(
                    name=info.get("name", r.title),
                    corporate_number=info.get("corporate_number"),
                    address=info.get("location"),
                    industry=info.get("business_summary"),
                    employee_count=info.get("employee_number"),
                    capital=info.get("capital_stock"),
                    founded=info.get("date_of_establishment"),
                    website=info.get("company_url"),
                    source="gbizinfo",
                    raw_data=info,
                )
            )
        return companies
