"""Stage proven rule provenance repairs; update only reviewed IDs after other ingests finish."""
from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.preprocessing import rule
from scripts.recovery.common import inside as resolve_inside
from scripts.artifact_paths import reference_path
from scripts.preprocessing.rule_provenance import (
    POLICY, attachment_identity, duplicate_version, official_rule_url, version_state,
)
from scripts.preprocessing.repair_archive_collisions import (
    atomic, checked_documents, digest, event, read, receipt, require_finished,
    vectorize, publish as publish_artifacts,
)


def inside(path):
    return resolve_inside(path, root=ROOT)


def rel(path):
    return reference_path(inside(path), ROOT)


def prepare(run):
    if (run / "plan.json").exists():
        raise ValueError("Plan exists; reuse it or select a new --run directory")
    preprocessed = ROOT / "preprocessed/rule"
    originals = ROOT / "files/rule/output"
    indexed, attachments = rule.load_rule_json_index(originals / "json")
    observations = list(indexed.values())
    documents = {p: read(p) for p in preprocessed.rglob("*.json")}
    targets = {p for p, doc in documents.items() if not doc.get("provenance", {}).get("doc_url")}
    missing_count = len(targets)
    # Include the JSON representations of the same historical HTML documents;
    # otherwise the old rules would still be retrieved from those copies.
    for path in list(targets):
        source = inside(documents[path]["source_path"])
        if source.suffix.lower() == ".html":
            paired = preprocessed / "json" / source.relative_to(originals / "html").with_suffix(".json")
            if paired in documents:
                targets.add(paired)
    staged, unresolved, evidence = [], [], set()
    for target in sorted(targets):
        document = documents[target]
        source = inside(document["source_path"])
        if source.suffix.lower() in {".html", ".json"}:
            recovered = indexed.get(f"{source.parent.name}/{source.stem}", {})
        else:
            recovered = attachments.get(source.as_posix().lower(), {})
        if not recovered.get("doc_title") or not official_rule_url(recovered.get("doc_url")):
            unresolved.append({"path": rel(target), "reason": "No unambiguous recorded official title/URL"})
            continue
        provenance = {**document.get("provenance", {}), **rule.public_provenance(recovered)}
        if source.suffix.lower() not in {".html", ".json"}:
            if not provenance.get("attachment_sha256") or digest(source) != provenance["attachment_sha256"]:
                raise ValueError(f"Attachment hash does not match its recorded parent: {source}")
            provenance = attachment_identity(provenance, document)
            provenance["applicability_note"] = "시행일 미확인. 게시글·개정일·파일명 날짜를 시행일로 단정하지 마세요. " + provenance["rule_effective_note"]
        elif provenance.get("rule_document_id"):
            provenance.update(version_state(provenance, observations))
            provenance["applicability_note"] = ("과거 버전 원문. 시행일은 확인됐으나 적용 종료일은 미확인입니다."
                                                  if provenance["rule_version_state"] == "historical"
                                                  else "수집 이력에서 확인한 버전이며 현재 유효성 전수 검증은 별도입니다.")
        provenance["rule_provenance_policy"] = POLICY
        provenance["rule_repair_run"] = rel(run)
        document = {**document, "provenance": provenance}
        destination = run / "preprocessed/rule" / target.relative_to(preprocessed)
        atomic(destination, document)
        status = "inactive" if provenance.get("rule_version_state") == "historical" else "active"
        entry = {"dataset": "rule", "target": rel(target), "staged": rel(destination), "sha256": digest(destination),
                 "slug": document["slug"], "source_path": document["source_path"], "desired_status": status,
                 "previous": [{"path": rel(target), "sha256": digest(target)}]}
        staged.append(entry)
        evidence.add(source)
        for key in ("source_json_path", "rule_identity_evidence_path"):
            if provenance.get(key):
                evidence.add(inside(provenance[key]))
        if provenance.get("rule_version_state") == "historical":
            for observation in observations:
                if (observation.get("rule_document_id") == provenance.get("rule_document_id")
                        and observation.get("rule_version_id") == provenance.get("rule_newer_observed_version")):
                    for key in ("source_json_path", "rule_identity_evidence_path"):
                        if observation.get(key):
                            evidence.add(inside(observation[key]))
    html_entries = [e for e in staged if inside(e["source_path"]).suffix.lower() == ".html"]
    for position, entry in enumerate(html_entries):
        document = read(inside(entry["staged"]))
        for previous in html_entries[:position]:
            primary = read(inside(previous["staged"]))
            if duplicate_version(document["provenance"], primary["provenance"]):
                document["provenance"]["rule_duplicate_of"] = previous["source_path"]
                primary["provenance"]["rule_source_aliases"] = sorted(set(primary["provenance"].get("rule_source_aliases", []))
                    | set(document["provenance"].get("rule_source_aliases", [])))
                atomic(inside(previous["staged"]), primary)
                previous["sha256"] = digest(inside(previous["staged"]))
                entry["desired_status"] = "inactive"
                atomic(inside(entry["staged"]), document)
                entry["sha256"] = digest(inside(entry["staged"]))
                break
    plan = {"policy": POLICY, "members": staged, "unresolved": unresolved,
            "archives": [{"path": rel(path), "sha256": digest(path)} for path in sorted(evidence)]}
    atomic(run / "plan.json", plan)
    atomic(run / "documents.json", staged)
    receipt(run, status="prepared", missing_url_sources=missing_count, selected_sources=len(staged),
            unresolved=unresolved, vectorization_complete=False, database_verified=False)
    event("prepared", missing_url_sources=missing_count, selected_sources=len(staged), unresolved=unresolved)
    if unresolved:
        raise ValueError("Some official URLs/titles are unconfirmed; no guessed values were staged")


