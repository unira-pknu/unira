"""Operator-run search verification of a fully verified missing-ingest selection.

Uses real local models and Supabase retrieval, never answer generation or OpenAI.
"""
from __future__ import annotations

from collections import Counter
import hashlib
import inspect
import logging
import os
from pathlib import Path, PurePosixPath
import re
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.recovery.common import atomic, digest, read, receipt

RPCS = {"rag": "match_rag_documents", "pknu_notice": "match_pknu_notice_documents", "pknu_main": "match_pknu_main_documents"}


def source_name(entry):
    name = PurePosixPath(entry["source_path"].replace("\\", "/").split("!/", 1)[-1]).stem
    return re.sub(r"[_-](?:Re)?(?=[0-9a-f]{12,}$)(?=[0-9a-f]*[a-f])[0-9a-f]+$", "", name, flags=re.I).strip()


def candidates(entries, family, limit):
    """Avoid testing six copies of one national manual or recruitment notice."""
    def score(entry):
        text = str(entry.get("title") or "") + " " + source_name(entry)
        return (sum(4 for term in ("신청 매뉴얼", "모집요강", "졸업기준", "선발 계획", "유의사항", "사용설명서") if term in text)
                + 3 * ("2026" in text) + 2 * (entry.get("source_ext") == ".pdf")
                - 5 * any(word in text for word in ("동의서", "신청서", "양식", "설문", "포스터")))
    selected, seen = [], set()
    for entry in sorted((e for e in entries if e["family"] == family), key=lambda e: (-score(e), e["id"])):
        name = source_name(entry)
        title = str(entry.get("title") or "")
        key = "faculty_recruitment" if "교원" in title and "채용" in title else re.sub(r"\W|붙임\d*", "", name).lower()
        if not key or key in seen:
            continue
        seen.add(key)
        selected.append(entry)
        if len(selected) >= limit:
            break
    return selected


def validate_case(entry, rows, failures):
    hits = [r for r in rows if r.get("source_id") == entry["id"]]
    expected = dict(entry["chunks"])
    errors = list(failures)
    for row in hits:
        metadata = row.get("metadata") or {}
        if metadata.get("ingestion_dataset") != entry["dataset"]:
            errors.append("dataset_mismatch")
        if metadata.get("source_path") != entry["source_path"]:
            errors.append("original_path_mismatch")
        text_hash = hashlib.sha256(str(row.get("content") or "").encode("utf-8")).hexdigest()
        if text_hash != expected.get(row.get("chunk_index")):
            errors.append("chunk_position_or_body_hash_mismatch")
        uri = str(row.get("url") or metadata.get("doc_url") or "")
        if not uri.startswith(("http://", "https://")) or uri != entry.get("uri"):
            errors.append("citation_url_mismatch")
        title = str(metadata.get("doc_title") or row.get("title") or metadata.get("source_file") or "")
        if " ".join(title.split()) != " ".join(str(entry.get("title") or "").split()):
            errors.append("citation_title_mismatch")
    return hits, list(dict.fromkeys(errors))


def verification_status(tests, families):
    counts = Counter(t["family"] for t in tests if t["target_hit"] and not t["errors"])
    passed = bool(families) and not any(t["errors"] for t in tests) and all(counts[f] >= 1 for f in families)
    return ("passed" if passed else "failed"), dict(counts)


class RetrievalFailures(logging.Handler):
    def __init__(self):
        super().__init__(logging.WARNING)
        self.messages = []

    def emit(self, record):
        message = record.getMessage()
        if "failed" in message.lower() or "timeout" in message.lower():
            self.messages.append(message)


