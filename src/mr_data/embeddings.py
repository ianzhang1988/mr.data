from typing import Any, Optional


class FastembedEmbedding:
    """Lightweight ONNX-based embedding wrapper around ``fastembed.TextEmbedding``.

    This avoids pulling in PyTorch while still supporting high-quality local
    models such as Nomic and BGE.

    The base class returns the full model output unchanged. Subclasses that
    rely on matryoshka truncation (e.g. Nomic) should override ``__call__`` to
    slice the vectors to the desired dimension.

    Callers are responsible for adding model-specific prefixes:

    - Nomic documents: ``search_document: <text>``
    - Nomic queries: ``search_query: <text>``
    - BGE queries: ``Represent this sentence for searching relevant passages: <text>``
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


class NomicPersonalityEmbedding(FastembedEmbedding):
    """Nomic Embed Text v1.5 truncated to 512 dims (matryoshka)."""

    def __init__(
        self,
        model_name: str = "nomic-ai/nomic-embed-text-v1.5",
        dim: int = 512,
        **kwargs: Any,
    ):
        super().__init__(model_name=model_name, dim=dim, **kwargs)

    def __call__(self, input: list[str]) -> list[list[float]]:
        embeddings = list(self._model.embed(input))
        return [emb[: self._dim].tolist() for emb in embeddings]


class BGEMemoryEmbedding(FastembedEmbedding):
    """BGE-base-zh-v1.5 for Chinese/English memory retrieval (768 dims)."""

    def __init__(
        self,
        model_name: str = "BAAI/bge-base-zh-v1.5",
        dim: int = 768,
        **kwargs: Any,
    ):
        super().__init__(model_name=model_name, dim=dim, **kwargs)
