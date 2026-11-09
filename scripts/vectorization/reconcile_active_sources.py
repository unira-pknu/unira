"""Operator-run audit and reversible deactivation of proven duplicate active sources.

DB-only sources are retained unless independently proven exact duplicates.
Importing this module performs no queries, model loading, or synchronization.
"""
from __future__ import annotations

from collections import defaultdict
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.recovery.common import read, atomic, digest, event as emit, receipt, inside as resolve_inside
from scripts.artifact_paths import reference_path
from scripts.vectorization.reconcile_ce import fingerprint

POLICY = "active-source-exact-duplicates-v1"
IDENTITY_FIELDS = ("document_id", "source_id", "attachment_id", "attachment_name", "attachment_sha256",
                   "archive_member_path", "archive_member_sha256", "source_site", "document_kind",
                   "year", "term", "term_code", "course_no", "section", "variant", "revision",
                   "effective_at", "published_at")


def inside(path):
    return resolve_inside(path, root=ROOT)


def original_identity(source):
    metadata = source.get("metadata") or {}
    return {"file_path": str(source.get("file_path") or "").replace("\\", "/"),
            "dataset": metadata.get("ingestion_dataset"), "title": source.get("title"),
            "url": source.get("url"), "parent_url": source.get("parent_url"),
            "file_ext": source.get("file_ext"),
            **{key: metadata.get(key) for key in IDENTITY_FIELDS}}


def duplicate_signature(snapshot):
    """Return no signature when evidence cannot establish equivalence safely."""
    from scripts.vectorization.load_to_supabase import DEFAULT_EMBEDDING_MODEL
    identity = original_identity(snapshot["source"])
    if not identity["file_path"] or not identity["dataset"]:
        return None
    if not any(identity.get(key) for key in ("document_id", "source_id", "attachment_id")):
        return None  # Same pathname and bytes alone cannot distinguish document revisions.
    chunks = snapshot["chunks"]
    if not chunks or len({c["chunk_index"] for c in chunks}) != len(chunks):
        return None
    hashes = []
    for chunk in sorted(chunks, key=lambda c: c["chunk_index"]):
        actual = hashlib.sha256(chunk["content"].encode("utf-8")).hexdigest()
        if (chunk["content_hash"] != actual or chunk["embedding"] is None
                or chunk["embedding_dim"] != 384 or chunk["embedding_model"] != DEFAULT_EMBEDDING_MODEL):
            return None
        from scripts.vectorization.common import validate_vectors
        try:
            vector = json.loads(chunk["embedding"]) if isinstance(chunk["embedding"], str) else chunk["embedding"]
            validate_vectors([vector], 1, 384)
        except (TypeError, ValueError):
            return None
        hashes.append((chunk["chunk_index"], actual))
    return fingerprint({"identity": identity, "chunks": hashes})


def choose_representative(snapshots, indexed):
    signatures = [duplicate_signature(value) for value in snapshots]
    if not signatures or None in signatures or len(set(signatures)) != 1:
        return None
    # Identical source evidence and ordered body hashes first; timestamps never
    # make different editions equivalent. Prefer an already indexed identity.
    return min((s["source"]["id"] for s in snapshots), key=lambda sid: (sid not in indexed, sid))


def validate_deactivation(prior, current):
    if current.get("status") != "inactive":
        raise ValueError("Deactivation did not set inactive status")
    ignored = {"status", "updated_at"}  # The DB's update trigger owns updated_at.
    if {k: v for k, v in prior.items() if k not in ignored} != {k: v for k, v in current.items() if k not in ignored}:
        raise ValueError("Deactivation changed source content, identity, or metadata")


def snapshot(cur, family, sid, *, lock=False):
    from psycopg import sql
    suffix = sql.SQL(" for update") if lock else sql.SQL("")
    cur.execute(sql.SQL("select to_jsonb(s) as value from {} s where id=%s").format(
        sql.Identifier("public", family + "_sources")) + suffix, (sid,))
    row = cur.fetchone()
    if not row:
        raise ValueError(f"Source disappeared: {family}/{sid}")
    cur.execute(sql.SQL("select to_jsonb(c)-'content_tsv' as value from {} c where source_id=%s order by chunk_index").format(
        sql.Identifier("public", family + "_chunks")) + suffix, (sid,))
    return {"source": row["value"], "chunks": [r["value"] for r in cur.fetchall()]}


