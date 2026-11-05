"""Apply human body-image OCR decisions to RAG preprocessing inputs.

Original crawler JSON, OCR sidecars, and review files are read-only. This
module builds text blocks and provenance overlays for the derived RAG JSON.
"""
from __future__ import annotations

from functools import lru_cache
import json
from pathlib import Path
from typing import Any

from scripts.preprocessing.storage import get_preprocessed_root
from scripts.preprocessing.text_cleaning import clean_extracted_text
from scripts.crawlers.common.storage import department_legacy_roots, get_dataset_root
from scripts.crawlers.common.reader import infer_dataset_from_path


def _document_paths(source_file: Path, project_root: Path) -> tuple[str, str, Path, Path] | None:
    """Return source key, dataset, OCR sidecar, and review file for crawler JSON."""
    try:
        relative = source_file.resolve().relative_to(project_root.resolve())
    except ValueError:
        return None
    parts = relative.parts
    marker = next((index for index in range(2, len(parts) - 2)
                   if parts[index:index + 2] == ("output", "json")), -1)
    if marker == -1:
        return None
    if len(parts) < marker + 3 or parts[0] != "files" or parts[marker:marker + 2] != ("output", "json"):
        return None
    dataset = infer_dataset_from_path(source_file)
    if not dataset:
        return None
    document_relative = Path(*parts[marker + 2:])
    source_path = relative.as_posix()
    ocr_path = get_preprocessed_root(project_root, dataset) / "body_images" / document_relative
    for root in department_legacy_roots(project_root, dataset):
        legacy_ocr = root / "preprocessed" / "body_images" / document_relative
        if not ocr_path.is_file() and legacy_ocr.is_file():
            ocr_path = legacy_ocr
    review_path = project_root / "files" / "_reviewed" / "body_image_ocr" / "reviews" / dataset / f"{source_file.stem}.json"
    return source_path, dataset, ocr_path, review_path


@lru_cache(maxsize=8)
def _load_extraction_index(project_root_text: str, filename: str) -> dict[tuple[str, str], dict[str, Any]]:
    path = Path(project_root_text) / "files" / "_reviewed" / "body_image_ocr" / filename
    index: dict[tuple[str, str], dict[str, Any]] = {}
    if not path.is_file():
        return index
    with path.open("r", encoding="utf-8-sig") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            source = str(row.get("source_path") or "").replace("\\", "/")
            digest = str(row.get("image_sha256") or "").lower()
            if source and digest:
                index[(source, digest)] = row
    return index


def _as_text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return clean_extracted_text(value).strip()


