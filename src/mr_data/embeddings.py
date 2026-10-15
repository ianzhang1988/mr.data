from typing import Any, Optional


class FastembedEmbedding:
    """Lightweight ONNX-based embedding wrapper around ``fastembed.TextEmbedding``.

    This avoids pulling in PyTorch while still supporting high-quality local
    models such as Jina.

    The base class returns the full model output unchanged (no truncation).

    The embedding function itself is transparent to input text: callers store
    and query raw text directly (Jina v2 requires no query/document prefixes).
    """

    def __init__(
        self,
        model_name: str,
        dim: int,
        max_length: int = 512,
        cache_dir: Optional[str] = None,
        threads: Optional[int] = None,
        **kwargs: Any,
    ):
        try:
            from fastembed import TextEmbedding
        except ImportError as exc:
            raise ImportError(
                "fastembed is required for local embeddings. "
                "Install it with: uv pip install fastembed"
            ) from exc

        self._model = TextEmbedding(
            model_name=model_name,
            max_length=max_length,
            cache_dir=cache_dir,
            threads=threads,
            **kwargs,
        )
        self._model_name = model_name
        self._dim = dim

    def __call__(self, input: list[str]) -> list[list[float]]:
        embeddings = list(self._model.embed(input))
        return [emb.tolist() for emb in embeddings]

    @property
    def dim(self) -> int:
        return self._dim

    @property
    def model_name(self) -> str:
        return self._model_name


class JinaEmbedding(FastembedEmbedding):
    """Jina Embeddings v2 base zh — bilingual Chinese/English, 768 dims.

    Jina v2 models require no query/document prefixes; texts are embedded
    as-is.
    """

    def __init__(
        self,
        model_name: str = "jinaai/jina-embeddings-v2-base-zh",
        dim: int = 768,
        **kwargs: Any,
    ):
        super().__init__(model_name=model_name, dim=dim, **kwargs)
