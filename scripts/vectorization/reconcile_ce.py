"""Compare CE conflicts against originals, preserve DB versions, and synchronize reviewed IDs.

No jobs, models, or database connections run on import. Commands are operator-run.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone, timedelta
import difflib
import hashlib
import json
import os
from pathlib import Path
import shutil
import struct
import sys

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.recovery.common import (
    atomic, digest, event as emit, read, receipt, inside as resolve_inside,
)
from scripts.artifact_paths import reference_path

POLICY = "ce-original-reconciliation-v1"


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def vector_key(values):
    """pgvector stores float32; compare those values rather than Python float64 spelling."""
    return struct.pack(f"<{len(values)}f", *values)


def inside(path, boundary=None):
    return resolve_inside(path, root=ROOT, boundary=boundary)


def rel(path):
    return reference_path(inside(path), ROOT)


def body_text(content, metadata):
    """Strip only the exact, known embedding prefix; never split at arbitrary '본문:'."""
    from scripts.vectorization.vectorization import build_context_prefix
    prefix = build_context_prefix(metadata, metadata.get("source_file", ""))
    marker = prefix + "\n본문: "
    return content[len(marker):] if prefix and content.startswith(marker) else content


def quality(text):
    from scripts.preprocessing.extractors.common import is_likely_broken_korean_text
    controls = sum(ord(c) < 32 and c not in "\r\n\t" for c in text)
    broken = is_likely_broken_korean_text(text)
    return {"chars": len(text), "replacement_characters": text.count("\ufffd"),
            "controls": controls, "question_runs": text.count("???"),
            "broken_korean_heuristic": broken,
            "warning": bool("\ufffd" in text or controls or "???" in text or broken)}


def coverage(original, bodies):
    """Exact character coverage after whitespace removal; overlap is counted once."""
    raw = "".join(original.split())
    covered = bytearray(len(raw))
    all_found = bool(raw and bodies)
    for text in bodies:
        part = "".join(text.split())
        position = raw.find(part) if part else -1
        if position < 0:
            all_found = False
            continue
        # Repeated rows must not be mistaken for complete coverage.
        covered[position:position + len(part)] = b"\1" * len(part)
    count = sum(covered)
    return {"original_chars": len(raw), "covered_chars": count,
            "all_chunks_in_original": all_found,
            "exact_full_coverage": all_found and len(raw) >= 80 and count == len(raw)}


def recommend(local, database):
    """No timestamp-only winner and no automatic replacement of differing DB text."""
    if database.get("faithful") and database.get("valid"):
        return "database"
    if local.get("faithful") and local.get("valid"):
        return "review"  # A newer DB extraction may contain content the fresh extractor missed.
    return "review"


def processing_timeline(local_processed, local_vectorized, db_ingested, db_embedded):
    """Compare processing events, never infer the source document's revision date."""
    def parse(value):
        if not isinstance(value, str) or not value.strip():
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone(timedelta(hours=9)))
        return parsed.astimezone(timezone.utc)
    local = parse(local_vectorized)
    embedded = [stamp for value in db_embedded if (stamp := parse(value)) is not None]
    newest = max(embedded) if embedded else None
    relation = ("unknown" if local is None or newest is None else
                "db_embedding_later" if newest > local else
                "local_vectorization_later" if newest < local else "same_processing_time")
    return {"local_preprocessed_at": local_processed, "local_vectorized_at": local_vectorized,
            "db_ingested_at": db_ingested, "db_embedded_at": db_embedded,
            "processing_order": relation, "document_revision_order": "unconfirmed",
            "naive_local_timezone": "Asia/Seoul",
            "note": "DB processed_at is ingestion time. Processing order alone never selects a document version."}


def chunk_evidence(texts):
    combined = "\n".join(texts)
    hashes = [hashlib.sha256(text.encode("utf-8")).hexdigest() for text in texts]
    return {"body_sha256": hashlib.sha256(combined.encode("utf-8")).hexdigest(),
            "chunk_body_sha256": hashes, "duplicate_chunk_bodies": len(hashes) - len(set(hashes)),
            "total_chunk_chars_including_overlap": sum(map(len, texts))}


