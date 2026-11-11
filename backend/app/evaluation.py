"""Opt-in evidence snapshots for offline quality evaluation."""

from __future__ import annotations

import hashlib
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from .generation import _format_source_block, _source_content
from .search_status import SearchResults
from .retrieval_trace import candidate_key, source_identity

# Explicitly exclude API keys, tokens, user identity and database credentials.
SETTING_NAMES = (
    "openai_model", "openai_temperature", "openai_max_tokens", "embedding_model",
    "expected_dimensions", "rpc_names", "rag_top_k", "rag_first_stage_k",
    "rag_min_similarity", "rag_max_chunks_per_url", "rag_max_lexical_chunks_per_url",
    "rag_priority_weight", "rag_dataset_priority_weight", "rag_source_kind_weight",
    "rag_title_anchor_enabled", "rag_rpc_concurrency", "reranker_enabled",
    "reranker_model", "reranker_weight", "reranker_quantize", "max_chars_per_chunk",
    "rag_context_neighbor_sources",
)
METADATA_NAMES = (
    "source_dataset", "ingestion_dataset", "department_id", "department_name",
    "published_at", "posted_at", "date", "year", "term", "applicability_note",
    "source_link_kind", "structured_table",
    "request_query",
    "request_coverage", "comparison_targets",
    "retrieval_incomplete", "unresolved_request",
    "catalog_table",
)


def evidence_snapshot(state: Any, plan: Any, rows: list[dict], sources: list[Any], *, question: str | None = None,
                      answer: str | None = None) -> dict:
    """Keep full retrieved chunks separately from the short UI source excerpts."""
    direct = bool(rows and "_direct_answer" in rows[0])
    evidence_rows = rows[1:] if direct else rows
    if len(evidence_rows) != len(sources):
        raise ValueError("evaluation source rows do not match citation numbering")
    limit = state.settings.max_chars_per_chunk
    question = question if question is not None else (plan.resolved_question or " ".join(plan.queries))
    cited = sorted({int(n) for n in re.findall(r"\[(\d+)\]", answer or "")}) if answer is not None else None
    captured = []
    for index, (row, source) in enumerate(zip(evidence_rows, sources), start=1):
        metadata = row.get("metadata") or {}
        content = str(row.get("content") or "")
        item = {
            "index": index, "title": source.title, "uri": source.uri,
            "content": content,
            "shown_content": None if direct else _source_content(row, limit, question),
            "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
            **source_identity(row), "key": candidate_key(row),
            "cited_in_answer": index in cited if cited is not None else None,
            "metadata": {key: metadata[key] for key in METADATA_NAMES if key in metadata},
        }
        if row.get("_context_chunks"):
            item["context_chunks"] = [{
                **source_identity(chunk), "key": candidate_key(chunk), "id": chunk.get("id"),
                "content": chunk["content"],
                "content_sha256": hashlib.sha256(chunk["content"].encode("utf-8")).hexdigest(),
                "metadata": {key: value for key, value in (chunk.get("metadata") or {}).items() if key in METADATA_NAMES},
            } for chunk in row["_context_chunks"]]
        captured.append(item)
    return {
        "schema_version": 1,
        "captured_at": datetime.now(timezone(timedelta(hours=9))).isoformat(),
        "body_scope": "retrieved_chunks",
        "answer_mode": "direct" if direct else plan.route,
        "backend_revision": os.getenv("BACKEND_REVISION") or None,
        "system_prompt_sha256": hashlib.sha256(state.system_prompt.encode("utf-8")).hexdigest(),
        "settings": {key: getattr(state.settings, key) for key in SETTING_NAMES if hasattr(state.settings, key)},
        "plan": plan.model_dump(mode="json"),
        "prompt_source_block": None if direct else _format_source_block(evidence_rows, limit, question),
        "sources": captured,
        "retrieval": rows.diagnostics() if isinstance(rows, SearchResults) else None,
        "generation": {
            "status": "not_recorded" if answer is None else ("direct" if direct else (
                plan.route if plan.route != "search" else ("generated" if evidence_rows else "no_sources"))),
            "answer_sha256": hashlib.sha256(answer.encode()).hexdigest() if answer is not None else None,
            "cited_source_indices": cited,
            "invalid_citation_indices": [n for n in cited if not 1 <= n <= len(sources)] if cited is not None else None,
        },
    }