def check_evidence(run):
    plan = read(run / "plan.json")
    if plan.get("policy") != POLICY or plan.get("unresolved"):
        raise ValueError("Resolve the provenance plan before applying")
    for source in plan["archives"]:
        if digest(inside(source["path"])) != source["sha256"]:
            raise ValueError(f"Original evidence changed: {source['path']}")


def apply(run, after):
    require_finished(after)
    check_evidence(run)
    if not read(run / "receipt.json").get("vectorization_complete"):
        raise ValueError("Complete vectorization before applying")
    from psycopg.errors import QueryCanceled, LockNotAvailable, DeadlockDetected
    from scripts.db import connect_postgres
    from scripts.vectorization.recovery_control import write_chunk_batches
    from scripts.vectorization.load_to_supabase import DEFAULT_EMBEDDING_MODEL, SOURCE_COLUMNS, CHUNK_COLUMNS, copy_upsert, prepare_row, preflight_index
    docs = checked_documents(run)
    bodies = {doc["source_path"]: read(inside(doc["staged"])) for doc in docs}
    preflight_index(run / "vectorized/rule/index.jsonl", "rule", body_documents=bodies)
    receipt(run, database_verified=False)
    for doc in docs:
        if "vector" not in doc:
            raise ValueError(f"No searchable chunks for repaired rule: {doc['source_path']}")
        rows = [prepare_row(r, DEFAULT_EMBEDDING_MODEL, "rule", body_document=bodies[doc["source_path"]]) for r in read(inside(doc["vector"]))["records"]]
        source = rows[0]["source"]
        with connect_postgres(autocommit=True) as conn:
            with conn.transaction(), conn.cursor() as cur:
                cur.execute("set local statement_timeout='600s'")
                cur.execute("set local lock_timeout='10s'")
                cur.execute("select id,file_path,metadata->>'ingestion_dataset',status from public.rule_sources where id=%s or file_path=%s for update",
                            (source["id"], doc["source_path"]))
                existing = cur.fetchall()
                if len(existing) != 1 or existing[0][:3] != (source["id"], doc["source_path"], "rule"):
                    raise ValueError(f"Existing rule source identity is absent or ambiguous: {doc['source_path']}")
                if doc["desired_status"] == "active" and existing[0][3] != "active":
                    raise ValueError("Refusing to reactivate an already excluded rule source without reviewing its status")
                copy_upsert(cur, "rule_sources", "rule_repair_sources", SOURCE_COLUMNS, [source], ("id",), "processed_at")
                cur.execute("delete from public.rule_chunks where source_id=%s", (source["id"],))
                write_chunk_batches([r["chunk"] for r in rows], batch_size=32, max_retries=2, savepoint=conn.transaction,
                    write=lambda part: copy_upsert(cur, "rule_chunks", "rule_repair_chunks", CHUNK_COLUMNS, part, ("source_id", "chunk_index"), "embedded_at"),
                    retryable=(QueryCanceled, LockNotAvailable, DeadlockDetected),
                    on_retry=lambda error, attempt, size: event("retry", source_id=source["id"], attempt=attempt, size=size))
                cur.execute("update public.rule_sources set status=%s where id=%s", (doc["desired_status"], source["id"]))
        event("rule_committed", source_id=source["id"], status=doc["desired_status"], chunks=len(rows))
    receipt(run, status="loaded")
    verify_database(run)


