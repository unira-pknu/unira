"""Optional cross-encoder reranking for retrieved RAG chunks."""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List

from .evidence_context import select_evidence

logger = logging.getLogger(__name__)


class CrossEncoderReranker:
    """Thin wrapper around sentence-transformers CrossEncoder."""

    def __init__(self, *, model_name: str, device: str, quantize: bool = False) -> None:
        from sentence_transformers import CrossEncoder

        started = time.perf_counter()
        self.model_name = model_name
        self._model = CrossEncoder(model_name, device=device)
        quantized = False
        if quantize and device == "cpu":
            import torch

            self._model.model = torch.quantization.quantize_dynamic(
                self._model.model, {torch.nn.Linear}, dtype=torch.qint8
            )
            quantized = True
        elif quantize:
            logger.warning("reranker int8 quantization is CPU-only; ignored for device=%s", device)
        logger.info(
            "reranker loaded model=%s device=%s quantized=%s latency_ms=%.1f",
            model_name,
            device,
            quantized,
            (time.perf_counter() - started) * 1000,
        )

    def score(self, question: str, rows: List[Dict[str, Any]]) -> List[float]:
        if not rows:
            return []
        pairs = [(question, _row_text(row, question=question)) for row in rows]
        started = time.perf_counter()
        scores = self._model.predict(pairs, show_progress_bar=False)
        values = [float(score) for score in scores]
        logger.info(
            "reranker scored model=%s candidates=%d latency_ms=%.1f",
            self.model_name,
            len(rows),
            (time.perf_counter() - started) * 1000,
        )
        return values


def _row_text(row: Dict[str, Any], max_chars: int = 1200, *, question: str = "") -> str:
    metadata = row.get("metadata") or {}
    title = (
        metadata.get("doc_title")
        or metadata.get("source_file")
        or metadata.get("title")
        or row.get("title")
        or row.get("source_slug")
        or ""
    )
    content = str(row.get("content") or "")
    if question:
        # Book chapters can put the answer after a long wrapper/timetable.
        # The reranker must see that passage, not only the original prefix.
        content = select_evidence(content, question=question, target_chars=max_chars, metadata=metadata)
    text = f"{title}\n{content}".strip()
    return text[:max_chars]


def normalize_scores(values: List[float]) -> List[float]:
    if not values:
        return []
    low = min(values)
    high = max(values)
    if high == low:
        return [0.5 for _ in values]
    return [(value - low) / (high - low) for value in values]
