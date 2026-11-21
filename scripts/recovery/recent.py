"""Resume a frozen recent-ingestion selection in disjoint dataset processes.

The coordinator alone publishes receipts. Workers retain existing vector paths,
commit bounded batches, and checkpoint only after database commit.
"""
from __future__ import annotations

import argparse
import hashlib
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
import itertools
import json
import logging
import os
from pathlib import Path
import random
import subprocess
import sys
import tempfile
import time
import zipfile

from scripts.recovery.common import atomic, digest, event, inside, read, receipt
from scripts.vectorization.recovery_control import LoadOptions, recovery_lock

ROOT = Path(__file__).resolve().parents[2]
POLICY = "recent-ingestion-parallel-resume-v1"


def relative(path):
    return Path(path).resolve().relative_to(ROOT).as_posix()


def partition(tasks, workers):
    """Keep primary/supplementary versions of a dataset on the same worker."""
    if not 1 <= workers <= 4:
        raise ValueError("workers must be between 1 and 4")
    groups = {}
    for task in tasks:
        groups.setdefault(task["dataset"], []).append(task)
    assigned, weights = [[] for _ in range(workers)], [0] * workers
    ordered = sorted(groups.items(), key=lambda item: (
        item[0] != "ee", -sum(len(t["output_files"]) for t in item[1]), item[0]))
    for _, group in ordered:
        slot = min(range(workers), key=lambda i: (weights[i], i))
        assigned[slot].extend(sorted(group, key=lambda t: t["phase"] != "primary"))
        weights[slot] += sum(len(t["output_files"]) for t in group)
    return assigned


def prepare(run, source_run, workers):
    if source_run is None:
        raise ValueError("--source-run must identify the failed recent ingestion")
    source_run = inside(source_run, root=ROOT, boundary=ROOT / "var/recovery")
    legacy_execution = read(source_run / "execution.json")
    primary_path = inside(legacy_execution.get("primary_receipt", "vectorized/_/ingestion.json"), root=ROOT)
    primary = read(primary_path)
    master_path = inside(primary["plan"], root=ROOT)
    master = read(master_path)
    if master.get("status") != "preprocessed":
        raise ValueError("Finish preprocessing before resuming ingestion")
    if legacy_execution.get("status") not in {"failed", "needs_attention", "interrupted"}:
        raise ValueError("Source execution is not a stopped/failed run")
    done = {n for n, v in primary["datasets"].items() if v.get("status") == "complete"}
    if not done.issubset(master["preprocessing"]):
        raise ValueError("Completed receipt contains datasets outside preprocessing selection")
    tasks = []
    for name, info in master["preprocessing"].items():
        if name not in done:
            tasks.append({"key": "primary_" + name, "phase": "primary", "dataset": name,
                          "output_files": info["output_files"], "preprocessing_errors": info.get("errors", [])})
    zip_receipt = read(source_run / "zip_retry.json")
    if zip_receipt.get("status") != "preprocessed":
        raise ValueError("Finish ZIP recovery before resuming")
    supplement = read(inside(zip_receipt["plan"], root=ROOT))
    structured = read(source_run / "structured_main.json")
    supplement["preprocessing"]["pknu_main"] = {
        "output_files": [d["output"] for d in structured["documents"]], "errors": []}
    for name, info in supplement["preprocessing"].items():
        tasks.append({"key": "supplement_" + name, "phase": "supplement", "dataset": name,
                      "output_files": info["output_files"], "preprocessing_errors": info.get("errors", [])})
    if not tasks:
        raise ValueError("No remaining ingestion tasks")
    for task in tasks:
        boundary = ROOT / "preprocessed" / task["dataset"]
        if len(task["output_files"]) != len(set(task["output_files"])):
            raise ValueError("Repeated preprocessing output in selection")
        for declared in task["output_files"]:
            path = inside(declared, root=ROOT, boundary=boundary)
            if not path.is_file():
                raise FileNotFoundError(path)
    supplement_path = ROOT / "vectorized/z/ingestion.json"
    if supplement_path.exists():
        raise ValueError("Existing supplementary receipt needs separate reconciliation")
    atomic(run / "prior_primary.json", primary)
    atomic(run / "prior_execution.json", legacy_execution)
    plan = {"policy": POLICY, "source_run": relative(source_run), "primary_receipt": relative(primary_path),
            "master_plan": relative(master_path), "workers": workers, "tasks": tasks,
            "assignments": [[t["key"] for t in part] for part in partition(tasks, workers)],
            "completed_before_resume": sorted(done), "prepared_at": datetime.now(timezone.utc).isoformat()}
    atomic(run / "plan.json", plan)
    receipt(run, status="prepared", remaining_primary_datasets=len(master["preprocessing"]) - len(done),
            supplementary_datasets=len(supplement["preprocessing"]), workers=workers)
    event("recent_prepared", workers=workers, primary=len(master["preprocessing"]) - len(done), supplement=len(supplement["preprocessing"]))


