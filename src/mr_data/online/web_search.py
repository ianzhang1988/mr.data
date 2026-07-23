import logging
from typing import Optional

from mr_data.config import settings
from mr_data.online.search_providers import (
    BaiduProvider,
    BingProvider,
    BraveProvider,
    DuckDuckGoProvider,
    GoogleCseProvider,
    Qihoo360Provider,
    SearchProvider,
    SearxngProvider,
)

logger = logging.getLogger(__name__)


_PROVIDER_CLASSES: list[type[SearchProvider]] = [
    DuckDuckGoProvider,
    SearxngProvider,
    BraveProvider,
    BingProvider,
    GoogleCseProvider,
    BaiduProvider,
    Qihoo360Provider,
]
_PROVIDER_MAP: dict[str, type[SearchProvider]] = {
    cls.name: cls for cls in _PROVIDER_CLASSES
}


class WebSearchTool:
    """Web search dispatcher that tries multiple providers in order."""

    def __init__(
        self,
        max_results: Optional[int] = None,
        providers: Optional[list[str]] = None,
    ):
        self.max_results = max_results or settings.web_search_max_results
        self.providers = providers or getattr(
            settings, "web_search_providers", ["duckduckgo"]
        )

    def search(self, query: str) -> list[dict]:
        if not query or not query.strip():
            return []

        for name in self.providers:
            provider_cls = _PROVIDER_MAP.get(name)
            if provider_cls is None:
                logger.warning("Unknown web search provider: %s", name)
                continue

            try:
                provider = self._build_provider(provider_cls)
                results = provider.search(query)
            except Exception as exc:
                logger.warning("Web search provider %s raised: %s", name, exc)
                continue

            if results:
                return results

            logger.warning("Web search provider %s returned no results", name)

        return []

    def _build_provider(self, provider_cls: type[SearchProvider]) -> SearchProvider:
        """Instantiate a provider, passing relevant config from settings."""
        kwargs: dict = {"max_results": self.max_results}

        if provider_cls is SearxngProvider:
            kwargs["base_url"] = getattr(settings, "searxng_base_url", None) or None
        elif provider_cls is BraveProvider:
            kwargs["api_key"] = getattr(settings, "brave_api_key", None) or None
        elif provider_cls is BingProvider:
            kwargs["api_key"] = getattr(settings, "bing_api_key", None) or None
        elif provider_cls is GoogleCseProvider:
            kwargs["api_key"] = getattr(settings, "google_api_key", None) or None
            kwargs["cse_id"] = getattr(settings, "google_cse_id", None) or None

        return provider_cls(**kwargs)