def approve(decision, *, raw_hash, local, database):
    winner = decision.get("winner")
    if winner not in {"database", "local"}:
        raise ValueError("Choose database/local after original and OCR review; unresolved items cannot sync")
    if decision.get("raw_sha256") != raw_hash:
        raise ValueError("Decision does not refer to the current original")
    selected = database if winner == "database" else local
    if not selected.get("valid"):
        raise ValueError("Selected version has invalid hashes, identity, or embeddings")
    auto = winner == "database" and selected.get("faithful")
    if not auto and (decision.get("original_reviewed") is not True or not str(decision.get("reason", "")).strip()):
        raise ValueError("Non-exact/OCR or local replacement requires original_reviewed=true and a reason")
    return winner


def db_snapshot(cur, sid, *, lock=False):
    cur.execute("select to_jsonb(s) as value from public.rag_sources s where id=%s" + (" for update" if lock else ""), (sid,))
    row = cur.fetchone()
    if not row:
        raise ValueError(f"Source disappeared: {sid}")
    source = row["value"]
    if source.get("metadata", {}).get("ingestion_dataset") != "ce":
        raise ValueError(f"Not an ingestion_dataset=ce source: {sid}")
    cur.execute("select (to_jsonb(c)-'content_tsv'-'embedding') || jsonb_build_object('embedding',c.embedding::text) as value "
                "from public.rag_chunks c where source_id=%s order by chunk_index" + (" for update" if lock else ""), (sid,))
    return {"source": source, "chunks": [r["value"] for r in cur.fetchall()]}


def records_from_db(snapshot, slug, target):
    from scripts.vectorization.load_to_supabase import DEFAULT_EMBEDDING_MODEL, record_identity, sha256_text
    from scripts.vectorization.common import validate_vectors
    source = snapshot["source"]
    records, positions = [], set()
    for chunk in snapshot["chunks"]:
        idx = chunk["chunk_index"]
        sid, cid = record_identity({"source_slug": slug, "chunk_index": idx}, "ce")
        if sid != source["id"] or chunk["id"] != cid or idx in positions or idx < 0:
            raise ValueError("DB chunk/source identity or positions are invalid")
        positions.add(idx)
        if chunk["content_hash"] != sha256_text(chunk["content"]):
            raise ValueError("DB content hash does not match actual text")
        vector = json.loads(chunk["embedding"]) if isinstance(chunk["embedding"], str) else chunk["embedding"]
        if chunk["embedding_model"] != DEFAULT_EMBEDDING_MODEL or chunk["embedding_dim"] != 384:
            raise ValueError("DB embedding model differs from configured search model")
        validate_vectors([vector], 1, 384)
        metadata = {**source.get("metadata", {}), **chunk.get("metadata", {}), "preprocessed_path": target}
        if metadata.get("source_path") != source["file_path"]:
            raise ValueError("DB chunk path differs from its source")
        records.append({"id": cid, "source_slug": slug, "chunk_id": chunk["chunk_id"], "chunk_index": idx,
                        "text": chunk["content"], "metadata": metadata, "embedding": vector,
                        "embedding_model": chunk["embedding_model"], "embedding_dim": 384})
    if not records:
        raise ValueError("DB version has no chunks")
    return records


def record_map(records):
    from scripts.vectorization.load_to_supabase import sha256_text
    result = {}
    for r in records:
        idx = int(r["chunk_index"])
        if idx in result:
            raise ValueError("Duplicate local chunk position")
        result[idx] = sha256_text(r["text"].replace("\x00", ""))
    return result


def excluded_sources():
    """Always leave Excel to its rebuild; leave missing-ingest selections to their owner."""
    excluded = set()
    for folder, name in (("missing_ingest_20261107", "missing_sources.json"),
                         ("excel_rebuild_20261107_032204", "targets.json")):
        path = ROOT / "var/recovery" / folder / name
        if path.exists():
            for item in read(path):
                if item.get("dataset") == "ce":
                    excluded.add(item["source_path"])
    return excluded