class CachedEmbedder:
    def __init__(self):
        from scripts.vectorization.common import DEFAULT_SENTENCE_MODEL
        self.name = "sentence-transformers:" + DEFAULT_SENTENCE_MODEL
        self.dimensions = 384
        self.cache, self.model, self.connection = {}, None, None
        self.generated = self.reused = 0

    def embed_texts(self, texts):
        missing = list(dict.fromkeys(t for t in texts if t not in self.cache))
        if missing:
            from scripts.vectorization.common import SentenceTransformerEmbedder
            if self.model is None:
                self.model = SentenceTransformerEmbedder()
            self.cache.update(zip(missing, self.model.embed_texts(missing)))
            self.generated += len(missing)
        self.reused += len(texts) - len(missing)
        return [self.cache[t] for t in texts]

    def prime(self, path, dataset, input_root):
        from psycopg import sql
        from scripts.db import connect_postgres
        from scripts.vectorization import vectorization as v
        from scripts.vectorization.datasets import table_family
        from scripts.vectorization.load_to_supabase import record_identity, remove_nul_bytes, sha256_text
        self.cache = {}
        prior = v.output_path_for(path, input_root, ROOT / "vectorized" / dataset)
        if prior.is_file() and prior.stat().st_size < 128 * 1024 * 1024:
            old = read(prior)
            if old.get("embedding_backend") == self.name and old.get("embedding_dimensions") == 384:
                self.cache = {r["text"]: r["embedding"] for r in old.get("records", [])
                              if r.get("embedding_model") == self.name and r.get("embedding_dim") == 384}
        grouped = {}
        for record in v.extract_chunk_records(read(path), path, ROOT):
            text = record["text"]
            if text not in self.cache and text == remove_nul_bytes(text):
                grouped.setdefault(table_family(dataset, record.get("metadata")), {})[
                    record_identity(record, dataset)[1]] = (text, sha256_text(text))
        if not grouped:
            return
        try:
            if self.connection is None or self.connection.closed:
                self.connection = connect_postgres(autocommit=True)
            with self.connection.cursor() as cur:
                cur.execute("set statement_timeout='30s'")
                for family, expected in grouped.items():
                    ids = list(expected)
                    for start in range(0, len(ids), 256):
                        cur.execute(sql.SQL("select id,content_hash,content,embedding::text from public.{} "
                                            "where id=any(%s) and embedding_model=%s and embedding_dim=384").format(
                                                sql.Identifier(family + "_chunks")), (ids[start:start+256], self.name))
                        for cid, saved_hash, body, encoded in cur.fetchall():
                            text, expected_hash = expected[cid]
                            if saved_hash == expected_hash and sha256_text(body) == expected_hash and encoded:
                                self.cache.setdefault(text, json.loads(encoded))
        except Exception as error:
            logging.warning("DB vector cache unavailable: %s", type(error).__name__)
            if self.connection is not None:
                self.connection.close()
            self.connection = None

    def close(self):
        if self.connection is not None:
            self.connection.close()


@contextmanager
def writer_slot(conn):
    """At most two new-job DB write transactions, with four embedding workers."""
    acquired = False
    with conn.cursor() as cur:
        deadline = time.monotonic() + 1200
        while not acquired:
            for slot in ((os.getpid() % 2), (1 - os.getpid() % 2)):
                cur.execute("select pg_try_advisory_xact_lock(hashtextextended(%s,0))",
                            ("campus-recent-ingest-writer-" + str(slot),))
                if cur.fetchone()[0]:
                    acquired = True
                    break
            if not acquired:
                if time.monotonic() > deadline:
                    raise TimeoutError("Timed out waiting for an ingestion write slot")
                time.sleep(0.25 + random.random() * 0.25)
    yield


