"""Selective ZIP repair. Prepare/stage/vectorize use isolated artifacts; apply waits for earlier jobs.

No processing, model loading, or database access occurs on import.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import sys
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.preprocessing.file_preprocessing import (
    ARCHIVE_PATH_POLICY_VERSION, EXTRACTABLE_EXTS, MAX_ARCHIVE_MEMBERS,
    MAX_ARCHIVE_MEMBER_BYTES, MAX_ARCHIVE_TOTAL_BYTES, decoded_archive_member_name,
    is_safe_archive_member, save_preprocessed_archive_member, validate_archive_member_targets,
)
from scripts.vectorization.recovery_control import recovery_lock, write_chunk_batches, replace_with_retry
from scripts.recovery.common import atomic, digest, event, read, receipt, inside as resolve_inside
from scripts.artifact_paths import reference_path


def inside(path, boundary=None):
    return resolve_inside(path, root=ROOT, boundary=boundary)


def relative(path):
    return reference_path(inside(path), ROOT)


def require_finished(paths):
    if not paths:
        raise ValueError("apply/publish require --after-receipt for each earlier ingestion job")
    known_jobs = [ROOT / "var/recovery/missing_ingest_20261107/receipt.json",
                  ROOT / "var/recovery/excel_rebuild_20261107_032204/receipt.json"]
    paths = list(paths) + [path for path in known_jobs if path.exists()]
    for path in paths:
        state = read(inside(path))
        if state.get("status") not in {"complete", "completed"}:
            raise RuntimeError(f"Earlier ingestion has not completed: {path} ({state.get('status')})")


def dependency_states(paths):
    known = [ROOT / "var/recovery/missing_ingest_20261107/receipt.json",
             ROOT / "var/recovery/excel_rebuild_20261107_032204/receipt.json"]
    paths = list(dict.fromkeys([inside(p) for p in paths] + [p for p in known if p.exists()]))
    if not paths:
        raise ValueError("resume requires --after-receipt for earlier ingestion jobs")
    return [{"path": relative(p), "status": read(p).get("status")} for p in paths]


def preflight(run):
    """Validate real staged files and vectors without opening a DB connection."""
    state = read(run / "receipt.json")
    if not state.get("vectorization_complete"):
        raise ValueError("Complete isolated vectorization first")
    plan = read(run / "plan.json")
    if state.get("status") != "complete" and not state.get("database_verified"):
        check_inputs(plan)
    else:
        for archive in plan["archives"]:
            if digest(inside(archive["path"])) != archive["sha256"]:
                raise ValueError("ZIP changed since verification")
    docs = checked_documents(run)
    if len(docs) != len(plan["members"]):
        raise ValueError("Document ledger does not cover the confirmed plan")
    from scripts.vectorization.load_to_supabase import preflight_index, record_identity
    from scripts.vectorization.datasets import table_family
    count = 0
    identities = set()
    bodies = {d["source_path"]: read(inside(d["staged"], run)) for d in docs}
    for dataset in sorted({d["dataset"] for d in docs if "vector" in d}):
        preflight_index(run / "vectorized" / dataset / "index.jsonl", dataset, body_documents=bodies)
    for doc in docs:
        if "vector" not in doc:
            continue
        payload = read(inside(doc["vector"], run))
        records = payload["records"]
        staged = read(inside(doc["staged"], run))
        expected_origin = staged["provenance"].get("source_dataset") or doc["dataset"]
        if state.get("status") != "complete" and not state.get("database_verified"):
            template = read(inside(doc["provenance_source"]))
            expected_origin = template.get("provenance", {}).get("source_dataset") or doc["dataset"]
        sid = record_identity(records[0], doc["dataset"])[0]
        if sid in identities:
            raise ValueError("Different repaired members share a source ID")
        identities.add(sid)
        for record in records:
            metadata = record["metadata"]
            if (record["source_slug"] != doc["slug"] or metadata.get("source_path") != doc["source_path"]
                    or metadata.get("archive_member_path") != doc["member"]
                    or metadata.get("archive_member_sha256") != staged["provenance"]["archive_member_sha256"]
                    or metadata.get("archive_path_policy_version") != ARCHIVE_PATH_POLICY_VERSION
                    or table_family(doc["dataset"], metadata) != table_family(doc["dataset"], {"source_dataset": expected_origin})):
                raise ValueError(f"Vector/member provenance mismatch; restage and vectorize: {doc['source_path']}")
        count += len(records)
    event("preflight_passed", members=len(docs), searchable_members=len(identities), chunks=count)
    return {"members": len(docs), "searchable_members": len(identities), "chunks": count}


def resume(run, after):
    state = read(run / "receipt.json")
    if state.get("status") == "complete":
        event("already_complete", message="No processing or DB writes performed")
        return
    preflight(run)
    dependencies = dependency_states(after)
    blocked = [item for item in dependencies if item["status"] not in {"complete", "completed"}]
    if blocked:
        receipt(run, status="waiting_for_ingestion", waiting_for=blocked)
        event("waiting_for_ingestion", dependencies=blocked,
              message="Staged artifacts passed. No DB or shared files changed. Run resume again after these jobs complete.")
        return
    plan = read(run / "plan.json")
    if state.get("database_verified"):
        verify(run)
    else:
        apply(run, plan, after)
    publish(run, plan, after)


def prepare(run):
    if (run / "plan.json").exists():
        plan = read(run / "plan.json")
        if (plan.get("policy_version") != ARCHIVE_PATH_POLICY_VERSION
                or not isinstance(plan.get("members"), list)
                or not isinstance(plan.get("archives"), list)):
            raise ValueError("Existing repair plan has an incompatible format; select a new --run directory")
        event("plan_reused", archives=len(plan["archives"]), affected_members=len(plan["members"]),
              message="Existing selection retained; no inputs reprocessed or progress reset")
        return
    by_archive = defaultdict(list)
    for path in sorted((ROOT / "preprocessed").glob("*")):
        if not path.is_dir() or path.name.startswith(("_", ".")):
            continue
        for output in path.rglob("*.json"):
            doc = read(output)
            source = str(doc.get("source_path") or "")
            if "!/" not in source:
                continue
            archive, member = source.split("!/", 1)
            if Path(archive).suffix.lower() != ".zip" or not is_safe_archive_member(member):
                continue
            by_archive[(path.name, archive)].append((output, doc, member))
    archives, members, issues = [], [], []
    for (dataset, archive_name), previous in sorted(by_archive.items()):
        archive = inside(archive_name)
        try:
            with zipfile.ZipFile(archive) as zf:
                supported = [(i, decoded_archive_member_name(i).replace("\\", "/"))
                             for i in zf.infolist() if not i.is_dir()]
                supported = [(i, name) for i, name in supported if is_safe_archive_member(name)
                             and PurePosixPath(name).suffix.lower() in EXTRACTABLE_EXTS]
                validate_archive_member_targets([name for _, name in supported])
                groups = defaultdict(list)
                for info, name in supported:
                    groups[PurePosixPath(name).with_suffix(".json").as_posix().casefold()].append((info, name))
                affected = [pair for group in groups.values() if len(group) > 1 for pair in group]
                if not affected:
                    continue
                if len(supported) > MAX_ARCHIVE_MEMBERS or sum(i.file_size for i, _ in supported) > MAX_ARCHIVE_TOTAL_BYTES:
                    raise ValueError("Archive exceeds extraction limits")
                if any(i.file_size > MAX_ARCHIVE_MEMBER_BYTES or i.flag_bits & 1 for i, _ in affected):
                    raise ValueError("Affected member is too large or encrypted")
                bases = set()
                for output, doc, name in previous:
                    expected = PurePosixPath(name)
                    if output.name not in {expected.name + ".json", expected.with_suffix(".json").name}:
                        raise ValueError(f"Unrecognized previous output path: {output}")
                    bases.add(output.parents[len(expected.parts) - 1])
                if len(bases) != 1:
                    raise ValueError("Archive has multiple preprocessing roots; resolve aliases first")
                base = bases.pop()
                archive_key = hashlib.sha256(archive_name.encode("utf-8")).hexdigest()[:16]
                archives.append({"path": archive_name, "sha256": digest(archive), "dataset": dataset})
                for info, name in affected:
                    old_key = PurePosixPath(name).with_suffix(".json").as_posix().casefold()
                    old_keys = {old_key, PurePosixPath(info.filename.replace("\\", "/")).with_suffix(".json").as_posix().casefold()}
                    candidates = [(p, d) for p, d, n in previous
                                  if PurePosixPath(n).with_suffix(".json").as_posix().casefold() in old_keys]
                    # A collision can have lost every derived result. Another
                    # member of the same ZIP still supplies parent provenance,
                    # but is never treated as a replacement/identity candidate.
                    template = candidates[0][0] if candidates else previous[0][0]
                    member_rel = Path(*PurePosixPath(name).parts)
                    target = base / member_rel.parent / (member_rel.name + ".json")
                    old = [{"path": relative(p), "sha256": digest(p)} for p, _ in candidates]
                    source = f"{archive_name}!/{name}"
                    key = hashlib.sha256(source.encode("utf-8")).hexdigest()
                    members.append({"dataset": dataset, "archive": archive_name, "archive_key": archive_key,
                                    "member": name, "original_name": info.filename, "crc": info.CRC,
                                    "size": info.file_size, "source_path": source, "target": relative(target),
                                    "previous": old, "key": key, "provenance_source": relative(template),
                                    "provenance_sha256": digest(template)})
        except (OSError, ValueError, zipfile.BadZipFile) as error:
            # Never publish a partially planned archive.
            members = [m for m in members if not (m["archive"] == archive_name and m["dataset"] == dataset)]
            archives = [a for a in archives if not (a["path"] == archive_name and a["dataset"] == dataset)]
            issues.append({"archive": archive_name, "error": str(error)})
    targets = [m["target"].casefold() for m in members]
    if len(targets) != len(set(targets)):
        raise ValueError("Planned output paths collide across archives")
    plan = {"policy_version": ARCHIVE_PATH_POLICY_VERSION, "archives": archives, "members": members, "issues": issues}
    atomic(run / "plan.json", plan)
    receipt(run, status="planned", selected_members=len(members), archives=len(archives), issues=issues)
    event("planned", archives=len(archives), affected_members=len(members), issues=issues)


def check_inputs(plan):
    for archive in plan["archives"]:
        if digest(inside(archive["path"])) != archive["sha256"]:
            raise ValueError(f"ZIP changed since planning: {archive['path']}")
    for member in plan["members"]:
        if digest(inside(member["provenance_source"])) != member["provenance_sha256"]:
            raise ValueError("Parent archive provenance changed since planning")
        for previous in member["previous"]:
            if digest(inside(previous["path"])) != previous["sha256"]:
                raise ValueError(f"Previous preprocessing changed: {previous['path']}")


def stage(run, plan):
    if not plan["members"]:
        raise ValueError("No repairable collision members were selected; inspect plan issues")
    receipt(run, vectorization_complete=False, database_verified=False)
    check_inputs(plan)
    documents = []
    for member in plan["members"]:
        archive = inside(member["archive"])
        prior = [inside(p["path"]) for p in member["previous"]]
        provenance = read(inside(member["provenance_source"])).get("provenance", {}).copy()
        for key in ("archive_member_path", "archive_member_original_name", "archive_member_ext", "archive_member_sha256",
                    "previous_preprocessed_path", "previous_source_path"):
            provenance.pop(key, None)
        # The ingestion dataset can be pknu_main while the original document is
        # a pknu_notice. Keep that origin so table_family() routes it correctly.
        provenance["source_dataset"] = provenance.get("source_dataset") or member["dataset"]
        provenance["archive_member_original_name"] = member["original_name"]
        output_root = run / "preprocessed" / member["dataset"] / member["archive_key"]
        with zipfile.ZipFile(archive) as zf, tempfile.TemporaryDirectory(prefix="zip_repair_") as temporary:
            info = zf.getinfo(member["original_name"])
            if info.CRC != member["crc"] or info.file_size != member["size"]:
                raise ValueError("ZIP member changed since planning")
            extracted = Path(temporary) / PurePosixPath(member["member"]).name
            with zf.open(info) as src, extracted.open("wb") as dst:
                shutil.copyfileobj(src, dst)
            saved, output = save_preprocessed_archive_member(
                archive, extracted, member["member"], archive.parent, output_root, ROOT, provenance,
                900, 120, "auto", "kor+eng", 200, previous_outputs=prior, retire_previous=False)
            if not saved:
                raise ValueError(output)
        path = Path(output)
        doc = read(path)
        if doc["source_path"] != member["source_path"]:
            raise ValueError("Extracted source identity mismatch")
        documents.append({**member, "staged": relative(path), "slug": doc["slug"], "sha256": digest(path)})
        event("staged", member=member["source_path"], chunks=doc["num_chunks"])
    atomic(run / "documents.json", documents)
    receipt(run, status="staged", staged_members=len(documents))


def vectorize(run):
    from scripts.vectorization.common import SentenceTransformerEmbedder
    from scripts.vectorization.vectorization import run_batch, atomic_json
    docs = checked_documents(run, include_vectors=False)
    receipt(run, vectorization_complete=False, database_verified=False)
    for doc in docs:
        doc.pop("vector", None)
        doc.pop("vector_sha256", None)
    atomic(run / "documents.json", docs)
    embedder = SentenceTransformerEmbedder()
    for dataset in sorted({d["dataset"] for d in docs}):
        selected = [d for d in docs if d["dataset"] == dataset]
        input_root, output_root = run / "preprocessed" / dataset, run / "vectorized" / dataset
        manifest = run_batch(input_root, output_root, ROOT, embedder, 32, False, dataset)
        if manifest["num_files_failed"]:
            raise RuntimeError(f"Vectorization failed for {dataset}")
        by_path = {d["staged"]: d for d in selected}
        records = []
        for vector in sorted(output_root.rglob("*.json")):
            if vector.name == "manifest.json":
                continue
            payload = read(vector)
            entry = by_path[payload["source_preprocessed_path"]]
            payload["source_preprocessed_path"] = entry["target"]
            for record in payload["records"]:
                record["metadata"]["preprocessed_path"] = entry["target"]
                marker = "rule_repair_run" if record["metadata"].get("rule_provenance_policy") else "archive_repair_run"
                record["metadata"][marker] = relative(run)
            atomic_json(vector, payload)
            records.extend(payload["records"])
            entry["vector"] = relative(vector)
            entry["vector_sha256"] = digest(vector)
        # This is an isolated delta index, never the shared dataset index.
        with (output_root / "index.jsonl").open("w", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        manifest["num_chunks"] = len(records)
        atomic_json(output_root / "manifest.json", manifest)
    atomic(run / "documents.json", docs)
    receipt(run, status="vectorized", vectorization_complete=True, searchable_members=sum("vector" in d for d in docs),
            excluded_members=[d["source_path"] for d in docs if "vector" not in d])


def checked_documents(run, include_vectors=True):
    docs = read(run / "documents.json")
    for doc in docs:
        if digest(inside(doc["staged"], run)) != doc["sha256"]:
            raise ValueError("Staged preprocessing changed")
        if include_vectors and "vector" in doc and digest(inside(doc["vector"], run)) != doc["vector_sha256"]:
            raise ValueError("Staged vectors changed")
    return docs


def apply(run, plan, after):
    require_finished(after)
    if not read(run / "receipt.json").get("vectorization_complete"):
        raise ValueError("Complete the isolated vectorize phase before applying")
    receipt(run, database_verified=False)
    check_inputs(plan)
    docs = checked_documents(run)
    from psycopg import sql
    from psycopg.errors import QueryCanceled, LockNotAvailable, DeadlockDetected
    from scripts.db import connect_postgres
    from scripts.vectorization.datasets import table_family
    from scripts.vectorization.load_to_supabase import (
        DEFAULT_EMBEDDING_MODEL, SOURCE_COLUMNS, DEPARTMENT_COLUMNS, CHUNK_COLUMNS,
        ensure_schema_ready, prepare_row, copy_upsert, preflight_index,
    )
    for dataset in {d["dataset"] for d in docs if "vector" in d}:
        preflight_index(run / "vectorized" / dataset / "index.jsonl", dataset,
                        body_documents={d["source_path"]: read(inside(d["staged"], run)) for d in docs})
    for doc in docs:
        if "vector" not in doc:
            # Never leave an older searchable DB row after publishing an empty
            # replacement. Newly recovered empty forms can simply stay excluded.
            from scripts.vectorization.load_to_supabase import record_identity
            family = table_family(doc["dataset"], read(inside(doc["staged"], run)).get("provenance"))
            sid = record_identity({"source_slug": doc["slug"], "chunk_index": 0}, doc["dataset"])[0]
            with connect_postgres() as conn:
                conn.execute("set transaction read only")
                found = conn.execute(sql.SQL("select id from {} where id=%s or file_path=%s").format(
                    sql.Identifier("public", family + "_sources")), (sid, doc["source_path"])).fetchone()
            if found:
                raise ValueError(f"Empty extraction would replace an existing DB source: {doc['source_path']}")
            continue
        records = read(inside(doc["vector"], run))["records"]
        body = read(inside(doc["staged"], run))
        rows = [prepare_row(r, DEFAULT_EMBEDDING_MODEL, doc["dataset"], body_document=body) for r in records]
        source = rows[0]["source"]
        sid = source["id"]
        family = table_family(doc["dataset"], records[0]["metadata"])
        aliases = {doc["source_path"], f"{doc['archive']}!/{doc['original_name'].replace(chr(92), '/')}"}
        with connect_postgres(autocommit=True) as conn:
            ensure_schema_ready(conn, family + "_sources", family + "_chunks")
            with conn.transaction(), conn.cursor() as cur:
                cur.execute("set local statement_timeout = '600s'")
                cur.execute("set local lock_timeout = '10s'")
                cur.execute("select pg_advisory_xact_lock(hashtext(%s))", (sid,))
                cur.execute(sql.SQL("select id,file_path,metadata->>'ingestion_dataset' from {} where id=%s or file_path=any(%s) for update").format(
                    sql.Identifier("public", family + "_sources")), (sid, list(aliases)))
                existing = cur.fetchall()
                if any(old_id != sid or path not in aliases or dataset != doc["dataset"] for old_id, path, dataset in existing):
                    raise ValueError(f"Conflicting existing source identity; no overwrite: {doc['source_path']}")
                columns = SOURCE_COLUMNS + DEPARTMENT_COLUMNS if family == "rag" else SOURCE_COLUMNS
                copy_upsert(cur, family + "_sources", "zip_repair_sources", columns, [source], ("id",), "processed_at")
                cur.execute(sql.SQL("delete from {} where source_id=%s").format(sql.Identifier("public", family + "_chunks")), (sid,))
                write_chunk_batches([r["chunk"] for r in rows], batch_size=32, max_retries=2,
                    savepoint=conn.transaction,
                    write=lambda part: copy_upsert(cur, family + "_chunks", "zip_repair_chunks", CHUNK_COLUMNS, part, ("source_id", "chunk_index"), "embedded_at"),
                    retryable=(QueryCanceled, LockNotAvailable, DeadlockDetected),
                    on_retry=lambda error, attempt, size: event("retry", source=sid, attempt=attempt, batch_size=size, error=str(error)))
        event("source_committed", source_id=sid, chunks=len(rows))
    receipt(run, status="loaded")
    verify(run)


def verify(run):
    if not read(run / "receipt.json").get("vectorization_complete"):
        raise ValueError("Complete vectorization before database verification")
    receipt(run, database_verified=False)
    from psycopg import sql
    from scripts.db import connect_postgres
    from scripts.vectorization.datasets import table_family
    from scripts.vectorization.load_to_supabase import DEFAULT_EMBEDDING_MODEL, prepare_row
    docs = checked_documents(run)
    count = chunks = 0
    with connect_postgres() as conn:
        conn.execute("set transaction isolation level repeatable read read only")
        for doc in docs:
            if "vector" not in doc:
                continue
            records = read(inside(doc["vector"], run))["records"]
            body = read(inside(doc["staged"], run))
            prepared = [prepare_row(r, DEFAULT_EMBEDDING_MODEL, doc["dataset"], body_document=body) for r in records]
            sid = prepared[0]["source"]["id"]
            family = table_family(doc["dataset"], records[0]["metadata"])
            source = conn.execute(sql.SQL("select file_path,metadata->>'archive_member_path',metadata->>'ingestion_dataset',metadata->>'archive_member_sha256',metadata->>'archive_member_ext',metadata->>'archive_path_policy_version' from {} where id=%s and status='active'").format(
                sql.Identifier("public", family + "_sources")), (sid,)).fetchone()
            provenance = read(inside(doc["staged"], run))["provenance"]
            if source != (doc["source_path"], doc["member"], doc["dataset"], provenance["archive_member_sha256"],
                          PurePosixPath(doc["member"]).suffix, str(ARCHIVE_PATH_POLICY_VERSION)):
                raise ValueError(f"Source provenance mismatch: {sid}")
            observed = conn.execute(sql.SQL("select chunk_index,content_hash,encode(sha256(convert_to(content,'UTF8')),'hex'),embedding_model,embedding_dim,embedding is not null from {} where source_id=%s").format(
                sql.Identifier("public", family + "_chunks")), (sid,)).fetchall()
            actual = {index: stored for index, stored, computed, model, dim, vector in observed
                      if stored == computed and model == DEFAULT_EMBEDDING_MODEL and dim == 384 and vector}
            expected = {r["chunk"]["chunk_index"]: r["chunk"]["content_hash"] for r in prepared}
            if len(observed) != len(expected) or actual != expected:
                raise ValueError(f"Chunk count/hash/embedding mismatch: {sid}")
            count += 1
            chunks += len(expected)
    receipt(run, status="verified", database_verified=True, verified_sources=count, verified_chunks=chunks)
    event("verified", sources=count, chunks=chunks)


def publication_backup_path(run, path):
    """Keep long backup names short while retaining their original path mapping."""
    key = relative(path)
    ledger_path = run / "backup_locations.json"
    ledger = read(ledger_path) if ledger_path.exists() else {}
    if key in ledger:
        return inside(ledger[key], run)
    backup = run / "backup" / key
    if len(str(backup.resolve())) >= 240:
        backup = run / "backup/_long_paths" / (hashlib.sha256(key.encode("utf-8")).hexdigest() + path.suffix)
        location = relative(backup)
        if key in ledger and ledger[key] != location:
            raise ValueError("Recovery backup mapping changed")
        if key not in ledger:
            ledger[key] = location
            atomic(ledger_path, ledger)
    return backup


def publish(run, plan, after, *, mark_complete=True):
    require_finished(after)
    if not read(run / "receipt.json").get("database_verified"):
        raise ValueError("Verify database contents before publishing local artifacts")
    docs = checked_documents(run)
    for archive in plan["archives"]:
        if digest(inside(archive["path"])) != archive["sha256"]:
            raise ValueError("ZIP changed before publication")
    # Keep all legacy bytes before installing any new canonical output. This also
    # handles a legacy *.pdf.json path occupied by a different JSON member.
    for doc in docs:
        for prior in doc["previous"]:
            path = inside(prior["path"])
            backup = publication_backup_path(run, path)
            if path.exists() and digest(path) == prior["sha256"]:
                backup.parent.mkdir(parents=True, exist_ok=True)
                if not backup.exists():
                    shutil.copy2(path, backup)
            elif not backup.exists() or digest(backup) != prior["sha256"]:
                raise ValueError(f"Legacy result changed; cannot publish: {path}")
    new_vector_targets = set()
    for doc in docs:
        target = inside(doc["target"], ROOT / "preprocessed" / doc["dataset"])
        if "vector" in doc:
            new_vector_targets.add(ROOT / "vectorized" / doc["dataset"] / target.relative_to(ROOT / "preprocessed" / doc["dataset"]))
    for doc in docs:
        for prior in doc["previous"]:
            old_preprocessed = inside(prior["path"], ROOT / "preprocessed" / doc["dataset"])
            old_vector = ROOT / "vectorized" / doc["dataset"] / old_preprocessed.relative_to(ROOT / "preprocessed" / doc["dataset"])
            if not old_vector.exists():
                continue
            backup = publication_backup_path(run, old_vector)
            backup.parent.mkdir(parents=True, exist_ok=True)
            if not backup.exists():
                shutil.copy2(old_vector, backup)
            expected_source = read(publication_backup_path(run, inside(prior["path"])))["source_path"]
            records = read(old_vector).get("records", [])
            if old_vector not in new_vector_targets and all(r.get("metadata", {}).get("source_path") == expected_source for r in records):
                old_vector.unlink()
    # Remove all verified legacy paths before writing any new target: one old
    # result can occupy another member's new extension-preserving location.
    for doc in docs:
        for prior in doc["previous"]:
            path = inside(prior["path"])
            if path.exists() and digest(path) == prior["sha256"]:
                path.unlink()
    for doc in docs:
        target = inside(doc["target"], ROOT / "preprocessed" / doc["dataset"])
        if target.exists() and read(target).get("source_path") != doc["source_path"]:
            raise ValueError(f"Refusing to replace another member: {target}")
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as handle:
            temporary = Path(handle.name)
        try:
            shutil.copy2(inside(doc["staged"], run), temporary)
            replace_with_retry(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        if "vector" in doc:
            vector_target = ROOT / "vectorized" / doc["dataset"] / target.relative_to(ROOT / "preprocessed" / doc["dataset"])
            vector_target.parent.mkdir(parents=True, exist_ok=True)
            payload = read(inside(doc["vector"], run))
            payload["source_signature"] = {"size": target.stat().st_size, "mtime_ns": target.stat().st_mtime_ns}
            if vector_target.exists():
                backup = publication_backup_path(run, vector_target)
                backup.parent.mkdir(parents=True, exist_ok=True)
                if not backup.exists():
                    shutil.copy2(vector_target, backup)
            atomic(vector_target, payload)
    from scripts.vectorization.load_to_supabase import iter_records, record_identity, preflight_index
    for dataset in sorted({d["dataset"] for d in docs}):
        selected = [d for d in docs if d["dataset"] == dataset]
        root = ROOT / "vectorized" / dataset
        root.mkdir(parents=True, exist_ok=True)
        path = root / "index.jsonl"
        signature = (path.stat().st_size, path.stat().st_mtime_ns) if path.exists() else None
        replaced = {record_identity({"source_slug": d["slug"], "chunk_index": 0}, dataset)[0] for d in selected}
        kept_ids, positions = set(), set()
        count = 0
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=root, delete=False) as handle:
            temporary = Path(handle.name)
            try:
                def write(record):
                    nonlocal count
                    sid, _ = record_identity(record, dataset)
                    key = (sid, int(record["chunk_index"]))
                    if key in positions:
                        raise ValueError("Duplicate source/chunk position while merging repaired index")
                    positions.add(key)
                    kept_ids.add(sid)
                    count += 1
                    handle.write(json.dumps(record, ensure_ascii=False) + "\n")
                if path.exists():
                    for record in iter_records(path):
                        if record_identity(record, dataset)[0] not in replaced:
                            write(record)
                for doc in selected:
                    if "vector" in doc:
                        for record in read(inside(doc["vector"], run))["records"]:
                            write(record)
            except BaseException:
                handle.close()
                temporary.unlink(missing_ok=True)
                raise
        current = (path.stat().st_size, path.stat().st_mtime_ns) if path.exists() else None
        if signature != current:
            temporary.unlink(missing_ok=True)
            raise ValueError("Shared index changed during publication; retry after the other job completes")
        backup = run / "backup/vectorized" / dataset
        backup.mkdir(parents=True, exist_ok=True)
        for existing in (path, root / "manifest.json"):
            if existing.exists() and not (backup / existing.name).exists():
                try:
                    os.link(existing, backup / existing.name)
                except OSError:
                    shutil.copy2(existing, backup / existing.name)
        replace_with_retry(temporary, path)
        manifest = read(root / "manifest.json") if (root / "manifest.json").exists() else read(run / "vectorized" / dataset / "manifest.json")
        manifest.update(num_chunks=count, num_files_indexed=len(kept_ids), index_path=relative(path),
                        input_root=f"preprocessed/{dataset}", output_root=f"vectorized/{dataset}",
                        archive_repair_run=relative(run), vectorized_at=datetime.now(timezone.utc).isoformat())
        atomic(root / "manifest.json", manifest)
        preflight_index(path, dataset)
        event("dataset_index_published", dataset=dataset, selected_sources=len(selected), total_chunks=count)
    for doc in docs:
        if digest(inside(doc["target"])) != doc["sha256"]:
            raise ValueError("Published preprocessing differs from the staged member")
    status = "complete" if mark_complete else "published_pending_search"
    receipt(run, status=status)
    event(status, affected_members=len(docs))


def main(argv=None):
    from scripts.recovery.cli import legacy_main
    return legacy_main("zip", argv)


if __name__ == "__main__":
    raise SystemExit(main())