def verify(run, backend_root, case_limit):
    state = read(run / "receipt.json")
    entries = read(run / "missing_sources.json")
    progress = read(run / "load.json")
    identifiers = [e["id"] for e in entries]
    completed = progress.get("completed", []) + progress.get("concurrently_present", [])
    families = sorted({e["family"] for e in entries})
    if (not entries or len(set(identifiers)) != len(identifiers)
            or len(set(completed)) != len(completed) or set(completed) != set(identifiers)
            or not set(families).issubset(RPCS)):
        raise ValueError("Incomplete or invalid confirmed source/checkpoint selection")
    for family in families:
        check = state.get("verification", {}).get(family, {})
        if any(check.get(key) != "passed" for key in ("source_ids", "chunk_counts", "actual_sha256", "embedding_metadata")):
            raise ValueError("Complete the actual DB/body verification before search verification")
    if not (backend_root / "app/retrieval.py").is_file():
        raise ValueError("Select the installed backend directory containing app/retrieval.py")
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    sys.path.insert(0, str(backend_root.resolve()))
    import torch
    torch.set_num_threads(2)
    from app.config import get_settings
    from app.embeddings import Embedder
    from app.reranking import CrossEncoderReranker
    from app.retrieval import _fetch_rpc_responses, build_supabase_client, search
    from app.routers.ask import _row_to_source
    if ("metadata_filters" not in inspect.signature(_fetch_rpc_responses).parameters
            or "per_rpc_metadata_filters" not in inspect.signature(search).parameters):
        raise ValueError("Installed backend lacks scoped lexical forwarding; deploy the tested backend and migration 019 first")
    settings = get_settings()
    embedder = Embedder(model_name=settings.embedding_model, device="cpu", expected_dimensions=384)
    reranker = (CrossEncoderReranker(model_name=settings.reranker_model, device="cpu", quantize=settings.reranker_quantize)
                if settings.reranker_enabled else None)
    client = build_supabase_client(settings.supabase_url, settings.supabase_service_role_key)
    output = run / "search_verification.json"
    if output.exists():
        from datetime import datetime, timezone
        backup = run / ("search_before_retry_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f") + ".json")
        atomic(backup, read(output))
    receipt(run, status="searching", search_verified=False)
    tests = []
    for family in families:
        for entry in candidates(entries, family, case_limit):
            name, title = source_name(entry), str(entry.get("title") or "")
            question = (title + " / " + name if name != title else title) + " 내용을 알려줘"
            failures = RetrievalFailures()
            logger = logging.getLogger("app.retrieval")
            logger.addHandler(failures)
            started = time.monotonic()
            try:
                rows = search(client, rpc_names=[RPCS[family]], embedding=embedder.encode_query(question),
                    top_k=settings.rag_top_k, first_stage_k=settings.rag_first_stage_k,
                    rpc_concurrency=settings.rag_rpc_concurrency, min_similarity=settings.rag_min_similarity,
                    priority_weight=settings.rag_priority_weight, dataset_priority_weight=settings.rag_dataset_priority_weight,
                    source_kind_weight=settings.rag_source_kind_weight, reranker=reranker,
                    reranker_weight=settings.reranker_weight, max_chunks_per_url=settings.rag_max_chunks_per_url,
                    max_lexical_chunks_per_url=settings.rag_max_lexical_chunks_per_url,
                    query_text=question, metadata_filter={"ingestion_dataset": entry["dataset"]},
                    title_anchor=settings.rag_title_anchor_enabled,
                    scope_check=lambda row, ds=entry["dataset"]: (row.get("metadata") or {}).get("ingestion_dataset") == ds)
                hits, errors = validate_case(entry, rows, failures.messages)
            except Exception as error:
                hits, errors = [], [*failures.messages, f"{type(error).__name__}: {error}"]
            finally:
                logger.removeHandler(failures)
            case = {"family": family, "dataset": entry["dataset"], "expected_source_id": entry["id"], "question": question,
                    "target_hit": bool(hits), "errors": errors, "seconds": round(time.monotonic() - started, 2),
                    "citations": [_row_to_source(r).model_dump() for r in hits]}
            tests.append(case)
            atomic(output, {"status": "running", "tests": tests})
            print({k: case[k] for k in ("family", "dataset", "expected_source_id", "target_hit", "errors", "seconds")}, flush=True)
    status, successes = verification_status(tests, families)
    result = {"status": status, "tests": tests, "healthy_target_hits_by_family": successes,
              "target_misses": sum(not t["target_hit"] for t in tests), "backend_root": str(backend_root),
              "production_retrieval_sha256": digest(backend_root / "app/retrieval.py"),
              "scope": "Real models + Supabase + citation serialization; at least one healthy target hit per family; misses retained. Not corpus accuracy, answer generation, authenticated HTTP, or browser verification."}
    atomic(output, result)
    if status != "passed":
        raise ValueError("Search verification failed: inspect target misses and RPC/citation/body errors in search_verification.json")
    from datetime import datetime, timezone
    receipt(run, status="complete", completed_at=datetime.now(timezone.utc).isoformat(), search_verified=True, search_verification=result)


def main(default_run=None, argv=None):
    from scripts.recovery.cli import legacy_main
    return legacy_main("missing", argv, default_run=default_run, default_phase="search")


if __name__ == "__main__":
    raise SystemExit(main())
