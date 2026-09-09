from dataclasses import dataclass
from typing import Optional

from mr_data.config import settings
from mr_data.logging import get_logger

logger = get_logger("mr_data.online")


@dataclass
class ExtractedPage:
    text: str
    url: str  # 跟随重定向后的最终 URL


class PageExtractor:
    """Fetch a web page and extract readable article text.

    Fetching is done once with ``requests`` (which follows redirects, so the
    final real URL is available); the HTML is then parsed by ``trafilatura``
    as the primary extractor, falling back to ``BeautifulSoup`` if it is
    unavailable or fails.
    """

    def __init__(self, max_length: Optional[int] = None, timeout: int = 10):
        self.max_length = max_length or settings.web_extract_max_length
        self.timeout = timeout

    def extract(self, url: str) -> Optional[ExtractedPage]:
        if not url or not url.startswith(("http://", "https://")):
            return None

        fetched = self._fetch(url)
        if not fetched:
            return None
        html, final_url = fetched

        text = self._extract_with_trafilatura(html)
        if not text:
            text = self._extract_with_fallback(html)

        if not text:
            return None
        text = text.strip()
        if self.max_length and len(text) > self.max_length:
            text = text[: self.max_length].rsplit("\n", 1)[0]
        return ExtractedPage(text=text, url=final_url)

    def _fetch(self, url: str) -> Optional[tuple[str, str]]:
        try:
            import requests
        except ImportError:  # pragma: no cover
            return None

        try:
            response = requests.get(url, timeout=self.timeout, headers={"User-Agent": "mr-data/0.1"})
            response.raise_for_status()
            return response.text, response.url
        except Exception as exc:
            logger.warning(
                "Page fetch failed",
                extra={
                    "event": "web.page_fetch_failed",
                    "details": {"url": url, "error": str(exc)},
                },
            )
            return None

    def _extract_with_trafilatura(self, html: str) -> Optional[str]:
        try:
            import trafilatura

            return trafilatura.extract(html, include_comments=False, include_tables=False)
        except Exception as exc:
            logger.warning(
                "Primary page extraction failed; falling back to requests",
                extra={
                    "event": "web.page_extract_primary_failed",
                    "details": {"error": str(exc)},
                },
            )
            return None

    def _extract_with_fallback(self, html: str) -> Optional[str]:
        try:
            from bs4 import BeautifulSoup
        except ImportError:  # pragma: no cover
            return None

        soup = BeautifulSoup(html, "html.parser")

        # Remove non-content elements.
        for tag in soup(["script", "style", "nav", "footer", "header", "aside"]):
            tag.decompose()

        # Prefer article/main content.
        article = soup.find("article") or soup.find("main") or soup.find("body")
        if article:
            return article.get_text(separator="\n", strip=True)
        return soup.get_text(separator="\n", strip=True)
