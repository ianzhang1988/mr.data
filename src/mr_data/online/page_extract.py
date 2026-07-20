from typing import Optional

from mr_data.config import settings


class PageExtractor:
    """Fetch a web page and extract readable article text.

    Uses ``trafilatura`` as the primary extractor and falls back to
    ``requests`` + ``BeautifulSoup`` if it is unavailable or fails.
    """

    def __init__(self, max_length: Optional[int] = None, timeout: int = 10):
        self.max_length = max_length or settings.web_extract_max_length
        self.timeout = timeout

    def extract(self, url: str) -> Optional[str]:
        if not url or not url.startswith(("http://", "https://")):
            return None

        text = self._extract_with_trafilatura(url)
        if not text:
            text = self._extract_with_fallback(url)

        if text:
            text = text.strip()
            if self.max_length and len(text) > self.max_length:
                text = text[: self.max_length].rsplit("\n", 1)[0]
        return text

    def _extract_with_trafilatura(self, url: str) -> Optional[str]:
        try:
            import trafilatura

            downloaded = trafilatura.fetch_url(url, timeout=self.timeout)
            if not downloaded:
                return None
            return trafilatura.extract(downloaded, include_comments=False, include_tables=False)
        except Exception:
            return None

    def _extract_with_fallback(self, url: str) -> Optional[str]:
        try:
            import requests
            from bs4 import BeautifulSoup
        except ImportError:  # pragma: no cover
            return None

        try:
            response = requests.get(url, timeout=self.timeout, headers={"User-Agent": "mr-data/0.1"})
            response.raise_for_status()
        except Exception:
            return None

        soup = BeautifulSoup(response.text, "html.parser")

        # Remove non-content elements.
        for tag in soup(["script", "style", "nav", "footer", "header", "aside"]):
            tag.decompose()

        # Prefer article/main content.
        article = soup.find("article") or soup.find("main") or soup.find("body")
        if article:
            return article.get_text(separator="\n", strip=True)
        return soup.get_text(separator="\n", strip=True)