def write_rows(conn, rows, timeout):
    from psycopg import sql
    from scripts.vectorization.load_to_supabase import SOURCE_COLUMNS, DEPARTMENT_COLUMNS, CHUNK_COLUMNS, copy_upsert
    with conn.transaction(), writer_slot(conn), conn.cursor() as cur:
        cur.execute("select set_config('statement_timeout',%s,true)", (str(timeout * 1000),))
        cur.execute("set local lock_timeout='15s'")
        for family in sorted({r["family"] for r in rows}):
            group = [r for r in rows if r["family"] == family]
            sources = list({r["source"]["id"]: r["source"] for r in group}.values())
            columns = SOURCE_COLUMNS + DEPARTMENT_COLUMNS if family == "rag" else SOURCE_COLUMNS
            copy_upsert(cur, family + "_sources", "recent_resume_sources", columns, sources, ("id",), "processed_at")
            target, stage = sql.Identifier("public", family + "_chunks"), sql.Identifier("pg_temp", "recent_resume_chunks")
            names = sql.SQL(",").join(map(sql.Identifier, CHUNK_COLUMNS))
            cur.execute(sql.SQL("create temporary table if not exists recent_resume_chunks on commit delete rows "
                                "as select {} from {} with no data").format(names, target))
            cur.execute("truncate pg_temp.recent_resume_chunks")
            with cur.copy(sql.SQL("copy {} ({}) from stdin").format(stage, names)) as cp:
                for row in group:
                    cp.write_row(tuple(row["chunk"][c] for c in CHUNK_COLUMNS))
            assignments = sql.SQL(",").join(sql.SQL("{}=excluded.{}").format(sql.Identifier(c), sql.Identifier(c))
                                           for c in CHUNK_COLUMNS if c not in {"source_id", "chunk_index"})
            compared = [c for c in CHUNK_COLUMNS if c not in {"embedding", "embedded_at", "metadata"}]
            left = sql.SQL(",").join(sql.Identifier("existing", c) for c in compared)
            right = sql.SQL(",").join(sql.Identifier("excluded", c) for c in compared)
            cur.execute(sql.SQL("""insert into {} as existing ({}) select {} from {} order by source_id,chunk_index
                on conflict(source_id,chunk_index) do update set {}
                where ({},existing.metadata #- '{{crawl,crawled_at}}')
                is distinct from ({},excluded.metadata #- '{{crawl,crawled_at}}')""").format(
                    target, names, names, stage, assignments, left, right))


def checkpoint_offset(checkpoint, fingerprint, total):
    if checkpoint.get("index_sha256") != fingerprint:
        return 0
    offset = checkpoint.get("committed_records", 0)
    if type(offset) is not int or not 0 <= offset <= total:
        raise ValueError("Invalid committed-record checkpoint")
    return offset


