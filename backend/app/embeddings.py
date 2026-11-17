"""Embedding model wrapper. Loaded once at app startup."""

from __future__ import annotations

import logging
from typing import List

logger = logging.getLogger(__name__)


class Embedder:
    """Wraps SentenceTransformer to produce normalized query embeddings.

    The model is loaded eagerly in `__init__`, so construction is the slow
    part (a few seconds). `encode_query` is fast on CPU for a single string.
    """

    def __init__(
        self,
        *,
        model_name: str,
        device: str,
        expected_dimensions: int,
        query_prefix: str = "",
    ) -> None:
        from sentence_transformers import SentenceTransformer  # heavy import

        self._query_prefix = query_prefix

        logger.info("Loading embedding model: %s (device=%s)", model_name, device)
        self._model = SentenceTransformer(model_name, device=device)
        actual = int(self._model.get_sentence_embedding_dimension() or 0)
        if actual != expected_dimensions:
            raise ValueError(
                f"Embedding model {model_name} produces {actual} dimensions, "
                f"but EXPECTED_DIMENSIONS={expected_dimensions}. "
                "Either change the model or migrate the rag_chunks.embedding column."
            )
        self._dimensions = actual
        logger.info("Embedding model loaded (dim=%d)", actual)

    @property
    def dimensions(self) -> int:
        return self._dimensions

    def encode_query(self, query: str) -> List[float]:
        vector = self._model.encode(
            [self._query_prefix + query],
            normalize_embeddings=True,
            show_progress_bar=False,
        )[0]
        # Round to keep payload size predictable; precision loss is negligible
        # for cosine similarity at 1e-8.
        return [round(float(value), 8) for value in vector]