def compare(run, expected):
    from scripts.db import connect_postgres
    from psycopg.rows import dict_row
    from scripts.vectorization.load_to_supabase import iter_records, record_identity
    index = ROOT / "vectorized/ce/index.jsonl"
    before = digest(index)
    excluded = excluded_sources()
    local, skipped = {}, set()
    for r in iter_records(index):
        meta = r["metadata"]
        sid, _ = record_identity(r, "ce")
        if meta["source_path"] in excluded or str(meta.get("source_ext", "")).lower() in {".xls", ".xlsx"}:
            skipped.add(sid)
            continue
        entry = local.setdefault(sid, {"slug": r["source_slug"], "path": meta["source_path"],
                                       "target": meta["preprocessed_path"], "hashes": {}})
        if (entry["path"], entry["target"]) != (meta["source_path"], meta["preprocessed_path"]):
            raise ValueError("Local source ID maps to multiple original/preprocessed paths")
        hashes = record_map([r])
        if set(hashes).intersection(entry["hashes"]):
            raise ValueError("Duplicate local index position")
        entry["hashes"].update(hashes)
    db_hashes = defaultdict(dict)
    corrupt = set()
    snapshots = {}
    with connect_postgres(row_factory=dict_row) as conn:
        conn.execute("set transaction isolation level repeatable read read only")
        conn.execute("set local statement_timeout='600s'")
        with conn.cursor(name="ce_conflict_hashes") as cur:
            cur.execute("select c.source_id,c.chunk_index,c.content_hash,encode(sha256(convert_to(c.content,'UTF8')),'hex') actual_hash "
                        "from public.rag_chunks c join public.rag_sources s on c.source_id=s.id "
                        "where s.metadata->>'ingestion_dataset'='ce'")
            for row in cur:
                sid = row["source_id"]
                if sid in local:
                    db_hashes[sid][row["chunk_index"]] = row["actual_hash"]
                    if row["content_hash"] != row["actual_hash"]:
                        corrupt.add(sid)
        # This job reconciles existing differences, not missing documents or DB-only sources.
        selected = [sid for sid in local if sid in db_hashes and (local[sid]["hashes"] != db_hashes[sid] or sid in corrupt)]
        with conn.cursor() as cur:
            for sid in selected:
                snapshots[sid] = db_snapshot(cur, sid)
    wanted = set(selected)
    local_records = defaultdict(list)
    for r in iter_records(index):
        sid, _ = record_identity(r, "ce")
        if sid in wanted:
            local_records[sid].append(r)
    if digest(index) != before:
        raise ValueError("CE index changed during comparison; retry after the current writer finishes")
    items = []
    for sid in selected:
        item = local[sid]
        target = inside(item["target"], ROOT / "preprocessed/ce")
        source = snapshots[sid]["source"]
        if source["file_path"] != item["path"]:
            raise ValueError("Local and DB source paths differ; do not merge by ID alone")
        folder = run / "snapshots" / sid
        target_hash = digest(target)
        document = read(target)
        if digest(target) != target_hash:
            raise ValueError("Local preprocessing changed while taking its snapshot")
        if document["slug"] != item["slug"] or document["source_path"] != item["path"]:
            raise ValueError("Local preprocessing does not belong to the indexed source")
        atomic(folder / "database.json", snapshots[sid])
        atomic(folder / "local_records.json", sorted(local_records[sid], key=lambda r: r["chunk_index"]))
        atomic(folder / "local_document.json", document)
        raw_path = item["path"].split("!/", 1)[0]
        raw = inside(raw_path, ROOT / "files")
        item.update(id=sid, target_sha256=target_hash, raw_path=rel(raw), raw_sha256=digest(raw),
                    db_fingerprint=fingerprint(snapshots[sid]), snapshot=rel(folder),
                    local_records_sha256=digest(folder / "local_records.json"),
                    local_document_sha256=digest(folder / "local_document.json"))
        vector_path = ROOT / "vectorized/ce" / target.relative_to(ROOT / "preprocessed/ce")
        vector_hash = digest(vector_path) if vector_path.exists() else None
        payload = read(vector_path) if vector_path.exists() else {}
        if (digest(vector_path) if vector_path.exists() else None) != vector_hash:
            raise ValueError("Canonical vector file changed while taking its snapshot")
        if payload:
            atomic(folder / "local_vector.json", payload)
        item["local_vectorized_at"] = payload.get("vectorized_at")
        item["local_vector_sha256"] = vector_hash
        item["local_vector_matches_index"] = fingerprint(sorted(payload.get("records", []), key=lambda r: r["chunk_index"])) == fingerprint(sorted(local_records[sid], key=lambda r: r["chunk_index"]))
        item.pop("hashes")
        items.append(item)
        emit("conflict", source_id=sid, path=item["path"], local_preprocessed_at=document.get("processed_at"),
             db_ingested_at=source.get("processed_at"), local_chunks=len(local_records[sid]),
             db_chunks=len(snapshots[sid]["chunks"]))
    atomic(run / "plan.json", {"policy": POLICY, "items": items, "expected_count": expected,
                               "observed_count": len(items), "excluded_sources": len(skipped)})
    receipt(run, status="compared", conflicts=len(items), expected_count=expected)
    if len(items) != expected:
        raise ValueError(f"Current conflicts={len(items)}, expected={expected}. Review counts; no sync was performed")