def load_index(index, task, run, options, progress):
    import psycopg
    from scripts.db import connect_postgres
    from scripts.vectorization.datasets import table_family
    from scripts.vectorization.load_to_supabase import iter_records, preflight_index, prepare_row
    dataset = task["dataset"]
    model = preflight_index(index, dataset)
    manifest = read(index.with_name("manifest.json"))
    total, fingerprint = manifest["num_chunks"], digest(index)
    checkpoint = run / "checkpoints" / (task["key"] + ".json")
    previous = read(checkpoint) if checkpoint.exists() else {}
    offset = checkpoint_offset(previous, fingerprint, total)
    records = itertools.islice(iter_records(index), offset, None)
    conn = None
    size = options.chunk_batch_size
    retryable = (psycopg.OperationalError, psycopg.errors.DeadlockDetected,
                 psycopg.errors.QueryCanceled, psycopg.errors.LockNotAvailable,
                 psycopg.errors.SerializationFailure)
    try:
        while offset < total:
            part = list(itertools.islice(records, size))
            if not part:
                raise ValueError("Index ended before the manifest record count")
            rows = [{**prepare_row(r, model, dataset), "family": table_family(dataset, r.get("metadata"))} for r in part]
            pending = rows
            failures = 0
            while pending:
                count = min(size, len(pending))
                try:
                    if conn is None or conn.closed:
                        conn = connect_postgres(autocommit=True)
                    write_rows(conn, pending[:count], options.statement_timeout_seconds)
                except retryable as error:
                    failures += 1
                    if conn is not None:
                        conn.close()
                    conn = None
                    size = max(1, size // 2)
                    progress(stage="retrying", checked=offset, total=total, batch_size=size,
                             retry=failures, error=type(error).__name__)
                    if failures > options.max_batch_retries:
                        raise
                    time.sleep(min(2 ** failures, 30) + random.random())
                    continue
                offset += count
                pending = pending[count:]
                failures = 0
                atomic(checkpoint, {"index_sha256": fingerprint, "index": relative(index),
                                    "committed_records": offset, "total": total,
                                    "updated_at": datetime.now(timezone.utc).isoformat()})
                progress(stage="loading", checked=offset, total=total, batch_size=size)
    finally:
        if conn is not None:
            conn.close()
    return total, model


def verify_and_cleanup(index, dataset, model, timeout, progress):
    from psycopg import sql
    from scripts.db import connect_postgres
    from scripts.vectorization.datasets import table_family
    from scripts.vectorization.load_to_supabase import iter_records, record_identity, remove_nul_bytes, sha256_text
    with connect_postgres(autocommit=True) as conn:
        conn.execute("select set_config('statement_timeout',%s,false)", (str(timeout * 1000),))
        conn.execute("create temporary table recent_expected(id text,source_id text,family text,content_hash text,content text)")
        families = Counter()
        with conn.cursor().copy("copy recent_expected(id,source_id,family,content_hash,content) from stdin") as cp:
            for record in iter_records(index):
                sid, cid = record_identity(record, dataset)
                family = table_family(dataset, record.get("metadata"))
                text = remove_nul_bytes(record.get("text") or record.get("content") or "")
                cp.write_row((cid, sid, family, sha256_text(text), text))
                families[family] += 1
        conn.execute("create index on recent_expected(family,id)")
        conn.execute("create index on recent_expected(family,source_id)")
        conn.execute("analyze recent_expected")
        removed, result = 0, {}
        for family in sorted(families):
            ids = [r[0] for r in conn.execute("select distinct source_id from recent_expected where family=%s order by source_id", (family,))]
            for start in range(0, len(ids), 16):
                with conn.transaction(), writer_slot(conn):
                    conn.execute("set local lock_timeout='15s'")
                    removed += conn.execute(sql.SQL("delete from public.{} c where c.source_id=any(%s) "
                        "and not exists(select 1 from recent_expected e where e.family=%s and e.id=c.id)").format(
                            sql.Identifier(family + "_chunks")), (ids[start:start+16], family)).rowcount
            checked, present, matched = conn.execute(sql.SQL(
                "select count(*),count(c.id),count(*) filter(where c.content_hash=e.content_hash "
                "and c.content=e.content and c.embedding_model=%s and c.embedding_dim=384) "
                "from recent_expected e left join public.{} c on c.id=e.id where e.family=%s").format(
                    sql.Identifier(family + "_chunks")), (model, family)).fetchone()
            result[family] = {"expected": checked, "present": present, "matched": matched}
            if not checked == present == matched:
                raise ValueError("Database verification mismatch: " + str(result))
        progress(stage="verified", verification=result)
        return result, removed


def archive_alias_selection(paths, input_root, output_root, embedder):
    """Retain canonical ZIP members only after proving a legacy alias is identical."""
    from scripts.preprocessing.extractors.common import SUPPORTED_EXTS
    from scripts.preprocessing.file_preprocessing import decoded_archive_member_name, is_safe_archive_member
    from scripts.vectorization import vectorization as v
    selected = {p.resolve(): p for p in paths}
    excluded, aliases = set(), []
    storage_fields = {"preprocessed_path", "previous_preprocessed_path", "previous_source_path",
                      "archive_path_policy_version", "archive_member_sha256", "archive_member_ext"}
    for canonical in paths:
        suffixes = canonical.suffixes
        if "zip" not in canonical.relative_to(input_root).parts or len(suffixes) < 2 or suffixes[-2].lower() not in SUPPORTED_EXTS:
            continue
        legacy = canonical.with_suffix("").with_suffix(".json").resolve()
        if legacy not in selected or legacy == canonical.resolve():
            continue
        current = v.current_payload(canonical, input_root, output_root, embedder)
        old = v.current_payload(selected[legacy], input_root, output_root, embedder)
        if not current or not old or not current.get("records"):
            raise ValueError("Missing current vector payload for possible ZIP alias")
        metadata = current["records"][0].get("metadata") or {}
        previous = metadata.get("previous_preprocessed_path")
        if not previous or inside(previous, root=ROOT, boundary=input_root) != legacy:
            continue
        source = metadata.get("source_path", "")
        raw, separator, member = source.partition("!/")
        if (metadata.get("archive_path_policy_version") != 2 or not separator
                or metadata.get("previous_source_path") != source or not is_safe_archive_member(member)
                or len(current["records"]) != len(old["records"])):
            raise ValueError("ZIP alias identity is not proven")
        for first, second in zip(current["records"], old["records"]):
            left, right = first.get("metadata", {}), second.get("metadata", {})
            a, b = first.get("embedding", []), second.get("embedding", [])
            same_vector = bool(a) and len(a) == len(b) and all(abs(x-y) <= 1e-7 for x,y in zip(a,b))
            if ({k: value for k, value in first.items() if k not in {"metadata", "embedding"}}
                    != {k: value for k, value in second.items() if k not in {"metadata", "embedding"}}
                    or not same_vector
                    or left.get("source_path") != source or right.get("source_path") != source
                    or {k: value for k, value in left.items() if k not in storage_fields}
                    != {k: value for k, value in right.items() if k not in storage_fields}):
                raise ValueError("ZIP alias has conflicting content, vectors or semantic metadata")
        archive = inside(raw, root=ROOT, boundary=ROOT / "files")
        with zipfile.ZipFile(archive) as zf:
            matches = [info for info in zf.infolist()
                       if decoded_archive_member_name(info).replace("\\", "/") == member]
            if len(matches) != 1:
                raise ValueError("ZIP original member is missing or ambiguous")
            with zf.open(matches[0]) as stream:
                original_hash = hashlib.file_digest(stream, "sha256").hexdigest()
        if original_hash != metadata.get("archive_member_sha256"):
            raise ValueError("ZIP original member changed; do not collapse the alias")
        if legacy in excluded:
            raise ValueError("Multiple canonical ZIP members claim the same legacy alias")
        excluded.add(legacy)
        aliases.append({"canonical": relative(canonical), "legacy_alias": relative(legacy),
                        "source_path": source, "original_member_sha256": original_hash,
                        "duplicate_records": len(old["records"])})
    return [p for p in paths if p.resolve() not in excluded], aliases


def scoped_index(paths, input_root, output_root, embedder, dataset):
    """Index exactly the frozen selection, preserving other per-file vectors."""
    from scripts.vectorization import vectorization as v
    from scripts.vectorization.recovery_control import replace_with_retry
    paths, aliases = archive_alias_selection(paths, input_root, output_root, embedder)
    output_root.mkdir(parents=True, exist_ok=True)
    index = output_root / "index.jsonl"
    temporary = None
    count = 0
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=output_root, delete=False) as stream:
            temporary = Path(stream.name)
            for path in paths:
                payload = v.current_payload(path, input_root, output_root, embedder)
                if payload is None:
                    raise ValueError("Vectorized selection changed before indexing: " + relative(path))
                for record in payload["records"]:
                    stream.write(json.dumps(record, ensure_ascii=False) + "\n")
                    count += 1
        replace_with_retry(temporary, index)
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)
    manifest = {"dataset": dataset, "vectorized_at": datetime.now(timezone.utc).isoformat(),
                "embedding_backend": embedder.name, "embedding_dimensions": embedder.dimensions,
                "num_files_failed": 0, "failures": [], "num_files_indexed": len(paths),
                "num_chunks": count, "index_path": relative(index), "selection_scoped": True,
                "deduplicated_archive_aliases": aliases}
    atomic(output_root / "manifest.json", manifest)
    return manifest