def _paragraph_texts(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    result = []
    for item in value:
        text = _as_text(item.get("text")) if isinstance(item, dict) else _as_text(item)
        if text:
            result.append(text)
    return result


def _cell_text(cell: Any) -> str:
    if isinstance(cell, dict):
        return _as_text(cell.get("text") or cell.get("value") or cell.get("content"))
    return _as_text(cell)


def _table_texts(tables: Any) -> list[str]:
    if not isinstance(tables, list):
        return []
    output: list[str] = []
    for table_number, table in enumerate(tables, start=1):
        if not isinstance(table, dict):
            text = _as_text(table)
            if text:
                output.append(text)
            continue
        caption = _as_text(table.get("title") or table.get("caption"))
        if caption:
            output.append(caption)
        rows = table.get("rows")
        if isinstance(rows, list):
            for row_number, row in enumerate(rows, start=1):
                cells = row.get("cells", []) if isinstance(row, dict) else row
                if not isinstance(cells, list):
                    continue
                cell_values = []
                for cell in cells:
                    text = _cell_text(cell)
                    if not text:
                        continue
                    if isinstance(cell, dict):
                        row_idx = cell.get("row")
                        col_idx = cell.get("column")
                        rowspan = cell.get("rowspan", 1)
                        colspan = cell.get("colspan", 1)
                        position = ""
                        if isinstance(row_idx, int) and isinstance(col_idx, int):
                            position = f"r{row_idx + 1}c{col_idx + 1}"
                        spans = []
                        if isinstance(rowspan, int) and rowspan > 1:
                            spans.append(f"rowspan={rowspan}")
                        if isinstance(colspan, int) and colspan > 1:
                            spans.append(f"colspan={colspan}")
                        label = ",".join(part for part in (position, *spans) if part)
                        cell_values.append(f"[{label}] {text}" if label else text)
                    else:
                        cell_values.append(text)
                if cell_values:
                    output.append(f"표 {table_number} 행 {row_number}: " + " | ".join(cell_values))
        elif isinstance(table.get("cells"), list):
            grouped: dict[int, list[str]] = {}
            for cell in table["cells"]:
                if not isinstance(cell, dict):
                    continue
                row = cell.get("row", 0)
                if isinstance(row, int):
                    grouped.setdefault(row, []).append(_cell_text(cell))
            for row_number, values in sorted(grouped.items()):
                values = [value for value in values if value]
                if values:
                    output.append(f"표 {table_number} 행 {row_number + 1}: " + " | ".join(values))
        else:
            fallback = _as_text(table.get("text"))
            if fallback:
                output.append(fallback)
    return output


def _diagram_texts(value: Any) -> list[str]:
    if not isinstance(value, dict):
        return []
    output: list[str] = []
    title = _as_text(value.get("diagram_title") or value.get("title"))
    if title:
        output.append(title)
    nodes = value.get("nodes")
    if isinstance(nodes, list):
        for node in nodes:
            if not isinstance(node, dict):
                continue
            text = _as_text(node.get("text"))
            if text:
                node_id = _as_text(str(node.get("id") or ""))
                output.append(f"도식 항목 {node_id}: {text}" if node_id else text)
    for field, label in (("edges", "확인된 관계"), ("unverified_edges", "검토가 필요한 관계")):
        edges = value.get(field)
        if not isinstance(edges, list):
            continue
        for edge in edges:
            if not isinstance(edge, dict):
                continue
            source = _as_text(str(edge.get("from") or ""))
            target = _as_text(str(edge.get("to") or ""))
            direction = _as_text(str(edge.get("direction") or ""))
            parts = [item for item in (source, direction, target) if item]
            if parts:
                output.append(f"{label}: " + " → ".join(parts))
    unassigned = value.get("unassigned_text_blocks")
    if isinstance(unassigned, list):
        for block in unassigned:
            text = _as_text(block.get("text")) if isinstance(block, dict) else _as_text(block)
            if text:
                output.append(f"미배정 도식 텍스트: {text}")
    return output


def _result_texts(result: Any, layout_type: str) -> list[str]:
    if not isinstance(result, dict):
        return []
    texts = _paragraph_texts(result.get("paragraphs"))
    texts.extend(_table_texts(result.get("tables")))
    diagram = result.get("diagram")
    if not isinstance(diagram, dict) and ("nodes" in result or "diagram_title" in result):
        diagram = result
    if layout_type in {"diagram", "mixed", "unknown"}:
        texts.extend(_diagram_texts(diagram))
    if not texts:
        # Compact infographic OCR keeps the grouped regions in the review
        # result and leaves the noisy raw blocks outside result.paragraphs.
        texts.extend(_paragraph_texts(result.get("regions")))
    return texts


def _corrected_texts(review: dict[str, Any], layout_type: str) -> list[str]:
    correction = review.get("correction") if isinstance(review.get("correction"), dict) else {}
    texts: list[str] = []
    corrected_text = review.get("corrected_text") or correction.get("corrected_text")
    if isinstance(corrected_text, str):
        texts.extend(_as_text(line) for line in corrected_text.splitlines())
    elif isinstance(corrected_text, list):
        texts.extend(_paragraph_texts(corrected_text))
    corrected_structure = review.get("corrected_structure") or correction.get("corrected_structure")
    if isinstance(corrected_structure, dict):
        structure_result = dict(corrected_structure)
        if "diagram" not in structure_result and any(key in structure_result for key in ("nodes", "edges", "diagram_title")):
            structure_result["diagram"] = structure_result
        texts.extend(_result_texts(structure_result, layout_type))
    elif isinstance(corrected_structure, list):
        texts.extend(_paragraph_texts(corrected_structure))

    if not texts:
        # Current review templates store corrections in these structured fields.
        legacy_structure = {
            "paragraphs": correction.get("paragraphs", []),
            "tables": correction.get("tables", []),
            "diagram": correction.get("diagram", {}),
        }
        texts.extend(_result_texts(legacy_structure, layout_type))
    return [text for text in texts if text]


def _image_result(image: dict[str, Any]) -> dict[str, Any]:
    result = image.get("result")
    if isinstance(result, dict) and any(result.get(key) for key in ("paragraphs", "tables", "diagram")):
        return result
    blocks = image.get("blocks") if isinstance(image.get("blocks"), list) else []
    return {
        "paragraphs": [block for block in blocks if isinstance(block, dict) and block.get("type") == "ocr_paragraph"],
        "tables": [block for block in blocks if isinstance(block, dict) and block.get("type") == "ocr_table"],
    }


def _review_index(review_path: Path, source_path: str) -> dict[str, dict[str, Any]]:
    if not review_path.is_file():
        return {}
    try:
        review = json.loads(review_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}
    if str(review.get("source_path") or "").replace("\\", "/") != source_path:
        return {}
    index: dict[str, dict[str, Any]] = {}
    for entry in review.get("images", []):
        if not isinstance(entry, dict):
            continue
        if str(entry.get("source_path") or "").replace("\\", "/") != source_path:
            continue
        digest = str(entry.get("image_sha256") or "").lower()
        if digest:
            index[digest] = entry
    return index


def compile_body_image_blocks(
    source_path: str,
    images: list[dict[str, Any]],
    review_entries: list[dict[str, Any]],
    base_blocks: list[dict[str, Any]],
    *,
    diagram_extractions: dict[tuple[str, str], dict[str, Any]] | None = None,
    table_extractions: dict[tuple[str, str], dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    """Compile a review overlay from already-loaded records (no filesystem I/O)."""
    reviews = {
        str(entry.get("image_sha256") or "").lower(): entry
        for entry in review_entries
        if isinstance(entry, dict)
        and str(entry.get("source_path") or "").replace("\\", "/") == source_path
        and entry.get("image_sha256")
    }
    diagram_index = diagram_extractions or {}
    table_index = table_extractions or {}
    resulting_blocks = list(base_blocks)
    provenance: list[dict[str, Any]] = []
    warnings: list[str] = []
    for image in images:
        if not isinstance(image, dict):
            continue
        digest = str(image.get("sha256") or "").lower()
        if not digest:
            warnings.append("BODY_IMAGE_HASH_MISSING")
            continue
        key = (source_path, digest)
        diagram_record = diagram_index.get(key) or {}
        table_record = table_index.get(key) or {}
        layout_type = str(image.get("layout_type") or "unknown")
        if layout_type == "table":
            specialized = table_record or diagram_record
        elif layout_type == "diagram":
            specialized = diagram_record or table_record
        else:
            specialized = diagram_record or table_record
        review = reviews.get(digest)
        decision = review.get("decision") if review else None
        extractor = str(specialized.get("extractor") or image.get("extractor") or "unknown")
        action = "excluded"
        reason = "NO_REVIEW_DECISION"
        texts: list[str] = []

        if decision == "corrected":
            texts = _corrected_texts(review, layout_type)
            reason = "CORRECTED_DATA_MISSING" if not texts else ""
            action = "included" if texts else "excluded"
        elif decision == "accepted":
            selected_result = specialized.get("result") if specialized else None
            if not isinstance(selected_result, dict):
                selected_result = _image_result(image)
            texts = _result_texts(selected_result, layout_type)
            reason = "NO_EXTRACTABLE_TEXT" if not texts else ""
            action = "included" if texts else "excluded"
        elif decision == "excluded":
            reason = "REVIEW_EXCLUDED"
        elif decision in {"reprocess", "needs_review"}:
            reason = "REVIEW_REPROCESS_REQUIRED" if decision == "reprocess" else "REVIEW_NEEDS_REVIEW"
        elif decision is None or decision == "pending":
            reason = "NO_REVIEW_DECISION" if review else "NO_REVIEW_FILE_OR_IMAGE_ENTRY"
        else:
            reason = "INVALID_REVIEW_DECISION"
            warnings.append(f"INVALID_REVIEW_DECISION:{digest}")

        if action == "included":
            for text in texts:
                resulting_blocks.append({
                    "type": "body_image_ocr",
                    "style": f"BodyImageOCR:{layout_type}",
                    "text": text,
                    "image_sha256": digest,
                })

        provenance.append({
            "source_path": source_path,
            "image_sha256": digest,
            "review_decision": decision,
            "extractor": extractor,
            "layout_type": layout_type,
            "included": action == "included",
            "exclusion_reason": reason or None,
        })
    return resulting_blocks, provenance, warnings


def apply_body_image_reviews(
    source_file: Path,
    base_blocks: list[dict[str, Any]],
    *,
    project_root: Path,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    """Append only reviewed image text, returning blocks, provenance, warnings.

    A missing or pending decision is deliberately fail-closed: base document
    blocks remain untouched and image OCR is not automatically merged.
    """
    paths = _document_paths(source_file, project_root)
    if paths is None:
        return base_blocks, [], []
    source_path, _dataset, ocr_path, review_path = paths
    if not ocr_path.is_file():
        return base_blocks, [], []
    warnings: list[str] = []
    try:
        ocr_entry = json.loads(ocr_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        return base_blocks, [], [f"BODY_IMAGE_OCR_UNREADABLE:{type(exc).__name__}"]

    if str(ocr_entry.get("source_path") or "").replace("\\", "/") != source_path:
        return base_blocks, [], ["BODY_IMAGE_SOURCE_PATH_MISMATCH"]

    review_entries = list(_review_index(review_path, source_path).values())
    diagram_index = _load_extraction_index(str(project_root.resolve()), "diagram_extractions.jsonl")
    table_index = _load_extraction_index(str(project_root.resolve()), "table_extractions.jsonl")
    blocks, provenance, compile_warnings = compile_body_image_blocks(
        source_path,
        ocr_entry.get("images", []) if isinstance(ocr_entry.get("images"), list) else [],
        review_entries,
        base_blocks,
        diagram_extractions=diagram_index,
        table_extractions=table_index,
    )
    warnings.extend(compile_warnings)
    return blocks, provenance, warnings


def body_image_input_mtime(source_file: Path, *, project_root: Path) -> float:
    """Newest OCR/review input time so changed-only preprocessing sees edits."""
    paths = _document_paths(source_file, project_root)
    if paths is None:
        return 0.0
    _source_path, _dataset, ocr_path, review_path = paths
    candidates = [path for path in (ocr_path, review_path) if path.is_file()]
    if ocr_path.is_file():
        reviewed_root = project_root / "files" / "_reviewed" / "body_image_ocr"
        candidates.extend(path for path in (
            reviewed_root / "table_extractions.jsonl",
            reviewed_root / "diagram_extractions.jsonl",
        ) if path.is_file())
    return max((path.stat().st_mtime for path in candidates), default=0.0)
