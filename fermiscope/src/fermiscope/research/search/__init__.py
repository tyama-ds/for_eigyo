"""検索プロバイダ抽象化。"""

from fermiscope.research.search.base import SearchProvider, SearchProviderError
from fermiscope.research.search.brave import BraveSearchProvider
from fermiscope.research.search.duckduckgo import DuckDuckGoSearchProvider
from fermiscope.research.search.mock import MockSearchProvider
from fermiscope.research.search.service import SearchBudgetExceeded, SearchService

__all__ = [
    "BraveSearchProvider",
    "DuckDuckGoSearchProvider",
    "MockSearchProvider",
    "SearchBudgetExceeded",
    "SearchProvider",
    "SearchProviderError",
    "SearchService",
]