def process_task(task, run, options, embedder, progress):
    from scripts.vectorization import vectorization as v
    name, key = task["dataset"], task["key"]
    input_root = ROOT / "preprocessed" / name
    output_root = ROOT / "vectorized" / ("_" if task["phase"] == "primary" else "z") / name
    paths = [inside(p, root=ROOT, boundary=input_root) for p in task["output_files"]]
    valid, skipped, failures = [], [], []
    generated, reused = embedder.generated, embedder.reused
    progress(stage="vectorizing", files=len(paths), done=0)
    for position, path in enumerate(paths, 1):
        try:
            if not v.is_vectorized_current(path, input_root, output_root, embedder):
                embedder.prime(path, name, input_root)
                v.vectorize_file(path, input_root, output_root, ROOT, embedder, 64, None)
            valid.append(path)
        except v.NoChunksError as error:
            skipped.append({"path": relative(path), "reason": str(error)})
        except Exception as error:
            failures.append({"path": relative(path), "error": str(error)})
            logging.exception("Vectorization failed: %s", path)
        if position % 100 == 0:
            progress(stage="vectorizing", files=len(paths), done=position, generated=embedder.generated-generated,
                     reused=embedder.reused-reused, vector_failures=len(failures))
    if failures:
        atomic(run / "tasks" / (key + ".json"), {"status": "failed", "failures": failures, "skipped": skipped})
        raise ValueError(f"{len(failures)} vector files failed; preserve outputs for retry")
    summary = scoped_index(valid, input_root, output_root, embedder, name)
    if summary.get("num_files_failed"):
        raise ValueError("Vector index has failures")
    index = output_root / "index.jsonl"
    row = {"status": "vectorized", "files": len(paths), "valid_files": len(valid), "skipped": skipped,
           "failures": [], "preprocessing_errors": task.get("preprocessing_errors", []),
           "generated_chunks": embedder.generated-generated, "reused_chunks": embedder.reused-reused,
           "index": relative(index), "chunks": summary.get("num_chunks", 0),
           "indexed_files": summary["num_files_indexed"],
           "deduplicated_archive_aliases": summary.get("deduplicated_archive_aliases", [])}
    atomic(run / "tasks" / (key + ".json"), row)
    if row["chunks"]:
        row["loaded"], model = load_index(index, task, run, options, progress)
        progress(stage="verifying", total=row["loaded"])
        row["verification"], row["removed_obsolete_chunks"] = verify_and_cleanup(
            index, name, model, options.statement_timeout_seconds, progress)
    else:
        row.update(loaded=0, verification={})
    row.update(status="complete", finished_at=datetime.now(timezone.utc).isoformat())
    atomic(run / "tasks" / (key + ".json"), row)
    return row


