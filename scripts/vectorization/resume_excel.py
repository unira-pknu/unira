"""Resume an existing Excel rebuild from immutable delta indexes and verified DB checkpoints.

Uses existing vectors, scopes all writes by source ID/path/dataset, and backs up replaced rows.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import itertools
import json
from pathlib import Path
import sys
from threading import Lock, Event

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.recovery.common import atomic, digest, event, read, receipt, inside as resolve_inside
from scripts.vectorization.recovery_control import write_chunk_batches, PeerWorkerStopped


def inside(path):
    return resolve_inside(path, root=ROOT)


def sequence_hash(parts):
    return hashlib.sha256("".join(f"{position}:{checksum};" for position, checksum in parts).encode()).hexdigest()


def validate_empty_target(target):
    """Header-only forms contain labels, but no searchable spreadsheet rows."""
    from scripts.preprocessing.text_cleaning import has_searchable_body
    from scripts.vectorization.vectorization import extract_chunk_records
    path = inside(target["preprocessed"])
    document = read(path)
    if (document.get("slug") != target["slug"] or document.get("source_path") != target["source_path"]
            or document.get("provenance", {}).get("spreadsheet_policy_version") != "spreadsheet-rows-v1"):
        raise ValueError("Excluded Excel target provenance changed")
    if extract_chunk_records(document, path, ROOT):
        raise ValueError("A supposedly empty Excel target now contains searchable rows")
    if has_searchable_body(document):
        for chunk in document.get("chunks", []):
            if chunk.get("search_excluded") is True or not str(chunk.get("text") or "").strip():
                continue
            table = chunk.get("structured_table") or {}
            if (table.get("kind") != "spreadsheet" or not table.get("rows")
                    or any(row.get("row_kind") != "header" for row in table["rows"])):
                raise ValueError("Excluded Excel body requires review; not a header-only form")
    return document


def inventory(run):
    from scripts.vectorization.load_to_supabase import DEFAULT_EMBEDDING_MODEL, record_identity, sha256_text
    from scripts.vectorization.datasets import table_family
    targets = read(run / "targets.json")
    by_key = {(t["dataset"], t["slug"]): t for t in targets}
    if len(by_key) != len(targets):
        raise ValueError("Duplicate Excel target identity")
    vectorization = read(run / "vectorization.json")
    if vectorization["status"] != "complete" or vectorization["failures"]:
        raise ValueError("Finish isolated Excel vectorization first")
    entries, indexes = [], []
    for dataset in vectorization["datasets"]:
        name = dataset["dataset"]
        path = inside(dataset["delta"]) / "index.jsonl"
        manifest = read(path.with_name("manifest.json"))
        if (manifest["dataset"] != name or manifest["embedding_backend"] != DEFAULT_EMBEDDING_MODEL
                or manifest["embedding_dimensions"] != 384 or manifest.get("num_files_failed") or manifest.get("failures")):
            raise ValueError("Invalid Excel delta manifest")
        signature = (path.stat().st_size, path.stat().st_mtime_ns)
        checksum, count, seen = hashlib.sha256(), 0, set()
        current, hashes, metadata = None, [], None

        def finish():
            if current is None:
                return
            target = by_key[name, current]
            if target["source_path"] != metadata["source_path"] or target["preprocessed"] != metadata["preprocessed_path"]:
                raise ValueError("Excel delta provenance differs from target")
            sid, _ = record_identity({"source_slug": current, "chunk_index": 0}, name)
            entries.append({"id": sid, "dataset": name, "slug": current, "family": table_family(name, metadata),
                            "chunks": len(hashes), "body_hash": sequence_hash(hashes), "source_path": target["source_path"],
                            "preprocessed": target["preprocessed"], "preprocessed_sha256": digest(inside(target["preprocessed"]))})
        with path.open("rb") as handle:
            for line in handle:
                checksum.update(line)
                if not line.strip():
                    continue
                record = json.loads(line)
                slug = record["source_slug"]
                if slug != current:
                    finish()
                    if slug in seen or (name, slug) not in by_key:
                        raise ValueError("Interleaved or unexpected Excel source")
                    seen.add(slug)
                    current, hashes, metadata = slug, [], record["metadata"]
                meta = record["metadata"]
                if (meta.get("spreadsheet_policy_version") != "spreadsheet-rows-v1"
                        or meta.get("structured_table", {}).get("kind") != "spreadsheet"
                        or len(meta.get("structured_table", {}).get("rows", [])) != 1
                        or meta.get("source_path") != metadata.get("source_path")):
                    raise ValueError("Excel row structure/provenance is invalid")
                position = int(record["chunk_index"])
                if hashes and position <= hashes[-1][0]:
                    raise ValueError("Duplicate/out-of-order Excel chunk position")
                hashes.append((position, sha256_text(record["text"].replace("\x00", ""))))
                count += 1
        finish()
        if signature != (path.stat().st_size, path.stat().st_mtime_ns) or count != manifest["num_chunks"]:
            raise ValueError("Excel delta changed or chunk count differs")
        indexes.append({"dataset": name, "path": path.relative_to(ROOT).as_posix(), "sha256": checksum.hexdigest()})
        event("excel_inventory", dataset=name, sources=len(seen), chunks=count)
    if sum(e["chunks"] for e in entries) != vectorization["chunks"] or len({e["id"] for e in entries}) != len(entries):
        raise ValueError("Excel selection is incomplete or duplicated")
    selected = {(e["dataset"], e["slug"]) for e in entries}
    empty = [t for key, t in by_key.items() if key not in selected]
    if len(entries) + len(empty) != len(targets):
        raise ValueError("Excel targets are not fully accounted for")
    result = {"policy": "excel-verified-resume-v1", "entries": entries, "indexes": indexes, "empty": empty}
    atomic(run / "resume_inventory.json", result)
    return result


def verify_entries(conn, entries):
    from psycopg import sql
    from scripts.vectorization.load_to_supabase import DEFAULT_EMBEDDING_MODEL
    for family in sorted({e["family"] for e in entries}):
        selected = [e for e in entries if e["family"] == family]
        for offset in range(0, len(selected), 100):
            group = selected[offset:offset + 100]
            with conn.cursor() as cur:
                cur.execute(sql.SQL("select id,file_path,metadata->>'ingestion_dataset',metadata->>'spreadsheet_policy_version',status from {} where id=any(%s)").format(
                    sql.Identifier("public", family + "_sources")), ([e["id"] for e in group],))
                sources = {row[0]: row[1:] for row in cur.fetchall()}
                cur.execute(sql.SQL("""select source_id,count(*),
                    encode(sha256(convert_to(string_agg(chunk_index::text||':'||encode(sha256(convert_to(content,'UTF8')),'hex')||';','' order by chunk_index),'UTF8')),'hex'),
                    count(*) filter(where content_hash is distinct from encode(sha256(convert_to(content,'UTF8')),'hex') or embedding is null
                      or embedding_dim is distinct from 384 or embedding_model is distinct from %s
                      or metadata->>'spreadsheet_policy_version' is distinct from 'spreadsheet-rows-v1'
                      or metadata->'structured_table'->>'kind' is distinct from 'spreadsheet'
                      or coalesce(jsonb_array_length(metadata->'structured_table'->'rows'),0)<>1)
                    from {} where source_id=any(%s) group by source_id""").format(sql.Identifier("public", family + "_chunks")),
                    (DEFAULT_EMBEDDING_MODEL, [e["id"] for e in group]))
                actual = {row[0]: row[1:] for row in cur.fetchall()}
            for entry in group:
                if sources.get(entry["id"]) != (entry["source_path"], entry["dataset"], "spreadsheet-rows-v1", "active"):
                    raise ValueError(f"Excel checkpoint source/provenance differs: {entry['id']}")
                if actual.get(entry["id"]) != (entry["chunks"], entry["body_hash"], 0):
                    raise ValueError(f"Excel checkpoint chunks/body hash/model/structure differ: {entry['id']}")
            event("excel_verified", family=family, verified=min(offset + 100, len(selected)), total=len(selected))


def validate_scope(entry, existing):
    if existing and (existing["file_path"] != entry["source_path"]
                     or existing.get("metadata", {}).get("ingestion_dataset") != entry["dataset"]):
        raise ValueError("Refusing to overwrite another dataset/path")


def checkpoint_sources(progress, entries):
    completed = progress["completed_sources"]
    if not isinstance(completed, list) or any(not isinstance(item, dict) or not item.get("id") for item in completed):
        raise ValueError("Invalid Excel checkpoint source list")
    done = {e["id"] for e in completed}
    if len(done) != len(completed) or not done.issubset(entries):
        raise ValueError("Excel checkpoint IDs are duplicated/outside selection")
    for item in completed:
        if any(item.get(key) != entries[item["id"]][key] for key in ("dataset", "slug", "family", "chunks")):
            raise ValueError("Excel checkpoint metadata/count differs from the confirmed inventory")
    if progress.get("chunks_loaded") != sum(entries[sid]["chunks"] for sid in done):
        raise ValueError("Excel checkpoint total does not match its completed sources")
    return done


def load(run, workers, batch):
    from psycopg import sql
    from psycopg.errors import QueryCanceled, LockNotAvailable, DeadlockDetected
    from scripts.db import connect_postgres
    from scripts.vectorization.load_to_supabase import (
        DEFAULT_EMBEDDING_MODEL, SOURCE_COLUMNS, DEPARTMENT_COLUMNS, CHUNK_COLUMNS,
        iter_records, record_identity, prepare_row, copy_upsert,
    )
    plan = read(run / "resume_inventory.json") if (run / "resume_inventory.json").exists() else inventory(run)
    for position, item in enumerate(plan["indexes"], 1):
        event("excel_delta_check", dataset=item["dataset"], position=position, total=len(plan["indexes"]))
        if digest(inside(item["path"])) != item["sha256"]:
            raise ValueError("Excel delta changed since inventory")
    entries = {e["id"]: e for e in plan["entries"]}
    progress = read(run / "db_load.json")
    done = checkpoint_sources(progress, entries)
    receipt(run, status="verifying_checkpoint", database_verified=False, search_verified=False)
    with connect_postgres() as conn:
        conn.execute("set transaction isolation level repeatable read read only")
        conn.execute("set local statement_timeout='600s'")
        verify_entries(conn, [entries[sid] for sid in sorted(done)])
    event("excel_checkpoint_verified", sources=len(done), remaining=len(entries) - len(done))
    guard = Lock()
    stop = Event()
    assigned = [[] for _ in range(workers)]
    weights = [0] * workers
    for index in sorted(plan["indexes"], key=lambda i: -sum(e["chunks"] for e in entries.values() if e["dataset"] == i["dataset"] and e["id"] not in done)):
        remaining = sum(e["chunks"] for e in entries.values() if e["dataset"] == index["dataset"] and e["id"] not in done)
        if remaining:
            position = min(range(workers), key=lambda i: weights[i])
            assigned[position].append(index)
            weights[position] += remaining
    event("excel_resume", remaining_chunks=weights, workers=workers, chunk_batch_size=batch)
    receipt(run, status="loading", resume_workers=workers, resume_chunk_batch_size=batch)

    def process_indexes(indexes):
        with connect_postgres(autocommit=True) as conn:
            for index in indexes:
                for slug, records in itertools.groupby(iter_records(inside(index["path"])), key=lambda r: r["source_slug"]):
                    if stop.is_set():
                        raise PeerWorkerStopped("Another Excel worker failed")
                    sid, _ = record_identity({"source_slug": slug, "chunk_index": 0}, index["dataset"])
                    if sid in done:
                        continue
                    entry = entries[sid]
                    path = inside(entry["preprocessed"])
                    if digest(path) != entry["preprocessed_sha256"]:
                        raise ValueError("Excel preprocessing changed since inventory")
                    body = read(path)
                    rows = [prepare_row(r, DEFAULT_EMBEDDING_MODEL, entry["dataset"], body_document=body) for r in records]
                    source, family = rows[0]["source"], entry["family"]
                    if len(rows) != entry["chunks"] or source["id"] != sid or source["file_path"] != entry["source_path"]:
                        raise ValueError("Excel staged source identity/count changed")
                    with conn.transaction(), conn.cursor() as cur:
                        cur.execute("set local statement_timeout='600s'")
                        cur.execute("set local lock_timeout='15s'")
                        cur.execute(sql.SQL("select to_jsonb(s) from {} s where id=%s for update").format(sql.Identifier("public", family + "_sources")), (sid,))
                        old = cur.fetchone()
                        existing = old[0] if old else None
                        validate_scope(entry, existing)
                        already = existing and existing.get("metadata", {}).get("spreadsheet_policy_version") == "spreadsheet-rows-v1"
                        if already:
                            verify_entries(conn, [entry])  # Committed before an interrupted checkpoint: preserve its timestamps.
                        else:
                            backup = run / "backup_database_resume" / (sid + ".json")
                            if existing and not backup.exists():
                                cur.execute(sql.SQL("select to_jsonb(c)-'content_tsv' from {} c where source_id=%s order by chunk_index").format(
                                    sql.Identifier("public", family + "_chunks")), (sid,))
                                atomic(backup, {"source": existing, "chunks": [r[0] for r in cur.fetchall()]})
                            columns = SOURCE_COLUMNS + DEPARTMENT_COLUMNS if family == "rag" else SOURCE_COLUMNS
                            copy_upsert(cur, family + "_sources", "excel_resume_sources", columns, [source], ("id",), "processed_at")
                            cur.execute(sql.SQL("delete from {} where source_id=%s").format(sql.Identifier("public", family + "_chunks")), (sid,))
                            def write_part(part):
                                if stop.is_set():
                                    raise PeerWorkerStopped("Another Excel worker failed")
                                return copy_upsert(cur, family + "_chunks", "excel_resume_chunks", CHUNK_COLUMNS, part,
                                                   ("source_id", "chunk_index"), "embedded_at")
                            write_chunk_batches([r["chunk"] for r in rows], batch_size=batch, max_retries=2, savepoint=conn.transaction,
                                write=write_part,
                                retryable=(QueryCanceled, LockNotAvailable, DeadlockDetected),
                                on_retry=lambda error, attempt, size: event("excel_retry", id=sid, attempt=attempt, size=size, error=type(error).__name__))
                        if stop.is_set():
                            raise PeerWorkerStopped("Excel worker stopped before source commit")
                    with guard:
                        done.add(sid)
                        progress["completed_sources"].append({k: entry[k] for k in ("id", "dataset", "slug", "family", "chunks")})
                        progress["chunks_loaded"] = sum(e["chunks"] for e in progress["completed_sources"])
                        progress["status"] = "loading"
                        atomic(run / "db_load.json", progress)
                        event("excel_loading", sources=len(done), total=len(entries), chunks=progress["chunks_loaded"], preserved_existing=bool(already))
    def worker(indexes):
        try:
            process_indexes(indexes)
        except BaseException:
            stop.set()
            raise

    errors = []
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(worker, indexes) for indexes in assigned]
        try:
            for future in futures:
                try:
                    future.result()
                except Exception as error:
                    errors.append(error)
        except BaseException:
            stop.set()
            for future in futures:
                future.cancel()
            raise
    if errors:
        raise next((e for e in errors if not isinstance(e, PeerWorkerStopped)), errors[0])
    # Empty new extracts stay excluded; no metadata-only replacement of old bodies.
    from scripts.vectorization.datasets import table_family
    for target in plan["empty"]:
        document = validate_empty_target(target)
        sid, _ = record_identity({"source_slug": target["slug"], "chunk_index": 0}, target["dataset"])
        family = table_family(target["dataset"], document.get("provenance"))
        with connect_postgres() as conn, conn.cursor() as cur:
            cur.execute(sql.SQL("update {} set status='inactive' where id=%s and file_path=%s and metadata->>'ingestion_dataset'=%s").format(
                sql.Identifier("public", family + "_sources")), (sid, target["source_path"], target["dataset"]))
        if not any(e.get("id") == sid for e in progress["excluded_sources"]):
            progress["excluded_sources"].append({"id": sid, "dataset": target["dataset"], "reason": "no_searchable_spreadsheet_rows"})
    with connect_postgres() as conn:
        conn.execute("set transaction isolation level repeatable read read only")
        conn.execute("set local statement_timeout='600s'")
        verify_entries(conn, list(entries.values()))
    progress.update(status="complete", completed_at=datetime.now(timezone.utc).isoformat())
    atomic(run / "db_load.json", progress)
    receipt(run, status="database_verified_pending_search", database_verified=True, search_verified=False,
            verified_sources=len(entries), verified_chunks=progress["chunks_loaded"])
    event("excel_database_complete", sources=len(entries), chunks=progress["chunks_loaded"], excluded=len(progress["excluded_sources"]))


def expected_courses(document, request):
    names = set()
    for chunk in document.get("chunks", []):
        for row in chunk.get("structured_table", {}).get("rows", []):
            fields = row.get("fields", {})
            if (fields.get("staff_name") == request.instructor and str(fields.get("year")) == str(request.year)
                    and str(fields.get("term")) == str(request.term) and fields.get("course_name")):
                names.add(str(fields["course_name"]))
    if not names:
        raise ValueError("Probe has no matching instructor/year/term rows in the verified original spreadsheet")
    return names


def finalize(run, backend_root, question, probe_path):
    """Finish the receipt only after DB verification and a real catalogue lookup."""
    plan, progress = read(run / "resume_inventory.json"), read(run / "db_load.json")
    entries = {e["id"]: e for e in plan["entries"]}
    if progress.get("status") != "complete" or checkpoint_sources(progress, entries) != set(entries):
        raise ValueError("Finish/resume the Excel load before finalization")
    for item in plan["indexes"]:
        if digest(inside(item["path"])) != item["sha256"]:
            raise ValueError("Excel delta changed before finalization")
    if not (backend_root / "app/syllabus.py").is_file():
        raise ValueError("Select the installed backend directory containing app/syllabus.py")
    sys.path.insert(0, str(backend_root.resolve()))
    from app.config import get_settings
    from app.retrieval import build_supabase_client
    from app.syllabus import extract_request, search_syllabus
    from app.routers.ask import _row_to_source
    request = extract_request(question)
    if not request or not request.list_courses or not all((request.instructor, request.year, request.term, request.level)):
        raise ValueError("Catalogue probe must explicitly name instructor, year, term, and level")
    if probe_path:
        probe = inside(probe_path)
        selected = [e for e in entries.values() if inside(e["preprocessed"]) == probe]
    else:
        selected = [e for e in entries.values() if e["dataset"] == "cbaliberal"
                    and "개설교과목현황" in Path(e["preprocessed"]).name.replace(" ", "")]
    if len(selected) != 1:
        raise ValueError("Use --probe-preprocessed to select one verified Excel course-offering document")
    source = selected[0]
    probe = inside(source["preprocessed"])
    if digest(probe) != source["preprocessed_sha256"]:
        raise ValueError("The original Excel probe changed since the verified inventory")
    wanted = expected_courses(read(probe), request)
    for target in plan["empty"]:
        validate_empty_target(target)
    # Freeze installed settings before scripts.db loads the workspace .env into
    # os.environ. The API probe must use the selected backend's configuration.
    settings = get_settings()
    receipt(run, status="finalizing", database_verified=False, search_verified=False)
    from scripts.db import connect_postgres
    with connect_postgres() as conn:
        conn.execute("set transaction isolation level repeatable read read only")
        conn.execute("set local statement_timeout='600s'")
        verify_entries(conn, list(entries.values()))
        from psycopg import sql
        for family in sorted({e["family"] for e in entries.values()}):
            empty = [t for t in plan["empty"] if t["dataset"] in {e["dataset"] for e in entries.values() if e["family"] == family}]
            if empty:
                rows = conn.execute(sql.SQL("select id from {} where status='active' and file_path=any(%s)").format(
                    sql.Identifier("public", family + "_sources")), ([t["source_path"] for t in empty],)).fetchall()
                if rows:
                    raise ValueError("An empty rebuilt spreadsheet still has an active source; review its exact ID before excluding it")
    result = search_syllabus(build_supabase_client(settings.supabase_url, settings.supabase_service_role_key), request)
    offerings = [offering for row in result.rows for offering in row.get("metadata", {}).get("catalogue_offerings", [])]
    actual = {o["course_name"] for o in offerings}
    if (not result.answerable or actual != wanted or not offerings
            or any(o.get("staff_name") != request.instructor or str(o.get("year")) != str(request.year)
                   or str(o.get("term")) != str(request.term) or o.get("level") != request.level for o in offerings)):
        raise ValueError("Real instructor catalogue search differs from the verified original Excel rows")
    citations = [_row_to_source(row).model_dump() for row in result.rows]
    if not citations or any(not c["uri"].startswith(("https://", "http://")) for c in citations):
        raise ValueError("Catalogue search did not return a valid source URL")
    verification = {"status": "passed", "sources": len(entries), "chunks": progress["chunks_loaded"],
                    "question": question, "expected_courses": sorted(wanted), "actual_courses": sorted(actual),
                    "citations": citations, "backend_root": str(backend_root), "syllabus_sha256": digest(backend_root / "app/syllabus.py"),
                    "scope": "Real DB chunk hashes/models/row structure and installed instructor catalogue lookup; no OpenAI, generation, authenticated HTTP, or browser test"}
    atomic(run / "final_verification.json", verification)
    receipt(run, status="complete", database_verified=True, search_verified=True,
            completed_at=datetime.now(timezone.utc).isoformat(), resume_verification=verification)
    event("excel_complete", sources=len(entries), chunks=progress["chunks_loaded"], courses=sorted(actual))


def main(argv=None):
    from scripts.recovery.cli import legacy_main
    return legacy_main("excel", argv)


if __name__ == "__main__":
    raise SystemExit(main())