def scan(run, expected_only, expected_groups):
    from scripts.db import connect_postgres
    from psycopg import sql
    from psycopg.rows import dict_row
    from scripts.vectorization.datasets import TABLE_FAMILIES, table_family
    from scripts.vectorization.load_to_supabase import iter_records, record_identity
    if (run / "plan.json").exists():
        raise ValueError("Existing snapshot retained; choose a new --run for a fresh audit")
    indexed, index_signatures = defaultdict(set), {}
    for index in sorted((ROOT / "vectorized").glob("*/index.jsonl")):
        dataset = index.parent.name
        before = (index.stat().st_size, index.stat().st_mtime_ns)
        for record in iter_records(index):
            indexed[table_family(dataset, record.get("metadata"))].add(record_identity(record, dataset)[0])
        if before != (index.stat().st_size, index.stat().st_mtime_ns):
            raise ValueError("Local index changed during audit")
        index_signatures[reference_path(index, ROOT)] = list(before)
    by_path = defaultdict(list)
    saved, db_only, groups = {}, [], []
    with connect_postgres(row_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute("set transaction isolation level repeatable read read only")
        cur.execute("set local statement_timeout='600s'")
        for family in TABLE_FAMILIES:
            cur.execute(sql.SQL("select id,file_path,metadata->>'ingestion_dataset' as dataset from {} where status='active'").format(
                sql.Identifier("public", family + "_sources")))
            for row in cur.fetchall():
                sid = row["id"]
                if sid not in indexed[family]:
                    db_only.append({"family": family, "id": sid})
                path = str(row["file_path"] or "").replace("\\", "/")
                if path and row["dataset"]:
                    by_path[(family, row["dataset"], path)].append(sid)
        targets = {(v["family"], v["id"]) for v in db_only}
        for (family, dataset, path), ids in sorted(by_path.items()):
            if len(ids) < 2:
                continue
            groups.append({"family": family, "dataset": dataset, "path": path, "ids": sorted(ids),
                           "group_id": fingerprint([family, dataset, path, sorted(ids)])})
            targets.update((family, sid) for sid in ids)
        for family, sid in sorted(targets):
            value = snapshot(cur, family, sid)
            path = run / "snapshots" / family / (fingerprint(sid) + ".json")
            atomic(path, value)
            raw = value["source"].get("file_path")
            try:
                raw = inside(raw.split("!/", 1)[0]) if raw else None
            except ValueError:
                raw = None  # Out-of-workspace originals are preserved, never guessed.
            saved[family + "/" + sid] = {"path": str(path.relative_to(ROOT)), "fingerprint": fingerprint(value),
                                        "raw_sha256": digest(raw) if raw and raw.is_file() else None,
                                        "indexed": sid in indexed[family]}
    for path, before in index_signatures.items():
        current = inside(path).stat()
        if [current.st_size, current.st_mtime_ns] != before:
            raise ValueError("An earlier local index changed during the DB snapshot")
    for group in groups:
        values = [read(inside(saved[group["family"] + "/" + sid]["path"])) for sid in group["ids"]]
        representative = choose_representative(values, indexed[group["family"]])
        # A present original must also match recorded attachment bytes. Missing
        # originals and archive members need review, even when bodies are equal.
        for value in values:
            source = value["source"]
            metadata = source.get("metadata") or {}
            raw_hash = saved[group["family"] + "/" + source["id"]]["raw_sha256"]
            if (not raw_hash or "!/" in (source.get("file_path") or "")
                    or not metadata.get("attachment_sha256") or metadata["attachment_sha256"] != raw_hash):
                representative = None
        group["suggested_representative"] = representative
        group["classification"] = "exact_duplicate" if representative else "preserve_pending_original_review"
    for item in db_only:
        value = read(inside(saved[item["family"] + "/" + item["id"]]["path"]))
        item["classification"] = "preserve_db_only"
        item["original_identity"] = original_identity(value["source"])
        item["chunk_count"] = len(value["chunks"])
        item["actual_body_hashes"] = [hashlib.sha256(c["content"].encode("utf-8")).hexdigest() for c in value["chunks"]]
    plan = {"policy": POLICY, "db_only": db_only, "groups": groups, "snapshots": saved,
            "expected_db_only": expected_only, "expected_groups": expected_groups,
            "local_indexes": index_signatures}
    atomic(run / "plan.json", plan)
    atomic(run / "decisions.json", [{"group_id": g["group_id"], "representative": g["suggested_representative"],
        "action": "deactivate_duplicates" if g["suggested_representative"] else "preserve",
        "reason": "Exact identity, original hash and ordered chunk hashes" if g["suggested_representative"] else "Missing or different evidence; preserve all"} for g in groups])
    receipt(run, status="scanned", plan_sha256=digest(run / "plan.json"),
            db_only=len(db_only), duplicate_groups=len(groups))
    emit("scanned", db_only=len(db_only), groups=len(groups))
    if len(db_only) != expected_only or len(groups) != expected_groups:
        raise ValueError("Observed counts differ from 34/16 expectations; inspect plan and rescan with reviewed counts. No sources changed")


def checked(run):
    plan = read(run / "plan.json")
    if (plan.get("policy") != POLICY or digest(run / "plan.json") != read(run / "receipt.json").get("plan_sha256")
            or len(plan["db_only"]) != plan["expected_db_only"] or len(plan["groups"]) != plan["expected_groups"]):
        raise ValueError("Selection changed or counts are unreviewed")
    current_indexes = {reference_path(p, ROOT) for p in (ROOT / "vectorized").glob("*/index.jsonl")}
    if current_indexes != {p.replace("\\", "/") for p in plan["local_indexes"]}:
        raise ValueError("Local index set changed after audit; compare again")
    for path, prior in plan["local_indexes"].items():
        stat = inside(path).stat()
        if [stat.st_size, stat.st_mtime_ns] != prior:
            raise ValueError("Local index changed after audit; compare again")
    values = {}
    for key, item in plan["snapshots"].items():
        value = read(inside(item["path"]))
        if fingerprint(value) != item["fingerprint"]:
            raise ValueError("Frozen source evidence changed")
        path = value["source"].get("file_path")
        if item["raw_sha256"] and (not path or digest(inside(path.split("!/", 1)[0])) != item["raw_sha256"]):
            raise ValueError("Original changed after comparison")
        values[key] = value
    decisions = read(run / "decisions.json")
    if len(decisions) != len(plan["groups"]) or {d["group_id"] for d in decisions} != {g["group_id"] for g in plan["groups"]}:
        raise ValueError("Decisions must cover each group exactly once")
    changes = []
    by_group = {g["group_id"]: g for g in plan["groups"]}
    for decision in decisions:
        group = by_group[decision["group_id"]]
        if decision["action"] == "preserve":
            continue
        rep = decision.get("representative")
        if decision["action"] != "deactivate_duplicates" or not group["suggested_representative"] or rep not in group["ids"]:
            raise ValueError("Only proven exact duplicates may be deactivated; preserve other document versions")
        changes.extend({"family": group["family"], "id": sid, "representative": rep} for sid in group["ids"] if sid != rep)
    return plan, values, changes


def search_cases(run, *, after=False, backend_root):
    import logging
    import os
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    sys.path.insert(0, str(backend_root.resolve()))
    from app.config import get_settings
    from app.embeddings import Embedder
    from app.retrieval import build_supabase_client, search
    from app.reranking import CrossEncoderReranker
    plan, values, changes = checked(run)
    settings = get_settings()
    embedder = Embedder(model_name=settings.embedding_model, device="cpu", expected_dimensions=384)
    reranker = CrossEncoderReranker(model_name=settings.reranker_model, device="cpu", quantize=settings.reranker_quantize) if settings.reranker_enabled else None
    client = build_supabase_client(settings.supabase_url, settings.supabase_service_role_key)
    removed = {(c["family"], c["id"]) for c in changes}
    representatives = {(c["family"], c["id"]): c["representative"] for c in changes}
    cases, seen = [], set()
    for key, value in values.items():
        family, sid = key.split("/", 1)
        target = representatives.get((family, sid), sid) if after else sid
        allowed = {target}
        if not after:
            for group in plan["groups"]:
                if (group["family"] == family and sid in group["ids"]
                        and any((family, member) in removed for member in group["ids"])):
                    allowed.update(group["ids"])
        case_key = (family, tuple(sorted(allowed)))
        if case_key in seen:
            continue
        seen.add(case_key)
        source = value["source"]
        metadata = source.get("metadata") or {}
        question = " ".join(str(v) for v in (source.get("title"), metadata.get("attachment_name")) if v)
        if not question:
            raise ValueError("Source has no supported search query; preserve and review")
        failures = []
        class Capture(logging.Handler):
            def emit(self, record):
                if "failed" in record.getMessage().lower():
                    failures.append(record.getMessage())
        logger, handler = logging.getLogger("app.retrieval"), Capture(logging.WARNING)
        logger.addHandler(handler)
        try:
            rows = search(client, rpc_names=["match_" + family + "_documents"],
                embedding=embedder.encode_query(question), top_k=settings.rag_top_k, first_stage_k=settings.rag_first_stage_k,
                rpc_concurrency=settings.rag_rpc_concurrency, min_similarity=settings.rag_min_similarity,
                priority_weight=settings.rag_priority_weight, dataset_priority_weight=settings.rag_dataset_priority_weight,
                source_kind_weight=settings.rag_source_kind_weight, reranker=reranker, reranker_weight=settings.reranker_weight,
                max_chunks_per_url=settings.rag_max_chunks_per_url, query_text=question,
                metadata_filter={"ingestion_dataset": metadata.get("ingestion_dataset")})
        finally:
            logger.removeHandler(handler)
        hits = [r for r in rows if r.get("source_id") in allowed]
        if failures or not hits or (after and any((family, r.get("source_id")) in removed for r in rows)):
            raise ValueError(f"Search coverage/RPC failure for {key}; no successful verification")
        for hit in hits:
            expected = values[family + "/" + hit["source_id"]]
            bodies = {c["chunk_index"]: c["content"] for c in expected["chunks"]}
            if hit.get("content") != bodies.get(hit.get("chunk_index")) or hit.get("url") != expected["source"].get("url"):
                raise ValueError("Retrieved body or citation differs from frozen evidence")
        cases.append({"source": key, "allowed_ids": sorted(allowed), "query": question, "hits": [r["source_id"] for r in hits]})
    atomic(run / ("search_after.json" if after else "search_before.json"), {"status": "passed", "cases": cases,
        "decisions_sha256": digest(run / "decisions.json"), "plan_sha256": digest(run / "plan.json"),
        "backend_root": str(backend_root)})


def apply(run, after):
    from scripts.preprocessing.repair_archive_collisions import require_finished
    from scripts.db import connect_postgres
    from psycopg import sql
    from psycopg.rows import dict_row
    require_finished(after)
    plan, values, changes = checked(run)
    baseline = read(run / "search_before.json")
    if (baseline.get("status") != "passed" or baseline.get("decisions_sha256") != digest(run / "decisions.json")
            or baseline.get("plan_sha256") != digest(run / "plan.json")):
        raise ValueError("Run baseline search for the current decisions before deactivation")
    post_update_sources = {}
    with connect_postgres(row_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute("set transaction isolation level serializable")
        cur.execute("set local lock_timeout='10s'")
        cur.execute("set local statement_timeout='600s'")
        for key in sorted(values):
            family, sid = key.split("/", 1)
            if fingerprint(snapshot(cur, family, sid, lock=True)) != fingerprint(values[key]):
                raise ValueError("DB changed since audit; nothing deactivated")
        for change in changes:
            cur.execute(sql.SQL("update {} as s set status='inactive' where id=%s and status='active' returning to_jsonb(s) as value").format(
                sql.Identifier("public", change["family"] + "_sources")), (change["id"],))
            if cur.rowcount != 1:
                raise ValueError("Source status changed during deactivation")
            key = change["family"] + "/" + change["id"]
            post_update_sources[key] = cur.fetchone()["value"]
            validate_deactivation(values[key]["source"], post_update_sources[key])
    receipt(run, status="applied_pending_search", changes=changes, post_update_sources=post_update_sources,
            decisions_sha256=digest(run / "decisions.json"))
    emit("deactivated", count=len(changes), chunks_deleted=0)


def verify(run, backend_root):
    from scripts.db import connect_postgres
    from psycopg.rows import dict_row
    plan, values, changes = checked(run)
    state = read(run / "receipt.json")
    if state.get("decisions_sha256") != digest(run / "decisions.json") or state.get("changes") != changes:
        raise ValueError("Apply the frozen decisions before verification")
    removed = {(c["family"], c["id"]) for c in changes}
    with connect_postgres(row_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute("set transaction isolation level repeatable read read only")
        for key, prior in values.items():
            family, sid = key.split("/", 1)
            expected = json.loads(json.dumps(prior))
            if (family, sid) in removed:
                expected["source"] = state["post_update_sources"][key]
                validate_deactivation(prior["source"], expected["source"])
            if fingerprint(snapshot(cur, family, sid)) != fingerprint(expected):
                raise ValueError("Unexpected source/chunk change; cannot certify preservation")
    search_cases(run, after=True, backend_root=backend_root)
    receipt(run, status="complete", search_verified=True)
    emit("complete", deactivated=len(changes), db_only_preserved=len(plan["db_only"]) - sum((v["family"], v["id"]) in removed for v in plan["db_only"]))


def main(argv=None):
    from scripts.recovery.cli import legacy_main
    return legacy_main("sources", argv)


if __name__ == "__main__":
    raise SystemExit(main())
