"""データ収集モジュール"""

from for_eigyo.collectors.base import BaseCollector
from for_eigyo.collectors.duckduckgo import DuckDuckGoCollector
from for_eigyo.collectors.gbizinfo import GBizInfoCollector
from for_eigyo.collectors.web import WebCollector

__all__ = ["BaseCollector", "DuckDuckGoCollector", "GBizInfoCollector", "WebCollector"]