def worker(run, number):
    import torch
    torch.set_num_threads(2)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    plan, config = read(run / "plan.json"), read(run / "options.json")
    options = LoadOptions(**config)
    tasks = {t["key"]: t for t in plan["tasks"]}
    assigned = plan["assignments"][number]
    state = {"status": "running", "pid": os.getpid(), "worker": number, "finished": [], "failed": {}}
    state_path = run / (f"worker_{number}.json")
    def progress(**values):
        state["progress"] = values
        state["heartbeat_at"] = datetime.now(timezone.utc).isoformat()
        atomic(state_path, state)
        event("recent_progress", worker=number, task=state.get("active"), progress=values)
    embedder = CachedEmbedder()
    try:
        with recovery_lock(run / f".worker_{number}.lock"):
            for attempt in range(2):
                for key in assigned:
                    result_path = run / "tasks" / (key + ".json")
                    if result_path.exists() and read(result_path).get("status") == "complete":
                        if key not in state["finished"]:
                            state["finished"].append(key)
                        state["failed"].pop(key, None)
                        continue
                    state["active"] = key
                    try:
                        process_task(tasks[key], run, options, embedder, progress)
                    except Exception as error:
                        logging.exception("Task failed: %s", key)
                        state["failed"][key] = str(error)
                        progress(stage="task_failed", error=str(error), attempt=attempt+1)
                    else:
                        state["finished"].append(key)
                        state["failed"].pop(key, None)
                        progress(stage="task_complete")
                if not state["failed"]:
                    break
            state.update(status="needs_attention" if state["failed"] else "complete", active=None)
            atomic(state_path, state)
    except BaseException as error:
        state.update(status="interrupted", error=str(error))
        atomic(state_path, state)
        raise
    finally:
        embedder.close()