def load_plan(run):
    plan = read(run / "plan.json")
    if plan.get("policy") != POLICY or plan["observed_count"] != plan["expected_count"]:
        raise ValueError("Unexpected conflict selection; compare again with the reviewed --expected-count")
    for item in plan["items"]:
        folder = inside(item["snapshot"], run)
        if fingerprint(read(folder / "database.json")) != item["db_fingerprint"]:
            raise ValueError("DB comparison snapshot changed")
        if (digest(folder / "local_records.json") != item["local_records_sha256"]
                or digest(folder / "local_document.json") != item["local_document_sha256"]):
            raise ValueError("Local comparison snapshot changed")
        if digest(inside(item["raw_path"])) != item["raw_sha256"]:
            raise ValueError("Original changed after comparison")
    return plan


def probe(run):
    """Extract originals only into the run; no shared preprocessing or embeddings are changed."""
    from scripts.preprocessing.file_formats import detect_file_format, extract_formatted_blocks
    from scripts.preprocessing.extractors.common import extract_blocks
    from scripts.preprocessing.text_cleaning import clean_extracted_text
    from scripts.vectorization.vectorization import extract_chunk_records
    from scripts.vectorization.load_to_supabase import prepare_row, DEFAULT_EMBEDDING_MODEL
    decisions, comparisons = [], []
    for item in load_plan(run)["items"]:
        folder = inside(item["snapshot"], run)
        document = read(folder / "local_document.json")
        local = read(folder / "local_records.json")
        snapshot = read(folder / "database.json")
        errors, blocks = [], []
        if not item["local_vector_matches_index"]:
            errors.append("Canonical vector file and shared index differ; review local_vector.json before selecting a version")
        try:
            if "!/" in item["path"]:
                raise ValueError("ZIP member requires its separate collision/provenance repair; no inferred member mapping")
            original = inside(item["raw_path"])
            copy = folder / "original" / original.name
            copy.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(original, copy)  # DOC converters must never create files beside the real original.
            if digest(copy) != item["raw_sha256"]:
                raise ValueError("Original changed while copying extraction evidence")
            if original.suffix.lower() == ".json":
                blocks = extract_blocks(copy)
            else:
                blocks = extract_formatted_blocks(copy, detect_file_format(copy))
            for block in blocks:
                block["text"] = clean_extracted_text(block.get("text", ""))
            atomic(folder / "original_blocks.json", blocks)
        except Exception as error:
            errors.append(f"Original extraction: {type(error).__name__}: {error}")
        raw_text = "\n".join(b.get("text", "") for b in blocks)
        ocr_blocks = sum("ocr" in b.get("type", "").lower() for b in blocks)
        recorded_hash = document.get("provenance", {}).get("attachment_sha256")
        if recorded_hash and recorded_hash != item["raw_sha256"] and "!/" not in item["path"]:
            errors.append("Current attachment hash differs from download-history hash; source version review required")
        local_valid = (item["local_vector_matches_index"]
                       and record_map(extract_chunk_records(document, inside(item["target"]), ROOT)) == record_map(local))
        try:
            for record in local:
                prepare_row(record, DEFAULT_EMBEDDING_MODEL, "ce", body_document=document)
        except (ValueError, TypeError, KeyError) as error:
            errors.append(f"Local validation: {error}")
            local_valid = False
        try:
            db_records = records_from_db(snapshot, item["slug"], item["target"])
            db_valid = True
        except (ValueError, TypeError, KeyError) as error:
            errors.append(f"DB validation: {error}")
            db_records, db_valid = [], False
        bodies = {"local": [body_text(r["text"], r["metadata"]) for r in local],
                  "database": [body_text(c["content"], {**snapshot["source"].get("metadata", {}), **c.get("metadata", {})})
                               for c in snapshot["chunks"]]}
        versions = {}
        for name, texts in bodies.items():
            q, cov = quality("\n".join(texts)), coverage(raw_text, texts)
            versions[name] = {"quality": q, "coverage": cov, "chunks": len(texts), "body_evidence": chunk_evidence(texts),
                              "chunk_lengths": [len(t) for t in texts],
                              "valid": local_valid if name == "local" else db_valid,
                              "faithful": cov["exact_full_coverage"] and not q["warning"]
                                          and not quality(raw_text)["warning"] and not ocr_blocks and not errors}
        result = {"id": item["id"], "path": item["path"], "original_quality": quality(raw_text),
                  "original_ocr_blocks": ocr_blocks,
                  "local_ocr_blocks": sum("ocr" in b.get("type", "").lower() for b in document.get("blocks", [])),
                  "db_quality_flags": sorted({flag for c in snapshot["chunks"] for flag in c.get("metadata", {}).get("quality_flags", [])}),
                  "local_preprocessed_at": document.get("processed_at"),
                  "local_vectorized_at": item.get("local_vectorized_at"),
                  "local_vector_matches_index": item["local_vector_matches_index"],
                  "local_file_mtime": inside(item["target"]).stat().st_mtime_ns,
                  "db_status": snapshot["source"].get("status"),
                  "db_ingested_at": snapshot["source"].get("processed_at"),
                  "db_embedded_at": sorted({c.get("embedded_at", "") for c in snapshot["chunks"]}),
                  "versions": versions, "errors": errors, "recommendation": recommend(versions["local"], versions["database"])}
        result["timeline"] = processing_timeline(result["local_preprocessed_at"], result["local_vectorized_at"],
                                                 result["db_ingested_at"], result["db_embedded_at"])
        result["ocr_quality_is_heuristic"] = True
        # Operational evidence for side-by-side original/DB/local review, not a guessed OCR repair.
        diff = "\n".join(difflib.unified_diff("\n".join(bodies["local"]).splitlines(),
                                             "\n".join(bodies["database"]).splitlines(), fromfile="local", tofile="database"))
        (folder / "body.diff").write_text(diff, encoding="utf-8")
        for name, texts in bodies.items():
            original_diff = "\n".join(difflib.unified_diff(raw_text.splitlines(), "\n".join(texts).splitlines(),
                                                         fromfile="original_extraction", tofile=name))
            (folder / f"original_to_{name}.diff").write_text(original_diff, encoding="utf-8")
        comparisons.append(result)
        decisions.append({"id": item["id"], "winner": result["recommendation"], "raw_sha256": item["raw_sha256"],
                          "original_reviewed": False, "reason": "Exact original coverage; preserve DB" if result["recommendation"] == "database" else ""})
        emit("comparison", **result)
    atomic(run / "comparisons.json", comparisons)
    if not (run / "decisions.json").exists():
        atomic(run / "decisions.json", decisions)
    receipt(run, status="compared_with_originals", unresolved=sum(d["winner"] == "review" for d in decisions),
            comparisons_sha256=digest(run / "comparisons.json"))


