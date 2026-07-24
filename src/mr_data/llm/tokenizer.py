from typing import Optional

from mr_data.config import settings


class TokenCounter:
    """Estimate token count for a given text.

    Uses tiktoken when available, otherwise falls back to a rough
    character-based heuristic (4 characters ≈ 1 token).
    """

    def __init__(self, model: Optional[str] = None):
        self.model = model or settings.tokenizer_model
        self._encoding = None

    def count(self, text: str) -> int:
        if not text:
            return 0
        try:
            import tiktoken

            if self._encoding is None:
                self._encoding = tiktoken.get_encoding(self.model)
            return len(self._encoding.encode(text))
        except Exception:
            # Fallback heuristic for environments without tiktoken or unsupported encodings.
            return len(text) // 4 + 1