def merge_receipts(run, plan, states, primary, supplement):
    tasks = {t["key"]: t for t in plan["tasks"]}
    for state in states:
        for key in state.get("finished", []):
            row = read(run / "tasks" / (key + ".json"))
            if row.get("status") != "complete":
                continue
            task = tasks[key]
            target = primary if task["phase"] == "primary" else supplement
            target["datasets"][task["dataset"]] = row
    active = [tasks[s["active"]]["dataset"] for s in states if s.get("active") in tasks]
    primary.update(status="running_parallel", active_dataset=None, active_datasets=active, resume_run=relative(run))
    supplement.update(status="running_parallel", resume_run=relative(run))
    atomic(inside(plan["primary_receipt"], root=ROOT), primary)
    atomic(ROOT / "vectorized/z/ingestion.json", supplement)


def refresh_priorities(run, families, timeout):
    """Avoid a whole-table department aggregation; checkpoint bounded source batches."""
    from psycopg import sql
    from psycopg.rows import dict_row
    from scripts.db import connect_postgres
    from scripts.vectorization.load_to_supabase import refresh_priority_scores
    from scripts.priority.department.update_priorities import (
        fetch_rule_contents, fetch_main_contents, build_rule_feature_set,
        build_reference_feature_set, build_updates,
    )
    path = run / "priorities.json"
    state = read(path) if path.exists() else {}
    for family in families:
        if state.get(family, {}).get("status") == "complete":
            continue
        event("recent_priorities", family=family)
        if family != "rag":
            refresh_priority_scores(family)
            state[family] = {"status": "complete", "completed_at": datetime.now(timezone.utc).isoformat()}
            atomic(path, state)
            continue
        with connect_postgres(row_factory=dict_row, autocommit=True) as conn:
            conn.execute("select set_config('statement_timeout',%s,false)", (str(timeout * 1000),))
            conn.execute("set lock_timeout='15s'")
            rule_features = build_rule_feature_set(fetch_rule_contents(conn))
            main_features = build_reference_feature_set(fetch_main_contents(conn))
            fingerprint = hashlib.sha256(json.dumps([sorted(rule_features), sorted(main_features)],
                                                   ensure_ascii=False).encode("utf-8")).hexdigest()
            current = state.get(family, {})
            if current.get("reference_sha256") != fingerprint:
                current = {"status": "running", "reference_sha256": fingerprint,
                           "last_source_id": "", "checked_sources": 0, "changed_sources": 0}
            conn.execute("create temporary table recent_priority_updates(id text, score double precision, details jsonb) on commit delete rows")
            while True:
                documents = conn.execute("""
                    select s.id,s.title,s.metadata,
                      coalesce((select string_agg(c.content,E'\\n\\n' order by c.chunk_index)
                                from public.rag_chunks c where c.source_id=s.id),'') as content
                    from public.rag_sources s
                    where s.status='active' and s.id>%s
                    order by s.id limit 128
                """, (current["last_source_id"],)).fetchall()
                if not documents:
                    break
                updates = build_updates(documents, rule_features, main_features)
                with conn.transaction(), conn.cursor() as cur:
                    cur.execute("truncate recent_priority_updates")
                    with cur.copy("copy recent_priority_updates(id,score,details) from stdin") as cp:
                        for item in updates:
                            cp.write_row((item["id"], item["priority_score"], item["priority_details"]))
                    cur.execute("""update public.rag_sources s
                        set priority_score=p.score,priority_details=p.details,priority_updated_at=now()
                        from recent_priority_updates p where s.id=p.id
                        and (s.priority_score,s.priority_details) is distinct from (p.score,p.details)""")
                    changed = cur.rowcount
                current.update(last_source_id=documents[-1]["id"],
                               checked_sources=current["checked_sources"]+len(documents),
                               changed_sources=current["changed_sources"]+changed,
                               updated_at=datetime.now(timezone.utc).isoformat())
                state[family] = current
                atomic(path, state)
                event("recent_priority_batch", family=family, checked=current["checked_sources"], changed=current["changed_sources"])
            current.update(status="complete", completed_at=datetime.now(timezone.utc).isoformat())
            state[family] = current
            atomic(path, state)


