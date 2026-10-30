"""Web search: Bing, Baidu and Bilibili engines behind one service and the LLM tools."""

from cgbot.search.models import SearchOutcome, SearchResult
from cgbot.search.service import SearchService
from cgbot.search.tools import search_tools

__all__ = ["SearchOutcome", "SearchResult", "SearchService", "search_tools"]