def verify_database(run):
    if not read(run / "receipt.json").get("vectorization_complete"):
        raise ValueError("Complete vectorization before database verification")
    from scripts.db import connect_postgres
    from scripts.vectorization.load_to_supabase import DEFAULT_EMBEDDING_MODEL, prepare_row
    docs = checked_documents(run)
    receipt(run, database_verified=False)
    total = 0
    with connect_postgres() as conn:
        conn.execute("set transaction isolation level repeatable read read only")
        for doc in docs:
            body = read(inside(doc["staged"]))
            rows = [prepare_row(r, DEFAULT_EMBEDDING_MODEL, "rule", body_document=body) for r in read(inside(doc["vector"]))["records"]]
            expected = rows[0]["source"]
            observed = conn.execute("select title,url,file_path,status,metadata from public.rule_sources where id=%s", (expected["id"],)).fetchone()
            if observed is None or observed[:4] != (expected["title"], expected["url"], doc["source_path"], doc["desired_status"]):
                raise ValueError(f"Rule title/URL/identity/status mismatch: {doc['source_path']}")
            provenance = read(inside(doc["staged"]))["provenance"]
            for key in ("effective_at", "rule_document_id", "rule_version_id", "rule_duplicate_of", "rule_valid_until",
                        "rule_effective_status", "rule_version_state", "applicability_note", "rule_filename_date_conflict"):
                if observed[4].get(key) != provenance.get(key):
                    raise ValueError(f"Rule date/version metadata mismatch: {key}")
            actual = conn.execute("select chunk_index,content_hash,encode(sha256(convert_to(content,'UTF8')),'hex'),embedding_model,embedding_dim,embedding is not null from public.rule_chunks where source_id=%s", (expected["id"],)).fetchall()
            hashes = {i: stored for i, stored, computed, model, dim, vector in actual if stored == computed and model == DEFAULT_EMBEDDING_MODEL and dim == 384 and vector}
            wanted = {r["chunk"]["chunk_index"]: r["chunk"]["content_hash"] for r in rows}
            if len(actual) != len(wanted) or hashes != wanted:
                raise ValueError("Rule chunk count/hash/embedding mismatch")
            total += len(rows)
    receipt(run, status="database_verified", database_verified=True, verified_sources=len(docs), verified_chunks=total)
    event("database_verified", sources=len(docs), chunks=total)


