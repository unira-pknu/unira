"""Operator-run original/decoder/OCR comparison; never guess replacement text.

Produces isolated evidence and reviewed preprocessing only. No DB writes or jobs on import.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import re
import shutil
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.recovery.common import read, atomic, digest, receipt, event as emit, inside as resolve_inside
from scripts.artifact_paths import reference_path
from scripts.vectorization.reconcile_ce import fingerprint, quality, body_text

POLICY = "original-text-quality-review-v1"


def inside(path, boundary=None):
    return resolve_inside(path, root=ROOT, boundary=boundary)


def rel(path):
    return reference_path(inside(path), ROOT)


def copy_review_original(item, folder):
    """Read an exact ZIP member without extracting archive paths into the workspace."""
    original = inside(item["raw_path"])
    if not item["raw_sha256"] or digest(original) != item["raw_sha256"]:
        raise ValueError("Original is missing or changed")
    source = item["source_path"]
    if "!/" in source:
        member = source.split("!/", 1)[1]
        if "!/" in member:
            raise ValueError("Nested archives require separate original review")
        with zipfile.ZipFile(original) as archive:
            from scripts.preprocessing.file_preprocessing import decoded_archive_member_name
            matches = [entry for entry in archive.infolist()
                       if decoded_archive_member_name(entry).replace("\\", "/") == member.replace("\\", "/")]
            if len(matches) != 1 or matches[0].is_dir():
                raise ValueError("ZIP member is missing or ambiguous")
            if matches[0].file_size > 128 * 1024 * 1024:
                raise ValueError("ZIP review member exceeds 128 MiB")
            payload = archive.read(matches[0])
        copy = folder / "original" / Path(member.replace("\\", "/")).name
        copy.parent.mkdir(parents=True, exist_ok=True)
        copy.write_bytes(payload)
    else:
        copy = folder / "original" / original.name
        copy.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(original, copy)
        if digest(copy) != item["raw_sha256"]:
            raise ValueError("Original changed while copying")
    return copy


def classify_original(kind, original_text, *, native=None, ocr=None):
    """Classifications are evidence labels, never reconstructed missing characters."""
    if kind in {"json", "utf8_text"} and "\ufffd" in original_text:
        return "replacement_present_in_source_text"
    if kind == "pdf":
        if native is not None and not quality(native)["warning"] and native.strip():
            return "native_text_available_visual_review_required"
        if ocr is not None and ocr.strip():
            return "text_layer_or_ocr_problem_visual_review_required"
        return "pdf_source_or_extractor_unresolved"
    return "source_or_decoder_unresolved"


def scan(run, expected_chunks, expected_pdfs, pdf_ids):
    from scripts.vectorization.load_to_supabase import iter_records, record_identity
    from scripts.vectorization.vectorization import assess_record_quality
    if (run / "plan.json").exists():
        raise ValueError("Existing evidence retained; choose a new run for a fresh scan")
    selected, pdf_candidates, signatures = {}, {}, {}
    broken = 0
    for index in sorted((ROOT / "vectorized").glob("*/index.jsonl")):
        dataset = index.parent.name
        before = (index.stat().st_size, index.stat().st_mtime_ns)
        for record in iter_records(index):
            metadata = record["metadata"]
            text = record.get("text", "")
            garbled = "\ufffd" in text
            pdf = str(metadata.get("source_ext", "")).lower() == ".pdf"
            if not garbled and not pdf:
                continue
            title = " ".join(str(metadata.get(k) or "") for k in ("source_file", "doc_title", "attachment_name"))
            if not garbled and not pdf_ids and not re.search(r"LMS|ZOOM", title, re.I):
                continue
            sid, _ = record_identity(record, dataset)
            key = dataset + "/" + sid
            matches = pdf and (sid in pdf_ids if pdf_ids else bool(re.search(r"LMS|ZOOM", title, re.I)))
            if not garbled and not matches:
                continue
            body = body_text(text, metadata)
            if matches:
                entry = pdf_candidates.setdefault(key, {"dataset": dataset, "id": sid, "metadata": metadata,
                                                       "indexed_slug": record["source_slug"], "positions": [],
                                                       "quality_warnings": False, "index_hashes": {}})
                entry["positions"].append(int(record["chunk_index"]))
                entry["index_hashes"][int(record["chunk_index"])] = fingerprint(record["text"])
                useful, flags = assess_record_quality(body, metadata)
                entry["quality_warnings"] |= quality(body)["warning"] or not useful or bool(flags)
            if not garbled:
                continue
            broken += 1
            entry = selected.setdefault(key, {"dataset": dataset, "id": sid, "metadata": metadata,
                "indexed_slug": record["source_slug"], "affected": [], "index_hashes": {}})
            entry["index_hashes"][int(record["chunk_index"])] = fingerprint(record["text"])
            entry["affected"].append({"chunk_index": int(record["chunk_index"]), "record_sha256": fingerprint(record),
                                      "body_replacements": body.count("\ufffd"),
                                      "prefix_or_metadata_only": "\ufffd" not in body})
        if before != (index.stat().st_size, index.stat().st_mtime_ns):
            raise ValueError("Index changed during inspection")
        signatures[rel(index)] = list(before)
        emit("scan_progress", dataset=dataset, garbled_chunks=broken,
             pdf_candidates=len(pdf_candidates))
    pdfs = {k: v for k, v in pdf_candidates.items() if pdf_ids or v["quality_warnings"]}
    for key, value in pdfs.items():
        selected.setdefault(key, {"dataset": value["dataset"], "id": value["id"], "metadata": value["metadata"],
                                 "indexed_slug": value["indexed_slug"], "affected": []})
        selected[key]["pdf_review"] = True
        selected[key]["pdf_positions"] = value["positions"]
        selected[key]["index_hashes"] = value["index_hashes"]
    items = []
    for value in selected.values():
        metadata = value.pop("metadata")
        path = inside(metadata["preprocessed_path"], ROOT / "preprocessed" / value["dataset"])
        target_hash = digest(path)
        document = read(path)
        if digest(path) != target_hash:
            raise ValueError("Preprocessing changed while taking evidence")
        if document["source_path"] != metadata["source_path"] or document["slug"] != value["indexed_slug"]:
            raise ValueError("Preprocessing and indexed original paths differ")
        from scripts.vectorization.vectorization import extract_chunk_records
        regenerated = {r["chunk_index"]: fingerprint(r["text"]) for r in extract_chunk_records(document, path, ROOT)}
        value["alignment_verified"] = all(regenerated.get(position) == checksum for position, checksum in value["index_hashes"].items())
        folder = run / "sources" / value["dataset"] / value["id"]
        atomic(folder / "local_document.json", document)
        source = document["source_path"]
        raw = inside(source.split("!/", 1)[0])
        items.append({**value, "folder": rel(folder), "target": rel(path), "target_sha256": target_hash,
                      "slug": document["slug"], "source_path": source, "raw_path": rel(raw),
                      "raw_sha256": digest(raw) if raw.is_file() else None,
                      "local_snapshot_sha256": digest(folder / "local_document.json")})
    plan = {"policy": POLICY, "items": items, "garbled_chunks": broken, "noisy_pdfs": len(pdfs),
            "expected_chunks": expected_chunks, "expected_pdfs": expected_pdfs, "indexes": signatures,
            "pdf_candidates": [{"id": v["id"], "dataset": v["dataset"], "file": v["metadata"].get("source_file"),
                                "title": v["metadata"].get("doc_title"), "quality_warnings": v["quality_warnings"]}
                               for v in pdf_candidates.values()]}
    atomic(run / "plan.json", plan)
    counts_match = broken == expected_chunks and len(pdfs) == expected_pdfs
    receipt(run, status="scanned" if counts_match else "selection_review_required",
            plan_sha256=digest(run / "plan.json"), garbled_chunks=broken, noisy_pdfs=len(pdfs))
    emit("scanned", garbled_chunks=broken, noisy_pdfs=len(pdfs), sources=len(items))
    if not counts_match:
        raise ValueError(f"Observed {broken} chunks/{len(pdfs)} PDFs; expected {expected_chunks} chunks/"
                         f"{expected_pdfs} PDFs. Inspect plan before selecting a new run; existing evidence is retained.")


def checked(run):
    plan = read(run / "plan.json")
    if (plan.get("policy") != POLICY or digest(run / "plan.json") != read(run / "receipt.json").get("plan_sha256")
            or plan["garbled_chunks"] != plan["expected_chunks"] or plan["noisy_pdfs"] != plan["expected_pdfs"]):
        raise ValueError("Selection or candidate counts are unreviewed")
    for item in plan["items"]:
        folder = inside(item["folder"], run)
        if digest(folder / "local_document.json") != item["local_snapshot_sha256"]:
            raise ValueError("Local evidence changed")
        if digest(inside(item["target"])) != item["target_sha256"]:
            raise ValueError("Shared preprocessing changed after inspection")
        if item["raw_sha256"] and digest(inside(item["raw_path"])) != item["raw_sha256"]:
            raise ValueError("Original bytes changed after inspection")
    return plan


def probe(run, with_ocr, max_ocr_pages=32, retry_errors=False):
    comparisons, decisions = [], []
    previous = {}
    if retry_errors:
        if not (run / "decisions.json").is_file():
            raise ValueError("Retry requires the existing full decision list")
        evidence = run / "comparisons.json"
        if digest(evidence) != read(run / "receipt.json").get("comparisons_sha256"):
            raise ValueError("Prior comparisons changed; cannot reuse evidence")
        previous = {(r["dataset"], r["id"]): r for r in read(evidence)}
    for item in checked(run)["items"]:
        prior = previous.get((item["dataset"], item["id"]))
        if prior and not prior["errors"]:
            for candidate in prior["candidates"].values():
                if digest(inside(candidate["path"], run)) != candidate["sha256"]:
                    raise ValueError("Prior candidate evidence changed")
            comparisons.append(prior)
            continue
        emit("comparison_started", id=item["id"], dataset=item["dataset"])
        folder = inside(item["folder"], run)
        original = inside(item["raw_path"])
        result = {"id": item["id"], "dataset": item["dataset"], "raw_sha256": item["raw_sha256"],
                  "classification": "source_or_decoder_unresolved", "candidates": {}, "errors": [],
                  "warning": "OCR and text heuristics do not establish visual correctness"}
        try:
            copy = copy_review_original(item, folder)
            original = copy
            result["review_original_sha256"] = digest(copy)
            if original.suffix.lower() == ".pdf":
                import fitz
                native_blocks = []
                with fitz.open(copy) as pdf:
                    result["page_count"] = len(pdf)
                    for page_no, page in enumerate(pdf, 1):
                        text = page.get_text("text")  # Preserve evidence before cleanup/paragraph merging.
                        native_blocks.append({"type": "paragraph", "page": page_no, "text": text})
                        page.get_pixmap(matrix=fitz.Matrix(1.5, 1.5), alpha=False).save(folder / f"page-{page_no:04d}.png")
                variants = {"native": native_blocks}
                if with_ocr and result["page_count"] > max_ocr_pages:
                    result["ocr_deferred"] = f"{result['page_count']} pages exceed OCR budget {max_ocr_pages}; not a source-damage finding"
                elif with_ocr:
                    from scripts.preprocessing.extractors.pdf import extract_pdf_ocr_blocks
                    try:
                        variants["ocr"] = extract_pdf_ocr_blocks(copy, preserve_raw_text=True)
                    except Exception as error:
                        result["ocr_error"] = str(error)
                        variants["ocr"] = []
                for name, blocks in variants.items():
                    path = folder / (name + "_blocks.json")
                    atomic(path, blocks)
                    text = "\n".join(b["text"] for b in blocks)
                    result["candidates"][name] = {"path": rel(path), "sha256": digest(path), "quality": quality(text),
                        "pages": sorted({b["page"] for b in blocks}), "review_required": True,
                        "empty_or_missing_pages": sorted(set(range(1, result["page_count"] + 1))
                            - {b["page"] for b in blocks if b["text"].strip()})}
                native = "\n".join(b["text"] for b in native_blocks)
                ocr = "\n".join(b["text"] for b in variants.get("ocr", []))
                result["classification"] = classify_original("pdf", "", native=native, ocr=ocr)
            elif original.suffix.lower() == ".json":
                raw = read(copy)
                body = "\n".join(str(raw.get(k) or "") for k in ("content", "html_text", "page_content"))
                result["classification"] = classify_original("json", body)
                result["original_body_quality"] = quality(body)
            elif original.suffix.lower() == ".txt":
                body = copy.read_text(encoding="utf-8-sig", errors="strict")
                result["classification"] = classify_original("utf8_text", body)
                result["original_body_quality"] = quality(body)
            elif original.suffix.lower() == ".xls":
                import xlrd
                workbook = xlrd.open_workbook(str(copy), on_demand=True)
                try:
                    body = "\n".join(str(value) for sheet in workbook.sheets()
                                     for row in range(sheet.nrows) for value in sheet.row_values(row))
                finally:
                    workbook.release_resources()
                result["classification"] = classify_original("utf8_text", body)
                result["original_body_quality"] = quality(body)
            else:
                result["errors"].append("Binary text/source damage cannot be inferred from extracted text alone; manual format-specific review required")
        except Exception as error:
            result["errors"].append(f"{type(error).__name__}: {error}")
        if (original.suffix.lower() in {".json", ".txt", ".xls"} and not result["errors"]
                and result.get("original_body_quality", {}).get("chars", 0) > 0
                and not result.get("original_body_quality", {}).get("warning", True)):
            try:
                from scripts.preprocessing.extractors.common import extract_blocks
                blocks = extract_blocks(copy)
                path = folder / "source_blocks.json"
                atomic(path, blocks)
                result["candidates"]["source"] = {"path": rel(path), "sha256": digest(path),
                    "quality": quality("\n".join(b["text"] for b in blocks)), "review_required": True}
                result["classification"] = "source_text_clean_extraction_or_metadata_review_required"
            except Exception as error:
                result["errors"].append(f"Source extraction: {error}")
        comparisons.append(result)
        atomic(run / "comparisons.partial.json", comparisons)
        decisions.append({"id": item["id"], "dataset": item["dataset"], "action": "review",
                          "raw_sha256": item["raw_sha256"], "original_reviewed": False, "reason": "",
                          "candidate": None, "exclude_positions": [], "reviewed_pages": [], "approved_blank_pages": []})
        emit("comparison", **result)
    atomic(run / "comparisons.json", comparisons)
    if not (run / "decisions.json").exists():
        atomic(run / "decisions.json", decisions)
    receipt(run, status="review_required", comparisons_sha256=digest(run / "comparisons.json"))


def stage(run):
    from scripts.preprocessing.file_preprocessing import chunk_blocks
    from scripts.preprocessing.text_cleaning import clean_extracted_text
    plan = checked(run)
    if digest(run / "comparisons.json") != read(run / "receipt.json").get("comparisons_sha256"):
        raise ValueError("Comparison evidence changed; probe again")
    comparisons = {(r["dataset"], r["id"]): r for r in read(run / "comparisons.json")}
    declared = read(run / "decisions.json")
    decisions = {(d["dataset"], d["id"]): d for d in declared}
    if len(decisions) != len(declared) or set(decisions) != {(i["dataset"], i["id"]) for i in plan["items"]}:
        raise ValueError("Decisions must cover the exact selected sources")
    pending = [r for r in declared if r.get("action") not in
               {"retain", "exclude_chunks", "replace_pdf", "replace_from_source"}]
    if pending:
        emit("review_required", pending_sources=len(pending), decisions_path=rel(run / "decisions.json"))
        raise ValueError(f"{len(pending)} source decisions remain unreviewed; no staging changes were made")
    documents = []
    for item in plan["items"]:
        decision = decisions[(item["dataset"], item["id"])]
        action = decision["action"]
        if action not in {"retain", "exclude_chunks", "replace_pdf", "replace_from_source"}:
            raise ValueError("Resolve review decisions before staging; no invented text is accepted")
        if decision["raw_sha256"] != item["raw_sha256"]:
            raise ValueError("Decision refers to different original bytes")
        if action != "retain" and (decision.get("original_reviewed") is not True or not decision.get("reason", "").strip()):
            raise ValueError("Changes require original/visual review and a concrete reason")
        if action != "retain" and not item["alignment_verified"]:
            raise ValueError("Indexed text and local chunks are not aligned; reconcile existing versions first")
        folder = inside(item["folder"], run)
        document = read(folder / "local_document.json")
        if action == "exclude_chunks":
            allowed = {c["chunk_index"] for c in item["affected"]} | set(item.get("pdf_positions", []))
            positions = decision.get("exclude_positions", [])
            if not positions or len(set(positions)) != len(positions) or not set(positions).issubset(allowed):
                raise ValueError("Exclude only reviewed candidate positions")
            for position in positions:
                if position < 0 or position >= len(document["chunks"]):
                    raise ValueError("Chunk-to-original alignment is not established")
                document["chunks"][position]["search_excluded"] = True
                document["chunks"][position]["search_exclusion_reason"] = decision["reason"]
        elif action in {"replace_pdf", "replace_from_source"}:
            if not item["raw_sha256"]:
                raise ValueError("An original file hash is required before replacing extraction")
            comparison = comparisons[(item["dataset"], item["id"])]
            candidate = comparison["candidates"].get(decision.get("candidate"))
            if comparison["errors"] or not candidate or candidate["quality"]["warning"] or not candidate["quality"]["chars"]:
                raise ValueError("No validated clean extraction candidate; preserve or review exclusion")
            if action == "replace_pdf":
                if not item["source_path"].lower().endswith(".pdf") or decision.get("candidate") not in {"native", "ocr"}:
                    raise ValueError("Only selected PDFs can use PDF extraction candidates")
                if set(decision.get("reviewed_pages", [])) != set(range(1, comparison["page_count"] + 1)):
                    raise ValueError("Review every PDF page against its rendered image")
                if not set(candidate["empty_or_missing_pages"]).issubset(set(decision.get("approved_blank_pages", []))):
                    raise ValueError("Candidate drops PDF pages; verify genuinely blank pages or retain/exclude")
            elif decision.get("candidate") != "source" or comparison["classification"] == "replacement_present_in_source_text":
                raise ValueError("Original missing characters cannot be reconstructed from cleaner-looking text")
            path = inside(candidate["path"], run)
            if digest(path) != candidate["sha256"]:
                raise ValueError("Candidate text was edited; arbitrary corrections are prohibited")
            blocks = read(path)
            for block in blocks:
                block["text"] = clean_extracted_text(block["text"])
            document["blocks"] = blocks
            document["chunks"] = chunk_blocks(blocks, 900, 120)
            document.update(num_blocks=len(blocks), num_chunks=len(document["chunks"]),
                            total_chars=sum(len(b["text"]) for b in blocks), used_metadata_fallback=False,
                            processed_at=datetime.now(timezone.utc).isoformat())
        document.setdefault("provenance", {})["text_quality_review"] = {
            "policy": POLICY, "run": rel(run), "action": action, "raw_sha256": item["raw_sha256"],
            "reason": decision.get("reason"), "candidate": decision.get("candidate"),
            "original_reviewed": decision.get("original_reviewed", False),
            "reviewed_at": datetime.now(timezone.utc).isoformat()}
        destination = run / "preprocessed" / item["dataset"] / inside(item["target"]).relative_to(ROOT / "preprocessed" / item["dataset"])
        atomic(destination, document)
        documents.append({"id": item["id"], "dataset": item["dataset"], "action": action, "staged": rel(destination),
                          "sha256": digest(destination), "target": item["target"], "prior_sha256": item["target_sha256"]})
    atomic(run / "documents.json", documents)
    receipt(run, status="staged_pending_scoped_sync", decisions_sha256=digest(run / "decisions.json"))
    emit("staged", sources=len(documents), db_changed=False)


def main(argv=None):
    from scripts.recovery.cli import legacy_main
    return legacy_main("quality", argv)


def audit(run, *, database=False, datasets=None):
    from scripts.preprocessing.quality_audit import audit as inspect_current
    return inspect_current(ROOT, run, database=database, datasets=datasets)


if __name__ == "__main__":
    raise SystemExit(main())
