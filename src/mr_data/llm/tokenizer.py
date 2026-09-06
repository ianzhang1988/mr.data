from typing import Optional

from mr_data.config import settings
from mr_data.logging import get_logger

logger = get_logger("mr_data.llm")


class TokenCounter:
    """Estimate token count for a given text.

    Uses tiktoken when available, otherwise falls back to a rough
    character-based heuristic (4 characters ≈ 1 token).
    """

    def __init__(self, model: Optional[str] = None):
        self.model = model or settings.tokenizer_model
        self._encoding = None
        self._fallback_warned = False

    def count(self, text: str) -> int:
        if not text:
            return 0
        try:
            import tiktoken

            if self._encoding is None:
                self._encoding = tiktoken.get_encoding(self.model)
            return len(self._encoding.encode(text))
        except Exception as exc:
            # Fallback heuristic for environments without tiktoken or unsupported encodings.
            if not self._fallback_warned:
                self._fallback_warned = True
                logger.warning(
                    "Tokenizer failed, falling back to character-based heuristic",
                    extra={
                        "event": "llm.tokenize_fallback",
                        "details": {
                            "model": self.model,
                            "text_len": len(text),
                            "error": str(exc),
                            "error_type": type(exc).__name__,
                        },
                    },
                )
            return len(text) // 4 + 1
