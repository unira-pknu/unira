"""Schema and review sidecars for body-image OCR results.

This module deliberately does not run OCR or alter crawler output.  It adds a
backwards-compatible layout envelope around existing OCR blocks and creates
review templates keyed by the immutable source path and image hash.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
from scripts.preprocessing.storage import get_preprocessed_root
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[2]
LAYOUT_SCHEMA_VERSION = "1.0"
REVIEW_SCHEMA_VERSION = "1.1"
LAYOUT_TYPES = {"prose", "table", "diagram", "mixed", "no_text", "unknown"}
EXTRACTORS = {"paragraph_ocr", "table_ocr", "diagram_ocr", "mixed_ocr"}
REVIEW_DECISIONS = {"accepted", "corrected", "excluded", "reprocess", "needs_review"}


def empty_correction() -> dict[str, Any]:
    """An overlay: only supplied blocks, cells, nodes, or edges are replaced."""
    return {
        "paragraphs": [],
        "tables": [],
        "diagram": {"nodes": [], "edges": [], "unverified_edges": []},
    }


def _has_correction_data(correction: dict[str, Any]) -> bool:
    diagram = correction["diagram"]
    return bool(
        correction["paragraphs"] or correction["tables"]
        or diagram["nodes"] or diagram["edges"] or diagram["unverified_edges"]
        or correction.get("corrected_text") or correction.get("corrected_structure")
    )


def _unique_strings(values: object) -> list[str]:
    result: list[str] = []
    for value in values if isinstance(values, list) else []:
        if isinstance(value, str) and value not in result:
            result.append(value)
    return result


def _default_layout(image: dict[str, Any]) -> dict[str, Any]:
    """Wrap legacy OCR blocks without claiming an unimplemented classification."""
    blocks = image.get("blocks") if isinstance(image.get("blocks"), list) else []
    paragraphs = [deepcopy(block) for block in blocks if block.get("type") == "ocr_paragraph"]
    tables = [deepcopy(block) for block in blocks if block.get("type") == "ocr_table"]
    if tables and paragraphs:
        layout_type, confidence, extractor, evidence = "mixed", 1.0, "mixed_ocr", ["LEGACY_PARAGRAPH_AND_TABLE_BLOCKS"]
    elif tables:
        layout_type, confidence, extractor, evidence = "table", 1.0, "table_ocr", ["LEGACY_TABLE_BLOCK_PRESENT"]
    elif image.get("status") == "empty":
        layout_type, confidence, extractor, evidence = "no_text", 1.0, "paragraph_ocr", ["OCR_RETURNED_NO_BLOCKS"]
    else:
        # Paragraph blocks alone do not prove that an image is prose: it may
        # be an unrecognised table or diagram. The router will classify it.
        layout_type, confidence, extractor, evidence = "unknown", 0.0, "paragraph_ocr", ["LEGACY_OCR_NO_LAYOUT_CLASSIFICATION"]
    return {
        "layout_type": layout_type,
        "layout_confidence": confidence,
        "extractor": extractor,
        "classification_evidence": evidence,
        "warnings": _unique_strings(image.get("warnings")),
        "result": {
            "paragraphs": paragraphs,
            "tables": tables,
            "diagram": {"nodes": [], "edges": [], "unverified_edges": []},
            "regions": deepcopy(image.get("ocr_regions", {}).get("regions", [])),
        },
    }


def enrich_image_layout(image: dict[str, Any]) -> bool:
    """Add missing layout fields while preserving future classifier output."""
    changed = False
    defaults = _default_layout(image)
    for field, value in defaults.items():
        if field not in image:
            image[field] = value
            changed = True
    if image.get("layout_type") not in LAYOUT_TYPES:
        raise ValueError(f"Invalid layout_type: {image.get('layout_type')!r}")
    confidence = image.get("layout_confidence")
    if not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
        raise ValueError("layout_confidence must be between 0 and 1")
    if image.get("extractor") not in EXTRACTORS:
        raise ValueError(f"Invalid extractor: {image.get('extractor')!r}")
    if not isinstance(image.get("classification_evidence"), list):
        raise ValueError("classification_evidence must be a list")
    if not isinstance(image.get("warnings"), list):
        raise ValueError("warnings must be a list")
    result = image.get("result")
    if not isinstance(result, dict) or not isinstance(result.get("paragraphs"), list) or not isinstance(result.get("tables"), list):
        raise ValueError("result must contain paragraphs and tables lists")
    if not isinstance(result.get("diagram"), dict):
        raise ValueError("result must contain a diagram object")
    if "regions" in result and not isinstance(result["regions"], list):
        raise ValueError("result regions must be a list")
    if "regions" not in result and isinstance(image.get("ocr_regions"), dict):
        result["regions"] = deepcopy(image["ocr_regions"].get("regions", []))
        changed = True
    return changed


def enrich_entry(entry: dict[str, Any]) -> bool:
    if not isinstance(entry.get("images"), list):
        raise ValueError("OCR entry must contain an images list")
    changed = entry.get("layout_schema_version") != LAYOUT_SCHEMA_VERSION
    entry["layout_schema_version"] = LAYOUT_SCHEMA_VERSION
    for image in entry["images"]:
        if not isinstance(image, dict):
            raise ValueError("OCR image record must be an object")
        changed = enrich_image_layout(image) or changed
    return changed


def review_key(source_path: str, image_sha256: str) -> str:
    if not source_path or not image_sha256:
        raise ValueError("source_path and image_sha256 are required review keys")
    return f"{source_path}#{image_sha256}"


def review_template(entry: dict[str, Any]) -> dict[str, Any]:
    source_path = entry.get("source_path")
    if not isinstance(source_path, str) or not source_path:
        raise ValueError("OCR entry source_path is required")
    records = []
    for image in entry.get("images", []):
        digest = image.get("sha256")
        if not isinstance(digest, str) or not digest:
            continue
        records.append({
            "review_key": review_key(source_path, digest),
            "source_path": source_path,
            "image_sha256": digest,
            "decision": None,
            "reviewed_at": None,
            "reviewer": None,
            "notes": None,
            "correction": empty_correction(),
        })
    return {
        "review_schema_version": REVIEW_SCHEMA_VERSION,
        "source_path": source_path,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "rag_inclusion_policy": {
            "accepted": "include",
            "corrected": "include",
            "excluded": "exclude",
            "reprocess": "exclude",
            "needs_review": "exclude",
            "pending": "exclude",
        },
        "images": records,
    }


def enrich_review(review: dict[str, Any]) -> bool:
    """Upgrade empty templates without replacing reviewer decisions or edits."""
    changed = review.get("review_schema_version") != REVIEW_SCHEMA_VERSION
    review["review_schema_version"] = REVIEW_SCHEMA_VERSION
    policy = {
        "accepted": "include", "corrected": "include", "excluded": "exclude",
        "reprocess": "exclude", "needs_review": "exclude", "pending": "exclude",
    }
    if review.get("rag_inclusion_policy") != policy:
        review["rag_inclusion_policy"] = policy
        changed = True
    for image in review.get("images", []):
        if image.get("correction") is None:
            image["correction"] = empty_correction()
            changed = True
    validate_review(review)
    return changed


def validate_review(review: dict[str, Any]) -> None:
    source_path = review.get("source_path")
    if not isinstance(source_path, str) or not source_path:
        raise ValueError("review source_path is required")
    seen = set()
    for image in review.get("images", []):
        if image.get("source_path") != source_path:
            raise ValueError("review image source_path must match review source_path")
        key = review_key(image.get("source_path"), image.get("image_sha256"))
        if image.get("review_key") != key or key in seen:
            raise ValueError("review_key must be unique and match source_path plus image_sha256")
        seen.add(key)
        decision = image.get("decision")
        if decision is not None and decision not in REVIEW_DECISIONS:
            raise ValueError(f"Invalid review decision: {decision!r}")
        correction = image.get("correction")
        required_correction_fields = {"paragraphs", "tables", "diagram"}
        optional_correction_fields = {"corrected_text", "corrected_structure"}
        if (not isinstance(correction, dict)
                or not required_correction_fields.issubset(correction)
                or not set(correction).issubset(required_correction_fields | optional_correction_fields)):
            raise ValueError("correction must contain paragraphs, tables, and diagram; corrected_text and corrected_structure are optional")
        if not isinstance(correction["paragraphs"], list) or not isinstance(correction["tables"], list):
            raise ValueError("correction paragraphs and tables must be lists")
        if "corrected_text" in correction and not isinstance(correction["corrected_text"], (str, list)):
            raise ValueError("correction corrected_text must be a string or list")
        if "corrected_structure" in correction and not isinstance(correction["corrected_structure"], (dict, list)):
            raise ValueError("correction corrected_structure must be an object or list")
        if "corrected_text" in image and not isinstance(image["corrected_text"], (str, list)):
            raise ValueError("review corrected_text must be a string or list")
        if "corrected_structure" in image and not isinstance(image["corrected_structure"], (dict, list)):
            raise ValueError("review corrected_structure must be an object or list")
        diagram = correction["diagram"]
        if not isinstance(diagram, dict) or set(diagram) != {"nodes", "edges", "unverified_edges"}:
            raise ValueError("correction diagram must contain nodes, edges, and unverified_edges")
        if any(not isinstance(diagram[field], list) for field in diagram):
            raise ValueError("correction diagram fields must be lists")
        if decision == "corrected" and not (
                _has_correction_data(correction)
                or image.get("corrected_text")
                or image.get("corrected_structure")):
            raise ValueError("corrected review decisions require correction data")


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".part")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def _paths(args: argparse.Namespace) -> list[Path]:
    selected: list[Path] = []
    if args.source:
        if args.all_datasets or args.dataset:
            raise ValueError("--source cannot be combined with --dataset or --all-datasets")
        for value in args.source:
            path = (PROJECT_ROOT / value).resolve()
            try:
                path.relative_to(PROJECT_ROOT / "files")
            except ValueError as exc:
                raise ValueError("--source must be inside files/") from exc
            if "preprocessed" not in path.parts or "body_images" not in path.parts or path.suffix != ".json":
                raise ValueError("--source must name a body_images JSON result")
            selected.append(path)
    else:
        datasets = args.dataset or []
        if args.all_datasets:
            from scripts.crawlers.common.storage import iter_dataset_roots
            datasets = [path.name for path in iter_dataset_roots(PROJECT_ROOT)
                        if (get_preprocessed_root(PROJECT_ROOT, path.name) / "body_images").is_dir()]
        for dataset in datasets:
            from scripts.crawlers.common.storage import get_dataset_root
            root = get_preprocessed_root(PROJECT_ROOT, dataset) / "body_images"
            if root.is_dir():
                selected.extend(sorted(root.rglob("*.json")))
    if not selected:
        raise ValueError("Specify --source, --dataset, or --all-datasets")
    return selected[:args.limit] if args.limit is not None else selected


def _review_path(ocr_path: Path) -> Path:
    from scripts.crawlers.common.reader import infer_dataset_from_path
    relative = ocr_path.resolve().relative_to(PROJECT_ROOT / "files")
    dataset = infer_dataset_from_path(ocr_path)
    if not dataset:
        raise ValueError(f"Cannot infer dataset from {ocr_path}")
    return PROJECT_ROOT / "files" / "_reviewed" / "body_image_ocr" / "reviews" / dataset / relative.name


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", action="append", help="OCR result JSON path, repeatable")
    parser.add_argument("--dataset", action="append", help="Dataset name, repeatable")
    parser.add_argument("--all-datasets", action="store_true", help="Select every existing body_images result")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--backfill", action="store_true", help="Add layout schema without running OCR")
    parser.add_argument("--create-review-template", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()
    if not args.backfill and not args.create_review_template:
        parser.error("Specify --backfill and/or --create-review-template")
    try:
        paths = _paths(args)
        backfilled = templates = 0
        for path in paths:
            entry = json.loads(path.read_text(encoding="utf-8"))
            changed = enrich_entry(entry)
            if args.backfill and changed:
                write_json(path, entry)
                backfilled += 1
            if args.create_review_template:
                destination = _review_path(path)
                if destination.exists():
                    existing = json.loads(destination.read_text(encoding="utf-8"))
                    review_changed = enrich_review(existing)
                    if existing.get("source_path") != entry.get("source_path"):
                        raise ValueError(f"review path collision: {destination}")
                    if review_changed:
                        write_json(destination, existing)
                else:
                    write_json(destination, review_template(entry))
                    templates += 1
            if not args.quiet:
                print(json.dumps({"path": path.relative_to(PROJECT_ROOT).as_posix(), "backfilled": bool(args.backfill and changed),
                                  "review_template": args.create_review_template}, ensure_ascii=False))
        print(json.dumps({"processed": len(paths), "backfilled": backfilled, "review_templates_created": templates}, ensure_ascii=False))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