def verify_search(run, backend_root):
    """Real rule retrieval plus the same source-card serializer used by /ask."""
    if not read(run / "receipt.json").get("artifacts_published"):
        raise ValueError("Publish the reviewed artifacts before the final search verification")
    verify_database(run)
    import os
    import logging
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
    sys.path.insert(0, str(backend_root.resolve()))
    from app.config import get_settings
    from app.embeddings import Embedder
    from app.reranking import CrossEncoderReranker
    from app.retrieval import build_supabase_client, search
    from app.routers.ask import _row_to_source
    from scripts.vectorization.load_to_supabase import record_identity
    settings = get_settings()
    embedder = Embedder(model_name=settings.embedding_model, device="cpu", expected_dimensions=384)
    reranker = CrossEncoderReranker(model_name=settings.reranker_model, device="cpu", quantize=settings.reranker_quantize) if settings.reranker_enabled else None
    client = build_supabase_client(settings.supabase_url, settings.supabase_service_role_key)
    docs = checked_documents(run)
    hidden = {record_identity({"source_slug": d["slug"], "chunk_index": 0}, "rule")[0] for d in docs if d["desired_status"] == "inactive"}
    active = [d for d in docs if d["desired_status"] == "active"]
    # Check the public citation page and the actual attached bytes, not merely
    # whether retrieval returned the URL string written by the repair.
    import requests
    from bs4 import BeautifulSoup
    from scripts.preprocessing.repair_attachment_formats import fetch_file, public_page_error
    from scripts.crawlers.departments.attachments import html_download_error
    official_sources = []
    with requests.Session() as session:
        session.trust_env = False
        for target in active:
            provenance = read(inside(target["staged"]))["provenance"]
            folder = run / "citation_sources" / target["slug"]
            page = folder / "source.html"
            evidence = fetch_file(session, provenance["doc_url"], page, source_page=True)
            if public_page_error(page.read_bytes(), evidence["final_url"]):
                raise ValueError("Citation URL is an error/login page")
            soup = BeautifulSoup(page.read_bytes(), "html.parser")
            expected_title = "".join(provenance["doc_title"].split())
            visible_text = "".join(soup.get_text(" ", strip=True).split())
            if not expected_title or expected_title not in visible_text:
                raise ValueError("Current official page title differs from the recorded source; review required")
            if provenance.get("attachment_url"):
                attachment = folder / "original.pdf"
                download = fetch_file(session, provenance["attachment_url"], attachment)
                if digest(attachment) != provenance.get("attachment_sha256"):
                    raise ValueError("Official attachment bytes differ from the recorded original; review required")
                evidence.update(attachment=download, attachment_sha256=digest(attachment))
            official_sources.append(evidence)
    questions = [("부경대학교 일반휴학 기간과 전과 신청 자격", None)]
    questions.extend((read(inside(d["staged"]))["provenance"]["doc_title"] + " 운영 기준과 교육과정 안내", d) for d in active)
    cases = []
    class RetrievalFailures(logging.Handler):
        def __init__(self):
            super().__init__(level=logging.WARNING)
            self.messages = []
        def emit(self, record):
            if "failed" in record.getMessage().lower():
                self.messages.append(record.getMessage())
    for question, target in questions:
        failures = RetrievalFailures()
        logger = logging.getLogger("app.retrieval")
        logger.addHandler(failures)
        try:
            rows = search(client, rpc_names=["match_rule_documents"], embedding=embedder.encode_query(question),
                top_k=settings.rag_top_k, first_stage_k=settings.rag_first_stage_k, min_similarity=settings.rag_min_similarity,
                priority_weight=settings.rag_priority_weight, dataset_priority_weight=settings.rag_dataset_priority_weight,
                source_kind_weight=settings.rag_source_kind_weight, reranker=reranker, reranker_weight=settings.reranker_weight,
                max_chunks_per_url=settings.rag_max_chunks_per_url, query_text=question,
                metadata_filter={"ingestion_dataset": "rule"}, title_anchor=settings.rag_title_anchor_enabled)
        finally:
            logger.removeHandler(failures)
        if failures.messages:
            atomic(run / "search_verification.json", {"status": "failed", "question": question, "retrieval_errors": failures.messages})
            raise ValueError("Search had internal failures; partial results are not verification success")
        if not rows or hidden.intersection(r.get("source_id") for r in rows):
            raise ValueError("Rule search is empty or exposes a historical/duplicate repaired source")
        if target:
            sid = record_identity({"source_slug": target["slug"], "chunk_index": 0}, "rule")[0]
            hits = [r for r in rows if r.get("source_id") == sid]
            if not hits:
                raise ValueError("Repaired rule was not found through the real search path")
            provenance = read(inside(target["staged"]))["provenance"]
            for hit in hits:
                card = _row_to_source(hit)
                if card.uri != provenance["doc_url"] or card.title != provenance["doc_title"] or not card.content:
                    raise ValueError("Citation card does not match the repaired official source")
                if hit.get("metadata", {}).get("effective_at") != provenance.get("effective_at"):
                    raise ValueError("Search inferred an unconfirmed effective date")
        cases.append({"question": question, "citations": [_row_to_source(r).model_dump() for r in rows]})
    atomic(run / "search_verification.json", {"status": "passed", "cases": cases, "official_sources": official_sources, "backend_root": str(backend_root),
        "scope": "Real Supabase rule search + /ask source-card serialization; generated answers/browser rendering not tested"})
    receipt(run, status="complete", search_verified=True)
    event("search_and_citations_verified", cases=len(cases))


def main(argv=None):
    from scripts.recovery.cli import legacy_main
    return legacy_main("rules", argv)


if __name__ == "__main__":
    raise SystemExit(main())