def stage(run):
    from scripts.vectorization.vectorization import extract_chunk_records
    from scripts.vectorization.load_to_supabase import prepare_row, DEFAULT_EMBEDDING_MODEL
    plan = load_plan(run)
    if digest(run / "comparisons.json") != read(run / "receipt.json").get("comparisons_sha256"):
        raise ValueError("Comparison evidence changed; run probe again before deciding")
    comparisons = {c["id"]: c for c in read(run / "comparisons.json")}
    declared = read(run / "decisions.json")
    decisions = {d["id"]: d for d in declared}
    if len(decisions) != len(declared):
        raise ValueError("Duplicate decision ID")
    if set(decisions) != {i["id"] for i in plan["items"]}:
        raise ValueError("Decisions must cover exactly the selected IDs")
    docs = []
    for item in plan["items"]:
        versions = comparisons[item["id"]]["versions"]
        winner = approve(decisions[item["id"]], raw_hash=item["raw_sha256"], **versions)
        folder = inside(item["snapshot"], run)
        document = read(folder / "local_document.json")
        snapshot = read(folder / "database.json")
        records = read(folder / "local_records.json")
        if winner == "database":
            records = records_from_db(snapshot, item["slug"], item["target"])
            chunks = [{"chunk_id": n + 1, "text": ""} for n in range(max(r["chunk_index"] for r in records) + 1)]
            for r in records:
                chunks[r["chunk_index"]] = {"chunk_id": r["chunk_id"], "text": body_text(r["text"], r["metadata"]),
                    **({"structured_table": r["metadata"]["structured_table"]} if r["metadata"].get("structured_table") else {})}
            document = {**document, "processed_at": None, "chunks": chunks,
                        "blocks": [{"type": "database_restored", "text": c["text"]} for c in chunks if c["text"]],
                        "provenance": snapshot["source"]["metadata"],
                        "restored_from_database": {"snapshot": item["snapshot"], "db_ingested_at": snapshot["source"].get("processed_at"),
                                                   "previous_local_processed_at": document.get("processed_at")},
                        "num_chunks": len(chunks), "used_metadata_fallback": False}
            document.update(num_blocks=len(document["blocks"]), total_chars=sum(len(b["text"]) for b in document["blocks"]))
        # A DB export must remain reproducible through the normal local vectorizer.
        if record_map(extract_chunk_records(document, inside(item["target"]), ROOT)) != record_map(records):
            raise ValueError(f"Selected version cannot reproduce its search chunks; preserve snapshots, review parser: {item['id']}")
        for record in records:
            row = prepare_row(record, DEFAULT_EMBEDDING_MODEL, "ce", body_document=document)
            if row["source"]["id"] != item["id"] or row["source"]["file_path"] != item["path"]:
                raise ValueError("Selected record does not match the scoped source ID and original path")
            if winner == "local" and (row["source"]["title"], row["source"]["url"]) != (snapshot["source"]["title"], snapshot["source"]["url"]):
                raise ValueError("Local title/URL differs from DB; resolve citation provenance separately before replacing chunks")
        destination = run / "preprocessed/ce" / inside(item["target"]).relative_to(ROOT / "preprocessed/ce")
        vector = run / "vectorized/ce" / destination.relative_to(run / "preprocessed/ce")
        atomic(destination, document)
        atomic(vector, {"source_preprocessed_path": item["target"], "records": records, "num_chunks": len(records),
                        "embedding_backend": records[0].get("embedding_model") or "sentence-transformers:sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
                        "embedding_dimensions": 384, "policy_version": 1, "reconciliation_run": rel(run),
                        "vectorized_at": item.get("local_vectorized_at") if winner == "local" else None,
                        "restored_at": datetime.now(timezone.utc).isoformat(),
                        "database_embedded_at": comparisons[item["id"]]["db_embedded_at"] if winner == "database" else []})
        docs.append({"dataset": "ce", "id": item["id"], "winner": winner, "slug": item["slug"], "source_path": item["path"],
                     "target": item["target"], "staged": rel(destination), "sha256": digest(destination),
                     "vector": rel(vector), "vector_sha256": digest(vector),
                     "previous": [{"path": item["target"], "sha256": item["target_sha256"]}]})
    atomic(run / "documents.json", docs)
    receipt(run, status="staged", database_verified=False, vectorization_complete=True,
            decision_sha256=digest(run / "decisions.json"), unresolved=0,
            ocr_quality_review_pending=sum(d.get("ocr_quality_status") == "needs_review" for d in declared))
    emit("staged", sources=len(docs), reused_embeddings=True)


