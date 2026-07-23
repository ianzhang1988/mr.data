import logging
from abc import ABC, abstractmethod
from typing import Optional
from urllib.parse import urlencode, urljoin

import requests
from bs4 import BeautifulSoup

from mr_data.config import settings

logger = logging.getLogger(__name__)


def _to_doc_format(results: list[dict]) -> list[dict]:
    """Normalize provider-specific rows into the standard web-search doc format."""
    docs = []
    for idx, r in enumerate(results):
        title = r.get("title", "")
        body = r.get("body", "")
        url = r.get("url", "")
        docs.append({
            "id": f"web:{idx}",
            "page_content": f"{title}\n{body}",
            "metadata": {
                "source_type": "web",
                "url": url,
                "title": title,
            },
        })
    return docs


class SearchProvider(ABC):
    """Abstract base class for web search providers."""

    name: str = ""

    def __init__(self, max_results: Optional[int] = None):
        self.max_results = max_results or settings.web_search_max_results

    @abstractmethod
    def search(self, query: str) -> list[dict]:
        """Return a list of docs in the standard web-search format."""
        raise NotImplementedError


class DuckDuckGoProvider(SearchProvider):
    """DuckDuckGo search provider (no API key required)."""

    name = "duckduckgo"

    def search(self, query: str) -> list[dict]:
        try:
            from duckduckgo_search import DDGS
        except ImportError:
            logger.warning("duckduckgo_search package is not installed")
            return []

        try:
            with DDGS() as ddgs:
                results = ddgs.text(query, max_results=self.max_results)
        except Exception as exc:  # pragma: no cover
            logger.warning("DuckDuckGo search failed: %s", exc)
            return []

        rows = [
            {
                "title": r.get("title", ""),
                "body": r.get("body", ""),
                "url": r.get("href", ""),
            }
            for r in results
        ]
        return _to_doc_format(rows[: self.max_results])


class SearxngProvider(SearchProvider):
    """SearXNG search provider using a configurable base URL."""

    name = "searxng"

    def __init__(
        self,
        max_results: Optional[int] = None,
        base_url: Optional[str] = None,
    ):
        super().__init__(max_results)
        self.base_url = base_url or getattr(settings, "searxng_base_url", "")

    def search(self, query: str) -> list[dict]:
        if not self.base_url:
            logger.warning("SearXNG base URL is not configured")
            return []

        url = urljoin(self.base_url.rstrip("/"), "/search")
        params = {"q": query, "format": "json"}
        try:
            resp = requests.get(url, params=params, timeout=10)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            logger.warning("SearXNG search failed: %s", exc)
            return []

        results = data.get("results", [])
        rows = [
            {
                "title": r.get("title", ""),
                "body": r.get("content", r.get("snippet", "")),
                "url": r.get("url", r.get("href", "")),
            }
            for r in results
        ]
        return _to_doc_format(rows[: self.max_results])


class BraveProvider(SearchProvider):
    """Brave Search API provider."""

    name = "brave"

    API_URL = "https://api.search.brave.com/res/v1/web/search"

    def __init__(
        self,
        max_results: Optional[int] = None,
        api_key: Optional[str] = None,
    ):
        super().__init__(max_results)
        self.api_key = api_key or getattr(settings, "brave_api_key", "")

    def search(self, query: str) -> list[dict]:
        if not self.api_key:
            logger.warning("Brave API key is not configured")
            return []

        headers = {
            "Accept": "application/json",
            "X-Subscription-Token": self.api_key,
        }
        params = {"q": query, "count": self.max_results}
        try:
            resp = requests.get(self.API_URL, headers=headers, params=params, timeout=10)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            logger.warning("Brave search failed: %s", exc)
            return []

        results = data.get("web", {}).get("results", [])
        rows = [
            {
                "title": r.get("title", ""),
                "body": r.get("description", ""),
                "url": r.get("url", ""),
            }
            for r in results
        ]
        return _to_doc_format(rows[: self.max_results])


