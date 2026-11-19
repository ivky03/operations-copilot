"""Turn text into embeddings: lists of numbers where similar meanings are close together.

Runs locally on the CPU with fastembed (ONNX runtime), so it's free, has no rate limits and
gives the same vectors every time, which keeps retrieval tests reproducible.

Questions and passages are embedded differently on purpose: the bge model expects a short
instruction in front of a search query, which fastembed's `query_embed` adds.
"""

from functools import lru_cache
from typing import Protocol

from app.config import get_settings


class Embedder(Protocol):
    model_name: str

    def passages(self, texts: list[str]) -> list[list[float]]: ...

    def query(self, text: str) -> list[float]: ...


class FastEmbedder:
    def __init__(self, model_name: str, cache_dir: str) -> None:
        from fastembed import TextEmbedding  # imported here: loading takes a second or two

        self.model_name = model_name
        self._model = TextEmbedding(model_name, cache_dir=cache_dir)

    def passages(self, texts: list[str]) -> list[list[float]]:
        return [v.tolist() for v in self._model.passage_embed(texts)]

    def query(self, text: str) -> list[float]:
        return next(iter(self._model.query_embed([text]))).tolist()


@lru_cache
def get_embedder() -> Embedder:
    """One model per process: loading it is the slow part, embedding is milliseconds."""
    settings = get_settings()
    return FastEmbedder(settings.embedding_model, settings.embedding_cache_dir)