def checked(run):
    from scripts.preprocessing.repair_archive_collisions import checked_documents
    if digest(run / "decisions.json") != read(run / "receipt.json").get("decision_sha256"):
        raise ValueError("Decisions changed; restage before applying")
    plan = load_plan(run)
    docs = checked_documents(run)
    if {d["id"] for d in docs} != {i["id"] for i in plan["items"]}:
        raise ValueError("Staged selection differs from comparison")
    return plan, docs


def check_local_selection(run, plan, *, allow_staged=False, require_staged=False):
    """Changes to unrelated index entries are allowed; selected versions must stay frozen."""
    from scripts.vectorization.load_to_supabase import iter_records, record_identity
    selected = {i["id"]: i for i in plan["items"]}
    found = defaultdict(list)
    index = ROOT / "vectorized/ce/index.jsonl"
    before = digest(index)
    for record in iter_records(index):
        sid, _ = record_identity(record, "ce")
        if sid in selected:
            found[sid].append(record)
    if digest(index) != before:
        raise ValueError("Shared CE index is still changing")
    staged = {d["id"]: d for d in read(run / "documents.json")} if allow_staged or require_staged else {}
    for sid, item in selected.items():
        allowed_body = set() if require_staged else {item["target_sha256"]}
        if sid in staged:
            allowed_body.add(staged[sid]["sha256"])
        if digest(inside(item["target"])) not in allowed_body:
            raise ValueError("Selected local preprocessing changed; take a new comparison")
        original = read(inside(item["snapshot"], run) / "local_records.json")
        allowed_records = set() if require_staged else {fingerprint(original)}
        if sid in staged:
            allowed_records.add(fingerprint(read(inside(staged[sid]["vector"], run))["records"]))
        if fingerprint(sorted(found[sid], key=lambda r: r["chunk_index"])) not in allowed_records:
            raise ValueError("Selected local index changed; do not publish the older comparison")
        vector = ROOT / "vectorized/ce" / inside(item["target"]).relative_to(ROOT / "preprocessed/ce")
        current_hash = digest(vector) if vector.exists() else None
        if require_staged or current_hash != item["local_vector_sha256"]:
            if not staged or not vector.exists() or fingerprint(read(vector)["records"]) not in allowed_records:
                raise ValueError("Selected canonical vector file changed")