class BingProvider(SearchProvider):
    """Bing Web Search API provider."""

    name = "bing"

    API_URL = "https://api.bing.microsoft.com/v7.0/search"

    def __init__(
        self,
        max_results: Optional[int] = None,
        api_key: Optional[str] = None,
    ):
        super().__init__(max_results)
        self.api_key = api_key or getattr(settings, "bing_api_key", "")

    def search(self, query: str) -> list[dict]:
        if not self.api_key:
            logger.warning("Bing API key is not configured")
            return []

        headers = {"Ocp-Apim-Subscription-Key": self.api_key}
        params = {"q": query, "count": self.max_results}
        try:
            resp = requests.get(self.API_URL, headers=headers, params=params, timeout=10)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            logger.warning("Bing search failed: %s", exc)
            return []

        results = data.get("webPages", {}).get("value", [])
        rows = [
            {
                "title": r.get("name", ""),
                "body": r.get("snippet", ""),
                "url": r.get("url", ""),
            }
            for r in results
        ]
        return _to_doc_format(rows[: self.max_results])


class GoogleCseProvider(SearchProvider):
    """Google Custom Search Engine API provider."""

    name = "google_cse"

    API_URL = "https://www.googleapis.com/customsearch/v1"

    def __init__(
        self,
        max_results: Optional[int] = None,
        api_key: Optional[str] = None,
        cse_id: Optional[str] = None,
    ):
        super().__init__(max_results)
        self.api_key = api_key or getattr(settings, "google_api_key", "")
        self.cse_id = cse_id or getattr(settings, "google_cse_id", "")

    def search(self, query: str) -> list[dict]:
        if not self.api_key or not self.cse_id:
            logger.warning("Google CSE API key or CSE ID is not configured")
            return []

        params = {
            "key": self.api_key,
            "cx": self.cse_id,
            "q": query,
            "num": self.max_results,
        }
        try:
            resp = requests.get(self.API_URL, params=params, timeout=10)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            logger.warning("Google CSE search failed: %s", exc)
            return []

        results = data.get("items", [])
        rows = [
            {
                "title": r.get("title", ""),
                "body": r.get("snippet", ""),
                "url": r.get("link", ""),
            }
            for r in results
        ]
        return _to_doc_format(rows[: self.max_results])


class BaiduProvider(SearchProvider):
    """Baidu web search provider using requests and BeautifulSoup."""

    name = "baidu"

    BASE_URL = "https://www.baidu.com/s"

    def search(self, query: str) -> list[dict]:
        params = {"wd": query}
        try:
            resp = requests.get(
                self.BASE_URL,
                params=params,
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=10,
            )
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "html.parser")
        except Exception as exc:
            logger.warning("Baidu search failed: %s", exc)
            return []

        rows = []
        for container in soup.select(".result")[: self.max_results]:
            title_tag = container.select_one("h3 a")
            if not title_tag:
                continue
            title = title_tag.get_text(strip=True)
            url = title_tag.get("href", "")
            snippet_tag = container.select_one(".content-right_8Zs40, .c-abstract, [class*='abstract']")
            body = ""
            if snippet_tag:
                body = snippet_tag.get_text(strip=True)
            rows.append({"title": title, "body": body, "url": url})

        return _to_doc_format(rows[: self.max_results])


class Qihoo360Provider(SearchProvider):
    """Qihoo 360 (so.com) web search provider using requests and BeautifulSoup."""

    name = "qihoo360"

    BASE_URL = "https://www.so.com/s"

    def search(self, query: str) -> list[dict]:
        params = {"q": query}
        try:
            resp = requests.get(
                self.BASE_URL,
                params=params,
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=10,
            )
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "html.parser")
        except Exception as exc:
            logger.warning("Qihoo360 search failed: %s", exc)
            return []

        rows = []
        for container in soup.select(".result")[: self.max_results]:
            title_tag = container.select_one("h3 a")
            if not title_tag:
                continue
            title = title_tag.get_text(strip=True)
            url = title_tag.get("href", "")
            snippet_tag = container.select_one(".res-desc, .content-wrapper, [class*='desc']")
            body = ""
            if snippet_tag:
                body = snippet_tag.get_text(strip=True)
            rows.append({"title": title, "body": body, "url": url})

        return _to_doc_format(rows[: self.max_results])