def resume(run, options):
    from scripts.vectorization.load_to_supabase import refresh_priority_scores
    plan = read(run / "plan.json")
    if options.workers != plan["workers"]:
        raise ValueError("Worker count must match the prepared assignments")
    if read(run / "receipt.json").get("status") == "complete":
        event("recent_already_complete", run=relative(run))
        return
    source_run = inside(plan["source_run"], root=ROOT)
    with recovery_lock(source_run / ".parallel_resume.lock"):
        atomic(run / "options.json", vars(options))
        primary = read(inside(plan["primary_receipt"], root=ROOT))
        supplement_path = ROOT / "vectorized/z/ingestion.json"
        supplement = read(supplement_path) if supplement_path.exists() else {
            "started_at": datetime.now(timezone.utc).isoformat(), "plan": relative(run / "plan.json"), "datasets": {}}
        if supplement.get("plan") != relative(run / "plan.json"):
            raise ValueError("Supplement namespace belongs to a different operation")
        processes, streams = [], []
        for number in range(options.workers):
            stream = (ROOT / "logs" / (run.name + f"_worker_{number}.log")).open("a", encoding="utf-8")
            streams.append(stream)
            processes.append(subprocess.Popen([sys.executable, "-u", "-X", "utf8", "-m", __name__,
                                                "--worker", str(number), "--run", str(run)],
                                               cwd=ROOT, stdout=stream, stderr=stream))
        receipt(run, status="running", workers=options.workers, pids=[p.pid for p in processes])
        try:
            while True:
                states = []
                for number, process in enumerate(processes):
                    path = run / f"worker_{number}.json"
                    state = read(path) if path.exists() else {"status": "starting"}
                    if process.poll() is not None and state.get("status") not in {"complete", "needs_attention"}:
                        state.update(status="interrupted", error="Worker process exited before its completion receipt")
                    states.append(state)
                merge_receipts(run, plan, states, primary, supplement)
                done = sum(v.get("status") == "complete" for v in primary["datasets"].values())
                receipt(run, status="running", workers=states, primary_completed=done,
                                remaining_primary_datasets=len(plan["completed_before_resume"])+sum(t["phase"]=="primary" for t in plan["tasks"])-done,
                                primary_verified_chunks=sum(v.get("loaded", 0) for v in primary["datasets"].values() if v.get("status") == "complete"),
                                supplementary_completed=len(supplement["datasets"]))
                state = read(run / "receipt.json")
                atomic(source_run / "execution.json", {**state, "resume_run": relative(run),
                    "primary_receipt": plan["primary_receipt"], "supplement_receipt": "vectorized/z/ingestion.json"})
                if all(p.poll() is not None for p in processes):
                    break
                time.sleep(5)
            if any(s.get("status") != "complete" for s in states):
                raise RuntimeError("Some workers need attention; see worker receipts and committed batch checkpoints")
            expected = set(read(inside(plan["master_plan"], root=ROOT))["preprocessing"])
            if {n for n,v in primary["datasets"].items() if v.get("status") == "complete"} != expected:
                raise ValueError("Primary completion receipt does not cover every selected dataset")
            receipt(run, status="refreshing_priorities")
            families = sorted({f for record in (primary, supplement) for row in record["datasets"].values()
                               for f in row.get("verification", {})})
            refresh_priorities(run, families, options.statement_timeout_seconds)
            for target, path in ((primary, inside(plan["primary_receipt"], root=ROOT)), (supplement, supplement_path)):
                target.update(status="complete", active_dataset=None, active_datasets=[], finished_at=datetime.now(timezone.utc).isoformat())
                atomic(path, target)
            receipt(run, status="complete", primary_completed=len(expected), supplementary_completed=len(supplement["datasets"]),
                            remaining_primary_datasets=0, remaining_supplementary_datasets=0,
                            primary_verified_chunks=sum(r.get("loaded",0) for r in primary["datasets"].values()),
                            supplementary_verified_chunks=sum(r.get("loaded",0) for r in supplement["datasets"].values()),
                            priorities_refreshed=True, database_verified=True)
            state = read(run / "receipt.json")
            atomic(source_run / "execution.json", {**state, "resume_run": relative(run),
                "primary_receipt": plan["primary_receipt"], "supplement_receipt": "vectorized/z/ingestion.json"})
        except BaseException as error:
            for process in processes:
                if process.poll() is None:
                    process.wait()
            receipt(run, status="needs_attention", error=str(error))
            state = read(run / "receipt.json")
            atomic(source_run / "execution.json", {**state, "resume_run": relative(run)})
            raise
        finally:
            for stream in streams:
                stream.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Internal dataset worker for the recent recovery CLI")
    parser.add_argument("--worker", type=int, required=True)
    parser.add_argument("--run", type=Path, required=True)
    args = parser.parse_args()
    worker(args.run.resolve(), args.worker)
