"""Bounded evaluation traces: identities/scores, never embeddings or raw metadata."""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any, Iterable

from .source_scope import source_department

RPC_DATASETS = {
    "match_rag_documents": "rag", "match_rule_documents": "rule",
    "match_pknu_notice_documents": "pknu_notice", "match_pknu_main_documents": "pknu_main",
    "match_pknu_syllabus_documents": "pknu_syllabus",
}
MAX_TRACE_RECORDS = 10000
SCORE_NAMES = ("similarity", "priority_score", "dataset_priority", "final_score",
               "rerank_score", "rerank_final_score")


def _identifier(value: Any) -> str | None:
    return str(value) if value is not None and str(value) else None


def source_identity(row: dict, rpc_name: str | None = None) -> dict:
    metadata = row.get("metadata") or {}
    rpc = rpc_name or row.get("_rpc_name") or metadata.get("rpc_name")
    rpc = str(rpc).removesuffix("_lexical") if rpc else None
    chunk_id = row.get("chunk_id")  # 0 is an identifier; never replace it with row.id.
    if chunk_id is not None and type(chunk_id) not in (str, int):
        chunk_id = str(chunk_id)
    position = row.get("chunk_index")
    department = source_department(row)
    if department is None and metadata.get("ingestion_dataset"):
        department = source_department({**row, "metadata": {**metadata, "source_dataset": None}})
    return {
        "source_id": _identifier(row.get("source_id")),
        "source_ids": [_identifier(value) for value in metadata["source_ids"] if _identifier(value) is not None]
        if isinstance(metadata.get("source_ids"), list) else None,
        "chunk_id": chunk_id,
        "chunk_row_id": None if row.get("_source_row") or row.get("_synthetic_source") else _identifier(row.get("id")),
        "chunk_index": position if type(position) is int else None,
        "source_dataset": _identifier(metadata.get("source_dataset") or RPC_DATASETS.get(rpc)
                                      or {"rag_sources": "rag", "pknu_notice_sources": "pknu_notice"}.get(metadata.get("catalog_table"))),
        "ingestion_dataset": _identifier(metadata.get("ingestion_dataset")),
        # Resolve from source metadata/registered source URL, never query target.
        "department_id": department,
        "rpc_name": rpc,
    }


def candidate_key(row: dict, rpc_name: str | None = None) -> str:
    if row.get("_trace_key"):
        return row["_trace_key"]
    identity = source_identity(row, rpc_name)
    family = identity["rpc_name"] or identity["source_dataset"] or "unknown"
    if identity["chunk_row_id"] is not None:
        return f"{family}|row:{identity['chunk_row_id']}"
    if identity["source_id"] is not None:
        if row.get("_source_row"):
            return f"{family}|source:{identity['source_id']}"
        if identity["chunk_index"] is not None:
            return f"{family}|source:{identity['source_id']}|index:{identity['chunk_index']}"
        if identity["chunk_id"] is not None:
            return f"{family}|source:{identity['source_id']}|chunk:{identity['chunk_id']}"
    # This digest is a trace key, not an invented DB source/chunk identifier.
    fallback = {**identity, "content": str(row.get("content") or ""),
                "uri": str(row.get("url") or row.get("uri") or "")}
    digest = hashlib.sha256(json.dumps(fallback, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return f"{family}|unidentified:{digest}"


class RetrievalTrace:
    def __init__(self, *, max_records: int = MAX_TRACE_RECORDS) -> None:
        self.max_records = max_records
        self.recorded = 0
        self.truncated = False
        self.stages: list[dict] = []
        self.decisions: list[dict] = []
        self.reranker: dict = {}
        self.parameters: dict = {}

    def _entry(self, row: dict, rpc_name: str | None = None) -> dict | None:
        if self.recorded >= self.max_records:
            self.truncated = True
            return None
        self.recorded += 1
        key = candidate_key(row, rpc_name)
        row.setdefault("_trace_key", key)
        content = str(row.get("content") or "")
        metadata = row.get("metadata") or {}
        item = {"key": key, **source_identity(row, rpc_name),
                "title": str(metadata.get("doc_title") or row.get("title") or "")[:300],
                "content_sha256": hashlib.sha256(content.encode()).hexdigest(), "content_chars": len(content),
                "lexical_only": bool(row.get("_lexical_only"))}
        for name in SCORE_NAMES:
            value = row.get(name)
            if type(value) in (int, float) and math.isfinite(value):
                item[name] = value
        if row.get("_lexical_rank_index") is not None:
            item["lexical_rank_index"] = row["_lexical_rank_index"]
        return item

    def snapshot(self, stage: str, rows: Iterable[dict], *, rpc_name: str | None = None, **details) -> None:
        rows = list(rows)
        entries = []
        for rank, row in enumerate(rows, 1):
            entry = self._entry(row, rpc_name)
            if entry is not None:
                entries.append({"rank": rank, **entry})
        self.stages.append({"stage": stage, "count": len(rows), "recorded_count": len(entries),
                            "candidates": entries, **details, **({"rpc_name": rpc_name} if rpc_name else {})})

    def reject(self, stage: str, row: dict, reason: str, rpc_name: str | None = None) -> None:
        entry = self._entry(row, rpc_name)
        if entry is not None:
            self.decisions.append({"stage": stage, "reason": reason, **entry})

    def export(self) -> dict:
        return {"schema_version": 1, "complete": not self.truncated,
                "record_limit": self.max_records, "recorded_count": self.recorded,
                "stages": self.stages, "decisions": self.decisions, "reranker": self.reranker,
                "parameters": self.parameters}
