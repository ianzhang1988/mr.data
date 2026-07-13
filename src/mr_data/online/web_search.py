from typing import Optional

from mr_data.config import settings


class WebSearchTool:
    """Lightweight web search using DuckDuckGo (no API key required)."""

    def __init__(self, max_results: Optional[int] = None):
        self.max_results = max_results or settings.web_search_max_results

    def search(self, query: str) -> list[dict]:
        if not query or not query.strip():
            return []
        try:
            from duckduckgo_search import DDGS
        except ImportError:  # pragma: no cover
            return []

        try:
            with DDGS() as ddgs:
                results = ddgs.text(query, max_results=self.max_results)
        except Exception:
            # Fail open: web search should not break the dialogue flow.
            return []

        docs = []
        for idx, r in enumerate(results):
            docs.append({
                "id": f"web:{idx}",
                "page_content": f"{r.get('title', '')}\n{r.get('body', '')}",
                "metadata": {
                    "source_type": "web",
                    "url": r.get("href", ""),
                    "title": r.get("title", ""),
                },
            })
        return docs
