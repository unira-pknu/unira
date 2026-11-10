"""Read-only current quality/applicability audit supporting the recovery quality CLI.

No receipts, decisions, originals, indexes or database rows are written.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import sys

from scripts.artifact_paths import resolve_artifact_path

COHORT_CUE = re.compile(r"입학\s*(?:연|년)도|(?:19|20)\d{2}(?:학년도|년도|년)?\s*(?:(?:이전|이후|이상|이하|부터|까지)\s*){0,3}(?:입학|학번)")
DEPARTMENT_CUE = re.compile(r"(?:학과|전공|학부)별\s*(?:이수|졸업|선발|자격|기준|요건|조건)|(?:학과|전공)\s*(?:제외|한정)|(?:적용|대상)\s*(?:학과|전공)")
EFFECTIVE_CUE = re.compile(r"시행\s*(?:일|일자)|(?:부터|에)\s*시행|적용\s*(?:일자|시기)|부\s*칙")
FIELDS = {"department": "applicable_department", "admission_year": "applicable_admission_years", "effective_date": "effective_at"}


def _read(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _hash(path):
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def _text_hash(text):
    # Same string fingerprint as review_text_quality's frozen index_hashes.
    return hashlib.sha256(json.dumps(text, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def usable_scope(metadata):
    department = metadata.get("applicable_department")
    years = metadata.get("applicable_admission_years")
    effective = metadata.get("effective_at")
    date_ok = False
    if isinstance(effective, str):
        try:
            datetime.fromisoformat(effective.replace("Z", "+00:00"))
            date_ok = bool(re.match(r"^\d{4}-\d{2}-\d{2}(?:T|$)", effective))
        except ValueError:
            pass
    status = metadata.get("rule_effective_status")
    date_ok &= status is None or (isinstance(status, str) and status not in {"unconfirmed", "unknown", "review_required"})
    return {
        "department": isinstance(department, str) and department.strip().casefold() not in {"", "unknown", "미확인"},
        "admission_year": isinstance(years, list) and bool(years) and all(
            type(y) in (int, str) and re.fullmatch(r"[12]\d{3}", str(y)) is not None for y in years),
        "effective_date": bool(date_ok),
    }


class ScopeSummary:
    def __init__(self):
        self.documents = {}
        self.chunks = Counter()
        self.dataset_counts = Counter()

    def add(self, family, sid, dataset, position, text, metadata):
        key = (family, str(sid))
        doc = self.documents.setdefault(key, {
            "family": family, "dataset": dataset, "positions": set(), "cue": set(), "present": set(),
            "effective_statuses": set(), "publisher_department": False,
        })
        present = usable_scope(metadata)
        doc["present"].update(name for name, ok in present.items() if ok)
        doc["publisher_department"] |= bool(metadata.get("department_id"))
        doc["effective_statuses"].add(str(metadata.get("rule_effective_status") or "missing"))
        if family == "rule":
            doc["cue"].add("effective_date")
        for name, expression in (("department", DEPARTMENT_CUE), ("admission_year", COHORT_CUE), ("effective_date", EFFECTIVE_CUE)):
            if expression.search(text or ""):
                doc["cue"].add(name)
        if position is None:
            return
        if position in doc["positions"]:
            self.chunks["duplicate_positions"] += 1
            return
        doc["positions"].add(position)
        self.chunks["total"] += 1
        self.chunks["with_replacement_character"] += "\ufffd" in (text or "")
        self.dataset_counts[dataset] += 1
        for name, ok in present.items():
            self.chunks[name + "_metadata_missing"] += not ok

    def export(self):
        docs = list(self.documents.values())
        total = len(docs)
        metrics = {}
        for name, field in FIELDS.items():
            present = sum(name in d["present"] for d in docs)
            required = [d for d in docs if name in d["cue"] or name in d["present"]]
            missing = [d for d in required if name not in d["present"]]
            metrics[name] = {
                "field": field, "documents": total, "metadata_present": present,
                "all_documents_missing_rate": round((total - present) / total, 6) if total else None,
                "condition_indicated": len(required), "structured_condition_missing": len(missing),
                "indicated_missing_rate": round(len(missing) / len(required), 6) if required else None,
                "not_indicated": total - len(required),
                "examples": [{"family": d["family"], "source_id": sid} for (family, sid), d in self.documents.items()
                             if name in d["cue"] and name not in d["present"]][:10],
            }
        return {"documents": total, "chunks": dict(self.chunks), "chunks_by_dataset": dict(self.dataset_counts),
                "conditions": metrics, "publisher_department_metadata_present": sum(d["publisher_department"] for d in docs),
                "rule_effective_statuses": dict(Counter(
                    ",".join(sorted(d["effective_statuses"])) for d in docs if d["family"] == "rule"))}


def historical_selection(root, run):
    """Inspect live preprocessing against frozen selection without staging it."""
    from scripts.vectorization.datasets import table_family
    plan = _read(run / "plan.json")
    receipt = _read(run / "receipt.json")
    if plan.get("policy") != "original-text-quality-review-v1" or _hash(run / "plan.json") != receipt.get("plan_sha256"):
        raise ValueError("Historical quality plan identity/hash is not verified")
    decisions = _read(run / "decisions.json") if (run / "decisions.json").exists() else []
    expected, documents = {}, Counter()
    for item in plan["items"]:
        path = resolve_artifact_path(root, item["target"])
        if not path.is_relative_to((root / "preprocessed").resolve()):
            raise ValueError("Historical target is outside preprocessing")
        doc = _read(path) if path.is_file() else None
        documents["missing" if doc is None else ("unchanged" if _hash(path) == item["target_sha256"] else "changed")] += 1
        family = table_family(item["dataset"], doc.get("provenance", {}) if doc else {})
        for affected in item["affected"]:
            position = affected["chunk_index"]
            chunks = (doc or {}).get("chunks") or []
            chunk = chunks[position] if type(position) is int and 0 <= position < len(chunks) else None
            key = (family, item["id"], position)
            if key in expected:
                raise ValueError("Historical selection has duplicate chunk identities")
            expected[key] = {"dataset": item["dataset"], "family": family, "source_id": item["id"], "chunk_index": position,
                             "historical_text_sha256": item["index_hashes"].get(str(position)),
                             "preprocessed_position_present": chunk is not None,
                             "preprocessed_body_replacement": "\ufffd" in str((chunk or {}).get("text") or ""),
                             "search_excluded": (chunk or {}).get("search_excluded") is True,
                             "current_record": "not_scanned"}
    if len(expected) != plan["garbled_chunks"]:
        raise ValueError("Historical affected count differs from signed selection")
    declared = {(d["dataset"], d["id"]): d for d in decisions}
    if len(declared) != len(decisions):
        raise ValueError("Historical decisions contain duplicate identities")
    selected_ids = {(i["dataset"], i["id"]) for i in plan["items"]}
    missing_decisions = selected_ids - set(declared)
    return {"receipt_status": receipt.get("status"), "historical_chunks": plan["garbled_chunks"],
            "selected_documents": len(plan["items"]), "current_preprocessed": dict(documents),
            "decision_actions": dict(Counter(d.get("action", "missing") for d in decisions)),
            "missing_decisions": len(missing_decisions),
            "original_reviewed": sum(d.get("original_reviewed") is True for d in decisions),
            "pending_decisions": len(missing_decisions) + sum(d.get("action") not in {"retain", "exclude_chunks", "replace_pdf", "replace_from_source"} for d in decisions)}, expected


def _observe(expected, family, sid, position, text, *, active=None):
    item = expected.get((family, str(sid), position))
    if item is not None:
        checksum = _text_hash(text or "")
        if item["current_record"] == "ambiguous_duplicate":
            return
        if item.get("current_text_sha256") and item["current_text_sha256"] != checksum:
            item.update(current_record="ambiguous_duplicate", current_replacement=None, same_text_as_historical=None)
            return
        item.update(current_record="present", current_replacement="\ufffd" in (text or ""),
                    same_text_as_historical=checksum == item["historical_text_sha256"], active=active,
                    current_text_sha256=checksum)


def audit_local(root, expected, datasets=None):
    from scripts.vectorization.datasets import table_family
    from scripts.vectorization.load_to_supabase import record_identity
    summary = ScopeSummary()
    indexes = sorted((root / "vectorized").glob("*/index.jsonl"))
    if datasets:
        available = {p.parent.name for p in indexes}
        if set(datasets) - available:
            raise ValueError("Requested local index is missing")
        indexes = [p for p in indexes if p.parent.name in datasets]
    if not indexes:
        raise ValueError("No current local indexes found; refusing an empty-success audit")
    checked = {p.parent.name for p in indexes}
    signatures = {p: (p.stat().st_size, p.stat().st_mtime_ns) for p in indexes}
    for item in expected.values():
        if item["dataset"] in checked:
            item["current_record"] = "not_found_in_index"
    for path in indexes:
        print(json.dumps({"stage": "quality_audit_local_index", "dataset": path.parent.name,
                          "bytes": path.stat().st_size}), file=sys.stderr, flush=True)
        signature = signatures[path]
        with path.open(encoding="utf-8-sig") as handle:
            for line in handle:
                if not line.strip():
                    continue
                record = json.loads(line)
                metadata = record.get("metadata") or {}
                family = table_family(path.parent.name, metadata)
                sid, _ = record_identity(record, path.parent.name)
                text = str(record.get("text") or "")
                summary.add(family, sid, path.parent.name, record["chunk_index"], text, metadata)
                _observe(expected, family, sid, record["chunk_index"], text)
        if signature != (path.stat().st_size, path.stat().st_mtime_ns):
            raise ValueError("Local index changed during audit")
    if any(signature != (p.stat().st_size, p.stat().st_mtime_ns) for p, signature in signatures.items()):
        raise ValueError("An earlier local index changed before the audit completed")
    return {"scope": "local_indexes_not_live_db", "datasets": sorted(checked), "summary": summary.export()}


def audit_database(expected, datasets=None):
    from psycopg import sql
    from psycopg.rows import dict_row
    from scripts.db import connect_postgres
    from scripts.vectorization.datasets import TABLE_FAMILIES, table_family
    if datasets:
        for dataset in datasets:
            table_family(dataset)
    summary = ScopeSummary()
    states = {}
    with connect_postgres(row_factory=dict_row) as conn:
        conn.execute("set transaction isolation level repeatable read read only")
        conn.execute("set local statement_timeout='600s'")
        snapshot = conn.execute("select current_timestamp::text as observed_at, txid_current_snapshot()::text as transaction_snapshot").fetchone()
        for family in TABLE_FAMILIES:
            print(json.dumps({"stage": "quality_audit_database", "family": family}), file=sys.stderr, flush=True)
            with conn.cursor(name="quality_" + family) as cur:
                cur.execute(sql.SQL("select s.id as sid,c.chunk_index,c.content,coalesce(s.metadata,'{{}}'::jsonb)||coalesce(c.metadata,'{{}}'::jsonb) as metadata from {} s left join {} c on c.source_id=s.id where s.status='active'").format(
                    sql.Identifier("public", family + "_sources"), sql.Identifier("public", family + "_chunks")))
                for record in cur:
                    metadata = record["metadata"]
                    dataset = metadata.get("ingestion_dataset") or metadata.get("source_dataset") or family
                    if not datasets or dataset in datasets:
                        summary.add(family, record["sid"], dataset, record["chunk_index"], record["content"] or "", metadata)
            selected = [i for i in expected.values() if i["family"] == family]
            if not selected:
                continue
            ids = sorted({i["source_id"] for i in selected})
            positions = sorted({i["chunk_index"] for i in selected})
            with conn.cursor() as cur:
                cur.execute(sql.SQL("select id,status from {} where id=any(%s)").format(sql.Identifier("public", family + "_sources")), (ids,))
                states.update({(family, str(r["id"])): r["status"] for r in cur.fetchall()})
                cur.execute(sql.SQL("select source_id,chunk_index,content from {} where source_id=any(%s) and chunk_index=any(%s)").format(sql.Identifier("public", family + "_chunks")), (ids, positions))
                for record in cur.fetchall():
                    _observe(expected, family, record["source_id"], record["chunk_index"], record["content"],
                             active=states.get((family, str(record["source_id"]))) == "active")
    for item in expected.values():
        item["database_source_status"] = states.get((item["family"], item["source_id"]), "absent")
        if item["current_record"] == "not_scanned":
            item["current_record"] = "not_found_in_database"
    return {"scope": "live_db_active_sources", "snapshot": snapshot, "dataset_filter": datasets, "summary": summary.export()}


def audit(root: Path, run: Path, *, database=False, datasets=None):
    frozen = {p: _hash(p) for p in (run / "plan.json", run / "receipt.json", run / "decisions.json") if p.is_file()}
    historical, expected = historical_selection(root, run)
    current = audit_database(expected, datasets) if database else audit_local(root, expected, datasets)
    if any(not p.is_file() or _hash(p) != checksum for p, checksum in frozen.items()):
        raise ValueError("Historical evidence or decisions changed during the audit")
    counts = dict(Counter(i["current_record"] for i in expected.values()))
    counts.update({"current_replacement": sum(i.get("current_replacement") is True for i in expected.values()),
                   "same_text_as_historical": sum(i.get("same_text_as_historical") is True for i in expected.values()),
                   "active_current_replacement": sum(i.get("active") is True and i.get("current_replacement") is True for i in expected.values()) if database else None,
                   "preprocessed_body_replacement": sum(i["preprocessed_body_replacement"] for i in expected.values()),
                   "search_excluded": sum(i["search_excluded"] for i in expected.values())})
    return {"audit_version": 1, "read_only": True, "historical": historical, "historical_current_counts": dict(counts),
            "current": current, "selected_chunks": list(expected.values()),
            "limitations": ["Local indexes do not establish live DB presence or active status.",
                            "Condition indicators are heuristics; not_indicated is not proof of non-applicability.",
                            "Publisher department_id does not establish applicable_department.",
                            "published_at, document year and filename dates do not establish effective_at.",
                            "U+FFFD detection is not a complete OCR/encoding accuracy measurement."]}
