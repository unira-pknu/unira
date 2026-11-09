"""Publish verified format/quality repairs with frozen originals and scoped DB backups.

All execution is explicit through CLI phases. No jobs or connections run on import.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.preprocessing.repair_archive_collisions import (
    atomic, checked_documents, digest, event, inside, read, receipt, relative,
    require_finished, vectorize, publish as publish_artifacts,
)
from scripts.vectorization.reconcile_ce import fingerprint, vector_key
from scripts.vectorization.recovery_control import write_chunk_batches

POLICY = "scoped-recovery-publication-v1"


def canonical_target(dataset, source_path):
    """Use the ordinary collector layout; do not create a second source namespace."""
    from scripts.preprocessing.collected import scopes
    from scripts.preprocessing.file_preprocessing import ensure_output_path, archive_member_output_path
    scope = next((s for name, s, _ in scopes(ROOT) if name == dataset), None)
    if scope is None:
        raise ValueError("Dataset has no collected source scope")
    raw, separator, member = source_path.partition("!/")
    original = inside(raw, scope)
    parts = original.relative_to(scope).parts
    if "files" not in parts:
        raise ValueError("Recovered attachment is outside the collected files tree")
    index = parts.index("files")
    input_root = scope.joinpath(*parts[:index + 1])
    output_root = ROOT / "preprocessed" / dataset / "files" / input_root.parent.relative_to(scope)
    if separator:
        return archive_member_output_path(original, input_root, output_root, member)
    return ensure_output_path(original, input_root, output_root)


def recovery_candidates(source_run, kind):
    deferred, candidates = [], []
    if kind == "formats":
        from scripts.preprocessing.repair_attachment_formats import successful_result_is_current
        results = read(source_run / "results.json")
        if results.get("status") != "complete":
            raise ValueError("Finish attachment extraction before selecting publication candidates")
        for item in results["results"]:
            if item["status"] != "processed":
                deferred.append({"source_path": item["source_path"], "reason": item.get("reason") or item.get("error") or item["status"]})
                continue
            if not successful_result_is_current(item, item, source_run):
                raise ValueError("A previously successful format repair no longer matches its original/artifacts")
            for declared in item["documents"]:
                staged = inside(declared["preprocessed_path"], source_run)
                document = read(staged)
                provenance = document.get("provenance", {})
                if provenance.get("requires_source_review") or provenance.get("ocr_review_required"):
                    deferred.append({"source_path": document["source_path"], "reason": "original_or_visual_review_required"})
                    continue
                candidates.append({"dataset": item["dataset"], "document": document, "source_artifact": relative(staged),
                                   "raw_path": item["source_path"], "raw_sha256": item["original_sha256"]})
    else:
        state = read(source_run / "receipt.json")
        if state.get("status") != "staged_pending_scoped_sync":
            raise ValueError("Finish the original quality review and stage phase first")
        if digest(source_run / "decisions.json") != state.get("decisions_sha256"):
            raise ValueError("Quality review decisions changed after staging")
        plan = read(source_run / "plan.json")
        by_id = {i["id"]: i for i in plan["items"]}
        for item in read(source_run / "documents.json"):
            if item["action"] == "retain":
                deferred.append({"id": item["id"], "reason": "original_preserved_by_review"})
                continue
            staged = inside(item["staged"], source_run)
            if digest(staged) != item["sha256"]:
                raise ValueError("Reviewed quality artifact changed")
            original = by_id[item["id"]]
            candidates.append({"dataset": item["dataset"], "document": read(staged), "source_artifact": relative(staged),
                               "raw_path": original["raw_path"], "raw_sha256": original["raw_sha256"],
                               "target": item["target"], "prior_sha256": item["prior_sha256"]})
    return candidates, deferred


def snapshot(conn, family, sid, *, lock=False):
    from psycopg import sql
    suffix = sql.SQL(" for update") if lock else sql.SQL("")
    source = conn.execute(sql.SQL("select to_jsonb(s) from {} s where id=%s").format(
        sql.Identifier("public", family + "_sources")) + suffix, (sid,)).fetchone()
    chunks = conn.execute(sql.SQL("select (to_jsonb(c)-'content_tsv'-'embedding') || jsonb_build_object('embedding',c.embedding::text) "
                                 "from {} c where source_id=%s order by chunk_index").format(
        sql.Identifier("public", family + "_chunks")) + suffix, (sid,)).fetchall()
    return {"source": source[0] if source else None, "chunks": [r[0] for r in chunks]}


def check_path_identity(conn, family, sid, dataset, path, *, lock=False):
    from psycopg import sql
    query = sql.SQL("select id,file_path,metadata->>'ingestion_dataset' from {} where id=%s or file_path=%s").format(
        sql.Identifier("public", family + "_sources"))
    if lock:
        query += sql.SQL(" for update")
    rows = conn.execute(query, (sid, path)).fetchall()
    if any(row != (sid, path, dataset) for row in rows):
        raise ValueError("Another source identity already uses this original path; review aliases first")


def validate_existing(existing, dataset, source_path, local_vector):
    source = existing["source"]
    if source is None:
        if existing["chunks"]:
            raise ValueError("Orphan database chunks require separate review")
        return
    if source["file_path"] != source_path or source.get("metadata", {}).get("ingestion_dataset") != dataset:
        raise ValueError("Existing DB source belongs to another path/dataset")
    if source.get("status") != "active":
        raise ValueError("Preserve an existing inactive source until its exclusion is reviewed")
    if not local_vector.is_file():
        raise ValueError("No local vector baseline for an existing DB source")
    local = read(local_vector)["records"]
    expected = {r["chunk_index"]: (r["text"].replace("\x00", ""), vector_key(r["embedding"])) for r in local}
    actual = {r["chunk_index"]: (r["content"], vector_key(json.loads(r["embedding"]))) for r in existing["chunks"]}
    if len(expected) != len(local) or len(actual) != len(existing["chunks"]) or actual != expected:
        raise ValueError("DB differs from the local baseline; preserve its newer/different version")


def prepare(run, source_run, kind, after):
    # Isolated preparation only reads DB snapshots. Writes/publication still
    # require every earlier job to complete and the snapshots to stay identical.
    if not after:
        raise ValueError("Declare earlier ingestion receipts with --after-receipt")
    dependencies = [{"path": relative(inside(p)), "status": read(inside(p)).get("status")} for p in after]
    if (run / "plan.json").exists():
        raise ValueError("Publication plan already exists; resume it rather than overwriting evidence")
    from scripts.db import connect_postgres
    from scripts.vectorization.datasets import table_family
    from scripts.vectorization.load_to_supabase import record_identity
    candidates, deferred = recovery_candidates(source_run, kind)
    documents, originals, identities = [], {}, set()
    with connect_postgres() as conn:
        conn.execute("set transaction isolation level repeatable read read only")
        conn.execute("set local statement_timeout='60s'")
        for item in candidates:
            document, dataset = item["document"], item["dataset"]
            source_path = document["source_path"]
            try:
                raw = inside(item["raw_path"])
                if not item["raw_sha256"] or digest(raw) != item["raw_sha256"]:
                    raise ValueError("Original bytes changed or are missing")
                target = inside(item["target"]) if item.get("target") else canonical_target(dataset, source_path)
                old = read(target) if target.exists() else None
                if item.get("prior_sha256") and (not old or digest(target) != item["prior_sha256"]):
                    raise ValueError("Quality baseline changed after review")
                if old and old.get("source_path") != source_path:
                    raise ValueError("Canonical target is occupied by another original")
                document["slug"] = old["slug"] if old else document["slug"]
                provenance = {**(old or {}).get("provenance", {}),
                              **{k: v for k, v in document.get("provenance", {}).items() if v is not None and v != ""}}
                document["provenance"] = provenance
                if provenance.get("actual_file_format") in {"xlsx", "xls"}:
                    from scripts.preprocessing.file_preprocessing import SPREADSHEET_POLICY_VERSION
                    provenance["spreadsheet_policy_version"] = SPREADSHEET_POLICY_VERSION
                sid, _ = record_identity({"source_slug": document["slug"], "chunk_index": 0}, dataset)
                if sid in identities:
                    raise ValueError("Duplicate recovered source identity")
                family = table_family(dataset, provenance)
                check_path_identity(conn, family, sid, dataset, source_path)
                prior = snapshot(conn, family, sid)
                vector = ROOT / "vectorized" / dataset / target.relative_to(ROOT / "preprocessed" / dataset)
                validate_existing(prior, dataset, source_path, vector)
                if prior["source"]:
                    current = prior["source"]
                    known_uri = current.get("url") or current.get("parent_url")
                    recovered_uri = provenance.get("doc_url") or provenance.get("source_page_url") or provenance.get("attachment_url")
                    if known_uri and recovered_uri and known_uri != recovered_uri:
                        raise ValueError("Restored citation conflicts with current DB; review both originals")
                    if current.get("title"):
                        provenance["doc_title"] = current["title"]
                    if known_uri:
                        provenance["doc_url"] = known_uri
                if not str(provenance.get("doc_url") or provenance.get("attachment_url") or "").startswith(("https://", "http://")):
                    raise ValueError("No verified citation URL for this recovered attachment")
                from scripts.vectorization.vectorization import extract_chunk_records
                if not extract_chunk_records(document, target, ROOT):
                    explicit_exclusion = kind == "quality" and provenance.get("text_quality_review", {}).get("action") == "exclude_chunks"
                    if not explicit_exclusion or not prior["source"]:
                        raise ValueError("Recovered text has no searchable chunks; preserve the existing version")
                destination = run / "preprocessed" / dataset / target.relative_to(ROOT / "preprocessed" / dataset)
                atomic(destination, document)
                baseline = run / "database" / (sid + ".json")
                atomic(baseline, prior)
                documents.append({"id": sid, "dataset": dataset, "family": family, "slug": document["slug"], "source_path": source_path,
                    "target": relative(target), "staged": relative(destination), "sha256": digest(destination),
                    "source_artifact": item["source_artifact"], "source_artifact_sha256": digest(inside(item["source_artifact"])),
                    "snapshot": relative(baseline), "snapshot_sha256": digest(baseline), "db_fingerprint": fingerprint(prior),
                    "previous": [{"path": relative(target), "sha256": digest(target)}] if old else []})
                originals[relative(raw)] = item["raw_sha256"]
                identities.add(sid)
            except ValueError as error:
                deferred.append({"dataset": dataset, "source_path": source_path, "reason": str(error)})
    plan = {"policy": POLICY, "source_run": relative(source_run), "kind": kind, "preparation_dependencies": dependencies,
            "archives": [{"path": p, "sha256": h} for p, h in originals.items()], "deferred": deferred}
    atomic(run / "documents.json", documents)
    atomic(run / "plan.json", plan)
    receipt(run, status="prepared", plan_sha256=digest(run / "plan.json"), selected_sources=len(documents), deferred_sources=len(deferred))
    event("publication_prepared", selected=len(documents), deferred=len(deferred))


def checked(run):
    plan = read(run / "plan.json")
    if plan.get("policy") != POLICY or digest(run / "plan.json") != read(run / "receipt.json").get("plan_sha256"):
        raise ValueError("Publication plan changed")
    for original in plan["archives"]:
        if digest(inside(original["path"])) != original["sha256"]:
            raise ValueError("Publication original changed")
    docs = checked_documents(run)
    for doc in docs:
        if digest(inside(doc["snapshot"], run)) != doc["snapshot_sha256"]:
            raise ValueError("DB backup changed")
        if digest(inside(doc["source_artifact"])) != doc["source_artifact_sha256"]:
            raise ValueError("Original recovery artifact changed")
    return plan, docs


def expected_rows(run, doc):
    from scripts.vectorization.load_to_supabase import prepare_row, DEFAULT_EMBEDDING_MODEL
    body = read(inside(doc["staged"], run))
    records = read(inside(doc["vector"], run))["records"] if "vector" in doc else []
    return [prepare_row(r, DEFAULT_EMBEDDING_MODEL, doc["dataset"], body_document=body) for r in records]


def verify_one(conn, run, doc):
    from scripts.vectorization.load_to_supabase import sha256_text, DEFAULT_EMBEDDING_MODEL
    current = snapshot(conn, doc["family"], doc["id"])
    rows = expected_rows(run, doc)
    source = current["source"]
    if not source or source["file_path"] != doc["source_path"] or source["metadata"].get("ingestion_dataset") != doc["dataset"]:
        raise ValueError("Published source provenance differs")
    if not rows:
        if source["status"] != "inactive" or current["chunks"] != read(inside(doc["snapshot"], run))["chunks"]:
            raise ValueError("Empty reviewed extraction did not preserve/exclude the original chunks")
        return
    expected_source = rows[0]["source"]
    if source["status"] != "active" or (source["title"], source["url"]) != (expected_source["title"], expected_source["url"]):
        raise ValueError("Published source status/citation differs")
    actual = {c["chunk_index"]: (c["content"], c["content_hash"], vector_key(json.loads(c["embedding"])))
              for c in current["chunks"] if c["content_hash"] == sha256_text(c["content"])
              and c["embedding_model"] == DEFAULT_EMBEDDING_MODEL and c["embedding_dim"] == 384}
    expected = {r["chunk"]["chunk_index"]: (r["chunk"]["content"], r["chunk"]["content_hash"], vector_key(json.loads(r["chunk"]["embedding"]))) for r in rows}
    if len(actual) != len(current["chunks"]) or actual != expected:
        raise ValueError("Published chunk position/body hash/model/vector differs")


def apply(run, after):
    require_finished(after)
    _, docs = checked(run)
    if not read(run / "receipt.json").get("vectorization_complete"):
        raise ValueError("Vectorize selected repairs before DB changes")
    from scripts.db import connect_postgres
    from psycopg import sql
    from psycopg.errors import QueryCanceled, LockNotAvailable, DeadlockDetected
    from scripts.vectorization.load_to_supabase import SOURCE_COLUMNS, DEPARTMENT_COLUMNS, CHUNK_COLUMNS, copy_upsert
    for doc in docs:
        rows = expected_rows(run, doc)
        with connect_postgres(autocommit=True) as conn, conn.transaction():
            conn.execute("set local statement_timeout='120s'")
            conn.execute("set local lock_timeout='10s'")
            conn.execute("select pg_advisory_xact_lock(hashtext(%s))", (doc["id"],))
            check_path_identity(conn, doc["family"], doc["id"], doc["dataset"], doc["source_path"], lock=True)
            current = snapshot(conn, doc["family"], doc["id"], lock=True)
            if fingerprint(current) != doc["db_fingerprint"]:
                verify_one(conn, run, doc)  # A previous attempt committed this exact selection.
                continue
            with conn.cursor() as cur:
                if rows:
                    columns = SOURCE_COLUMNS + DEPARTMENT_COLUMNS if doc["family"] == "rag" else SOURCE_COLUMNS
                    copy_upsert(cur, doc["family"] + "_sources", "repair_publish_sources", columns, [rows[0]["source"]], ("id",), "processed_at")
                    cur.execute(sql.SQL("delete from {} where source_id=%s").format(sql.Identifier("public", doc["family"] + "_chunks")), (doc["id"],))
                    write_chunk_batches([r["chunk"] for r in rows], batch_size=32, max_retries=2, savepoint=conn.transaction,
                        write=lambda part: copy_upsert(cur, doc["family"] + "_chunks", "repair_publish_chunks", CHUNK_COLUMNS, part, ("source_id", "chunk_index"), "embedded_at"),
                        retryable=(QueryCanceled, LockNotAvailable, DeadlockDetected), on_retry=lambda *args: event("publication_retry", id=doc["id"]))
                elif current["source"]:
                    cur.execute(sql.SQL("update {} set status='inactive' where id=%s").format(sql.Identifier("public", doc["family"] + "_sources")), (doc["id"],))
                else:
                    raise ValueError("No searchable recovered content and no existing source to preserve")
            verify_one(conn, run, doc)
        event("publication_committed", id=doc["id"], chunks=len(rows))
    verify_database(run)


def verify_database(run):
    from scripts.db import connect_postgres
    _, docs = checked(run)
    with connect_postgres() as conn:
        conn.execute("set transaction isolation level repeatable read read only")
        conn.execute("set local statement_timeout='120s'")
        for doc in docs:
            verify_one(conn, run, doc)
    receipt(run, status="verified", database_verified=True)


def verify_search(run, backend_root):
    import logging
    import os
    if not read(run / "receipt.json").get("artifacts_published"):
        raise ValueError("Publish verified artifacts before checking search")
    if not (backend_root / "app/retrieval.py").is_file():
        raise ValueError("Select the installed backend directory")
    sys.path.insert(0, str(backend_root.resolve()))
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    import torch
    torch.set_num_threads(2)
    from app.config import get_settings
    from app.embeddings import Embedder
    from app.reranking import CrossEncoderReranker
    from app.retrieval import build_supabase_client, search
    from app.routers.ask import _row_to_source
    from scripts.vectorization.verify_recovery_search import RetrievalFailures
    settings = get_settings()  # Freeze installed settings before workspace DB dotenv loading.
    verify_database(run)
    plan, docs = checked(run)
    if not docs:
        raise ValueError("No selected repairs to verify")
    previous_search = run / "search_verification.json"
    if previous_search.exists():
        atomic(run / ("search_before_retry_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f") + ".json"),
               read(previous_search))
    embedder = Embedder(model_name=settings.embedding_model, device="cpu", expected_dimensions=384)
    reranker = (CrossEncoderReranker(model_name=settings.reranker_model, device="cpu", quantize=settings.reranker_quantize)
                if settings.reranker_enabled else None)
    client = build_supabase_client(settings.supabase_url, settings.supabase_service_role_key)
    tests = []
    for doc in docs:
        if digest(inside(doc["target"])) != doc["sha256"]:
            raise ValueError("Published preprocessing differs from the approved recovery")
        expected = expected_rows(run, doc)
        baseline = read(inside(doc["snapshot"], run))
        source = expected[0]["source"] if expected else baseline["source"]
        question = str(source.get("title") or Path(doc["source_path"]).name) + " 내용을 알려줘"
        failures = RetrievalFailures()
        logger = logging.getLogger("app.retrieval")
        logger.addHandler(failures)
        try:
            # Check exact identity routing in the same RPC as production. A separate
            # missing-ingest smoke check covers unscoped natural retrieval.
            rows = search(client, rpc_names=["match_" + doc["family"] + "_documents"],
                          embedding=embedder.encode_query(question), query_text=question,
                          top_k=settings.rag_top_k, first_stage_k=settings.rag_first_stage_k,
                          rpc_concurrency=settings.rag_rpc_concurrency, min_similarity=settings.rag_min_similarity,
                          priority_weight=settings.rag_priority_weight, dataset_priority_weight=settings.rag_dataset_priority_weight,
                          source_kind_weight=settings.rag_source_kind_weight, max_chunks_per_url=settings.rag_max_chunks_per_url,
                          max_lexical_chunks_per_url=settings.rag_max_lexical_chunks_per_url,
                          reranker=reranker, reranker_weight=settings.reranker_weight,
                          title_anchor=settings.rag_title_anchor_enabled,
                          metadata_filter={"ingestion_dataset": doc["dataset"], "original_source_slug": doc["slug"]},
                          scope_check=lambda row, sid=doc["id"]: row.get("source_id") == sid)
        finally:
            logger.removeHandler(failures)
        hits = [r for r in rows if r.get("source_id") == doc["id"]]
        errors = list(failures.messages)
        if bool(hits) != bool(expected):
            errors.append("recovered_source_missing_or_excluded_source_still_searchable")
        by_position = {r["chunk"]["chunk_index"]: r["chunk"]["content"] for r in expected}
        citations = []
        for hit in hits:
            card = _row_to_source(hit).model_dump()
            if (hit.get("content") != by_position.get(hit.get("chunk_index"))
                    or card["uri"] != source.get("url") or card["title"] != source.get("title")):
                errors.append("returned_body_or_citation_differs")
            citations.append(card)
        test = {"id": doc["id"], "dataset": doc["dataset"], "expected_active": bool(expected), "target_hits": len(hits),
                "question": question, "errors": errors, "citations": citations}
        tests.append(test)
        atomic(run / "search_verification.json", {"status": "running", "tests": tests})
        event("publication_search", id=doc["id"], hits=len(hits), errors=errors)
    status = "failed" if any(t["errors"] for t in tests) else "passed"
    result = {"status": status, "tests": tests, "backend_root": str(backend_root),
              "retrieval_sha256": digest(backend_root / "app/retrieval.py"),
              "scope": "Real DB hashes/vectors, canonical files, exact-source metadata-scoped retrieval and citations; no generated answer or browser verification"}
    atomic(run / "search_verification.json", result)
    if status != "passed":
        raise ValueError("Recovered-source search/citation verification failed")
    receipt(run, status="complete", database_verified=True, search_verified=True, selected_sources=len(docs),
            deferred_sources=len(plan["deferred"]), completed_at=datetime.now(timezone.utc).isoformat())


def main(argv=None):
    from scripts.recovery.cli import legacy_main
    return legacy_main("publish", argv)


if __name__ == "__main__":
    raise SystemExit(main())