def apply(run, after):
    from scripts.preprocessing.repair_archive_collisions import require_finished
    from scripts.db import connect_postgres
    from psycopg.rows import dict_row
    from scripts.vectorization.load_to_supabase import prepare_row, DEFAULT_EMBEDDING_MODEL, CHUNK_COLUMNS, copy_upsert
    require_finished(after)
    plan, docs = checked(run)
    check_local_selection(run, plan)
    # One transaction for the entire selection, including the preserved DB sources.
    with connect_postgres(row_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute("set transaction isolation level serializable")
        cur.execute("set local statement_timeout='600s'")
        cur.execute("set local lock_timeout='10s'")
        cur.execute("select pg_advisory_xact_lock(hashtextextended('ce-original-reconciliation',0))")
        for item in sorted(plan["items"], key=lambda i: i["id"]):
            current = db_snapshot(cur, item["id"], lock=True)
            if fingerprint(current) != item["db_fingerprint"]:
                raise ValueError("DB changed since comparison; compare again rather than overwriting newer data")
            if digest(inside(item["target"])) != item["target_sha256"]:
                raise ValueError("Local preprocessing changed since comparison")
        for doc in docs:
            if doc["winner"] == "database":
                continue  # Preserve all DB text, vectors, timestamps, status and priority byte-for-byte.
            body = read(inside(doc["staged"], run))
            rows = [prepare_row(r, DEFAULT_EMBEDDING_MODEL, "ce", body_document=body) for r in read(inside(doc["vector"], run))["records"]]
            cur.execute("delete from public.rag_chunks where source_id=%s", (doc["id"],))
            for start in range(0, len(rows), 32):
                copy_upsert(cur, "rag_chunks", "ce_reconciled_chunks", CHUNK_COLUMNS,
                            [r["chunk"] for r in rows[start:start + 32]], ("source_id", "chunk_index"), "embedded_at")
            # Source title/URL/status/priority/ingestion time stay authoritative in DB.
    receipt(run, status="applied", database_verified=False)
    verify_database(run)


def verify_database(run):
    from scripts.db import connect_postgres
    from psycopg.rows import dict_row
    plan, docs = checked(run)
    if read(run / "receipt.json").get("artifacts_published"):
        check_local_selection(run, plan, require_staged=True)
    by_id = {i["id"]: i for i in plan["items"]}
    with connect_postgres(row_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute("set transaction isolation level repeatable read read only")
        for doc in docs:
            current = db_snapshot(cur, doc["id"])
            prior = read(inside(by_id[doc["id"]]["snapshot"], run) / "database.json")
            if current["source"] != prior["source"]:
                raise ValueError("Source metadata/status/priority changed; review before continuing")
            actual = records_from_db(current, doc["slug"], doc["target"])
            selected = read(inside(doc["vector"], run))["records"]
            if (record_map(actual) != record_map(selected)
                    or [vector_key(r["embedding"]) for r in actual] != [vector_key(r["embedding"]) for r in selected]):
                raise ValueError("DB chunk positions, actual body hashes, or embeddings differ")
            if doc["winner"] == "database" and fingerprint(current) != by_id[doc["id"]]["db_fingerprint"]:
                raise ValueError("A preserved database version was modified")
    receipt(run, status="database_verified", database_verified=True)


def publish(run, after):
    from scripts.preprocessing.repair_archive_collisions import publish as publish_artifacts
    verify_database(run)
    plan, _ = checked(run)
    # On a retry accept only the frozen original or this run's selected version.
    check_local_selection(run, plan, allow_staged=True)
    # Reuse scoped, backed-up publication; it never uploads the whole CE dataset.
    publish_artifacts(run, {"archives": [{"path": i["raw_path"], "sha256": i["raw_sha256"]} for i in plan["items"]]}, after,
                      mark_complete=False)
    receipt(run, status="published_pending_search", artifacts_published=True)


def verify_search(run, backend_root):
    import logging
    if not read(run / "receipt.json").get("artifacts_published"):
        raise ValueError("Publish selected local artifacts first")
    verify_database(run)
    _, docs = checked(run)
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
    sys.path.insert(0, str(backend_root.resolve()))
    from app.config import get_settings
    from app.embeddings import Embedder
    from app.reranking import CrossEncoderReranker
    from app.retrieval import build_supabase_client, search
    from app.routers.ask import _row_to_source
    settings = get_settings()
    embedder = Embedder(model_name=settings.embedding_model, device="cpu", expected_dimensions=384)
    reranker = CrossEncoderReranker(model_name=settings.reranker_model, device="cpu", quantize=settings.reranker_quantize) if settings.reranker_enabled else None
    client = build_supabase_client(settings.supabase_url, settings.supabase_service_role_key)
    class Failures(logging.Handler):
        def __init__(self):
            super().__init__(logging.WARNING)
            self.messages = []
        def emit(self, record):
            if "failed" in record.getMessage().lower():
                self.messages.append(record.getMessage())
    cases = []
    for doc in docs:
        document = read(inside(doc["staged"], run))
        question = document["provenance"].get("doc_title") or document["source_file"]
        # Several attachments share one parent notice title. Identify the file
        # when verifying its exact source ID, as missing-ingest verification does.
        kind = document.get("source_kind") or document["provenance"].get("source_kind")
        if kind in {"attachment", "archive_member"}:
            name = Path(document["source_file"]).stem
            if name not in question:
                question += " / " + name
        failures, logger = Failures(), logging.getLogger("app.retrieval")
        logger.addHandler(failures)
        try:
            rows = search(client, rpc_names=["match_rag_documents"], embedding=embedder.encode_query(question),
                top_k=settings.rag_top_k, first_stage_k=settings.rag_first_stage_k, rpc_concurrency=settings.rag_rpc_concurrency,
                min_similarity=settings.rag_min_similarity, priority_weight=settings.rag_priority_weight,
                dataset_priority_weight=settings.rag_dataset_priority_weight, source_kind_weight=settings.rag_source_kind_weight,
                reranker=reranker, reranker_weight=settings.reranker_weight, max_chunks_per_url=settings.rag_max_chunks_per_url,
                max_lexical_chunks_per_url=settings.rag_max_lexical_chunks_per_url,
                title_anchor=settings.rag_title_anchor_enabled,
                query_text=question, metadata_filter={"ingestion_dataset": "ce"},
                scope_check=lambda r: (r.get("metadata") or {}).get("ingestion_dataset") == "ce")
        finally:
            logger.removeHandler(failures)
        hits = [r for r in rows if r.get("source_id") == doc["id"]]
        expected = {r["chunk_index"]: r["text"].replace("\x00", "") for r in read(inside(doc["vector"], run))["records"]}
        snapshot = next(i for i in load_plan(run)["items"] if i["id"] == doc["id"])
        source = read(inside(snapshot["snapshot"], run) / "database.json")["source"]
        active = source.get("status") == "active"
        if failures.messages or (active and not hits) or (not active and hits):
            raise ValueError(f"Real search failed or source visibility differs: {doc['id']}; {failures.messages}")
        for hit in hits:
            card = _row_to_source(hit)
            if (hit.get("content") != expected.get(hit.get("chunk_index")) or not card.uri or card.uri != source.get("url")
                    or card.title != source.get("title")):
                raise ValueError("Search body or citation URL differs from the selected source")
        cases.append({"id": doc["id"], "question": question, "expected_active": active,
                      "citations": [_row_to_source(r).model_dump() for r in hits]})
    atomic(run / "search_verification.json", {"status": "passed", "cases": cases, "backend_root": str(backend_root),
        "scope": "Installed retrieval + real Supabase RPC and /ask source serialization; attachments identified by parent title plus filename; HTTP/browser/answer generation not tested"})
    receipt(run, status="complete", search_verified=True, unresolved=0,
            ocr_quality_review_pending=sum(d.get("ocr_quality_status") == "needs_review" for d in read(run / "decisions.json")))
    emit("complete", sources=len(docs))


def main(argv=None):
    from scripts.recovery.cli import legacy_main
    return legacy_main("ce", argv)


if __name__ == "__main__":
    raise SystemExit(main())
