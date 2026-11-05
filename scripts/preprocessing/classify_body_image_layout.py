"""Conservatively classify saved body images without running OCR or rewriting OCR output."""
from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
from scripts.preprocessing.storage import get_preprocessed_root
from typing import Any

import numpy as np
from PIL import Image, ImageOps

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT = PROJECT_ROOT / "files" / "_reviewed" / "body_image_ocr" / "layout_classifications.jsonl"
DEFAULT_SUMMARY = PROJECT_ROOT / "files" / "_reviewed" / "body_image_ocr" / "layout_classification_summary.json"
LAYOUT_TYPES = {"prose", "table", "diagram", "mixed", "no_text", "unknown"}
AUTO_ROUTE_THRESHOLD = 0.85


def _resize_for_analysis(image: Image.Image) -> Image.Image:
    image = image.copy()
    image.thumbnail((1400, 1400))
    return image


def _group_count(values: np.ndarray) -> int:
    padded = np.concatenate(([False], values.astype(bool), [False]))
    return int(np.count_nonzero(np.diff(padded.astype(np.int8)) == 1))


def _long_line_rows(dark: np.ndarray, *, axis: int, minimum_fractions: tuple[float, ...]) -> dict[float, int]:
    """Count raster rows containing a near-continuous, unusually long line.

    Text can make a row look dark in aggregate.  It does not normally create a
    continuous stroke across a third of an image, whereas ruled tables do.  The
    count is deliberately only a veto for automatic diagram classification;
    it never promotes an unverified image to ``table``.
    """
    rows = dark if axis == 1 else dark.T
    minima = {fraction: int(rows.shape[1] * fraction) for fraction in minimum_fractions}
    counts = {fraction: 0 for fraction in minimum_fractions}
    for row in rows:
        padded = np.concatenate(([False], row, [False]))
        edges = np.flatnonzero(np.diff(padded.astype(np.int8)))
        longest = int(np.max(edges[1::2] - edges[::2])) if len(edges) else 0
        for fraction, minimum in minima.items():
            if longest >= minimum:
                counts[fraction] += 1
    return counts


def visual_features(path: Path) -> dict[str, Any]:
    with Image.open(path) as original:
        image = _resize_for_analysis(ImageOps.exif_transpose(original).convert("RGB"))
    dark = np.asarray(image.convert("L")) < 150
    # Text strokes rarely cover 18% of an entire row/column. Long box edges
    # and connectors do, while the stricter grid detector handles tables.
    horizontal_groups = _group_count(dark.mean(axis=1) >= 0.18)
    vertical_groups = _group_count(dark.mean(axis=0) >= 0.18)
    horizontal_lines = _long_line_rows(dark, axis=1, minimum_fractions=(.10, .30))
    vertical_lines = _long_line_rows(dark, axis=0, minimum_fractions=(.10, .30))
    return {
        "analysis_width": image.width,
        "analysis_height": image.height,
        "dark_ratio": round(float(dark.mean()), 5),
        "horizontal_line_groups": horizontal_groups,
        "vertical_line_groups": vertical_groups,
        "connector_horizontal_line_rows": horizontal_lines[.10],
        "connector_vertical_line_rows": vertical_lines[.10],
        "wide_horizontal_line_rows": horizontal_lines[.30],
        "wide_vertical_line_rows": vertical_lines[.30],
        # The OCR preprocessor already runs the strict enclosed-grid detector
        # and records its verified table blocks.  Re-running it here would be
        # costly and could not make an unverified grid safe to auto-label.
        "closed_grid": False,
    }


def _paragraph_blocks(image: dict[str, Any]) -> list[dict[str, Any]]:
    result = image.get("result") if isinstance(image.get("result"), dict) else {}
    paragraphs = result.get("paragraphs")
    if image.get("ocr_storage") == "compact_v1" and not paragraphs:
        blocks = image.get("blocks", [])
    else:
        blocks = paragraphs if isinstance(paragraphs, list) else image.get("blocks", [])
    return [block for block in blocks if isinstance(block, dict) and block.get("type") == "ocr_paragraph" and isinstance(block.get("bbox"), list)]


def _table_blocks(image: dict[str, Any]) -> list[dict[str, Any]]:
    result = image.get("result") if isinstance(image.get("result"), dict) else {}
    blocks = result.get("tables") if isinstance(result.get("tables"), list) else image.get("blocks", [])
    return [block for block in blocks if isinstance(block, dict) and block.get("type") == "ocr_table"]


def _spatial_features(image: dict[str, Any]) -> dict[str, Any]:
    blocks = _paragraph_blocks(image)
    width, height = max(int(image.get("width") or 1), 1), max(int(image.get("height") or 1), 1)
    centers = []
    for block in blocks:
        bbox = block.get("bbox")
        if len(bbox) != 4:
            continue
        centers.append(((bbox[0] + bbox[2]) / 2 / width, (bbox[1] + bbox[3]) / 2 / height))
    if not centers:
        return {"blocks": 0, "characters": 0, "x_bins": 0, "y_bins": 0, "x_spread": 0.0}
    points = np.asarray(centers)
    return {
        "blocks": len(centers),
        "characters": sum(len(str(block.get("text") or "")) for block in blocks),
        "x_bins": len(set(np.clip((points[:, 0] * 4).astype(int), 0, 3))),
        "y_bins": len(set(np.clip((points[:, 1] * 4).astype(int), 0, 3))),
        "x_spread": round(float(np.std(points[:, 0])), 4),
    }


def classify(image: dict[str, Any], features: dict[str, Any]) -> dict[str, Any]:
    warnings = [item for item in image.get("warnings", []) if isinstance(item, str)]
    tables = _table_blocks(image)
    spatial = _spatial_features(image)
    evidence = [
        f"DARK_RATIO={features['dark_ratio']}",
        f"HORIZONTAL_LINE_GROUPS={features['horizontal_line_groups']}",
        f"VERTICAL_LINE_GROUPS={features['vertical_line_groups']}",
        f"CONNECTOR_HORIZONTAL_LINE_ROWS={features['connector_horizontal_line_rows']}",
        f"CONNECTOR_VERTICAL_LINE_ROWS={features['connector_vertical_line_rows']}",
        f"WIDE_HORIZONTAL_LINE_ROWS={features['wide_horizontal_line_rows']}",
        f"WIDE_VERTICAL_LINE_ROWS={features['wide_vertical_line_rows']}",
        f"OCR_BLOCKS={spatial['blocks']}",
    ]
    # OCR returning no blocks is not enough: a diagram or a small-font notice
    # can fail OCR while still containing substantial visual content.
    if spatial["blocks"] == 0 and features["dark_ratio"] < 0.025:
        return {"layout_type": "no_text", "layout_confidence": 0.98,
                "classification_evidence": evidence + ["OCR_RETURNED_NO_TEXT"], "candidate_layout_type": "no_text"}
    if tables and spatial["blocks"]:
        return {"layout_type": "mixed", "layout_confidence": 0.98,
                "classification_evidence": evidence + ["EXISTING_TABLE_AND_PARAGRAPH_BLOCKS"], "candidate_layout_type": "mixed"}
    if tables or features["closed_grid"]:
        return {"layout_type": "table", "layout_confidence": 0.96,
                "classification_evidence": evidence + ["CLOSED_GRID_OR_EXISTING_TABLE_BLOCK"], "candidate_layout_type": "table"}
    line_groups = features["horizontal_line_groups"] + features["vertical_line_groups"]
    diagram_score = min(0.94, 0.45 + 0.07 * line_groups + 0.08 * min(spatial["x_bins"], 3) + 0.05 * min(spatial["y_bins"], 3))
    grid_like = (features["wide_horizontal_line_rows"] >= 6 and
                 features["wide_vertical_line_rows"] >= 6)
    connector_like = (features["connector_horizontal_line_rows"] >= 2 and
                      features["connector_vertical_line_rows"] >= 2)
    if spatial["blocks"] >= 4 and spatial["x_bins"] >= 3 and spatial["y_bins"] >= 3 and line_groups >= 4 and connector_like and not grid_like and diagram_score >= AUTO_ROUTE_THRESHOLD:
        return {"layout_type": "diagram", "layout_confidence": round(diagram_score, 3),
                "classification_evidence": evidence + ["DISPERSED_TEXT_BOXES", "LONG_LINE_GROUPS"], "candidate_layout_type": "diagram"}
    prose_score = min(0.90, 0.55 + 0.06 * min(spatial["blocks"], 5) + 0.12 * (spatial["x_bins"] <= 2) + 0.08 * (spatial["y_bins"] >= 2) - 0.06 * line_groups)
    if spatial["blocks"] >= 3 and spatial["x_bins"] <= 2 and spatial["x_spread"] <= 0.22 and line_groups <= 2 and prose_score >= AUTO_ROUTE_THRESHOLD:
        return {"layout_type": "prose", "layout_confidence": round(prose_score, 3),
                "classification_evidence": evidence + ["SINGLE_COLUMN_READING_ORDER"], "candidate_layout_type": "prose"}
    candidate, score = ("diagram", diagram_score) if diagram_score >= prose_score else ("prose", prose_score)
    unknown_evidence = evidence + [f"CANDIDATE={candidate}", "BELOW_AUTO_ROUTE_THRESHOLD"]
    if grid_like:
        unknown_evidence.append("GRID_LIKE_LAYOUT_REQUIRES_REVIEW")
    return {"layout_type": "unknown", "layout_confidence": round(min(score, AUTO_ROUTE_THRESHOLD - .01), 3),
            "classification_evidence": unknown_evidence,
            "candidate_layout_type": candidate, "warnings": warnings + ["LAYOUT_CLASSIFICATION_UNCERTAIN"]}


def iter_entries(datasets: list[str] | None) -> list[tuple[str, Path]]:
    from scripts.crawlers.common.storage import get_dataset_root, iter_dataset_roots
    roots = [get_preprocessed_root(PROJECT_ROOT, dataset) / "body_images" for dataset in datasets] if datasets else [
        get_preprocessed_root(PROJECT_ROOT, path.name) / "body_images" for path in iter_dataset_roots(PROJECT_ROOT)
        if (get_preprocessed_root(PROJECT_ROOT, path.name) / "body_images").is_dir()
    ]
    result = []
    for root in sorted(roots):
        if root.is_dir():
            dataset = root.parent.name
            result.extend((dataset, path) for path in sorted(root.rglob("*.json")))
    return result


def _feature_or_error(path_text: str) -> tuple[dict[str, Any] | None, str | None]:
    try:
        return visual_features(Path(path_text)), None
    except Exception as exc:  # Kept per image so one corrupt file never aborts a dataset.
        return None, str(exc)


def _failed_features() -> dict[str, Any]:
    return {"analysis_width": None, "analysis_height": None, "dark_ratio": None,
            "horizontal_line_groups": None, "vertical_line_groups": None,
            "connector_horizontal_line_rows": None, "connector_vertical_line_rows": None,
            "wide_horizontal_line_rows": None, "wide_vertical_line_rows": None,
            "closed_grid": None}


def classify_entries(datasets: list[str] | None, limit: int | None, *, workers: int = 1) -> list[dict[str, Any]]:
    inputs: list[tuple[str, str, dict[str, Any], Path]] = []
    for dataset, path in iter_entries(datasets):
        entry = json.loads(path.read_text(encoding="utf-8"))
        source_path = entry.get("source_path")
        if not isinstance(source_path, str):
            continue
        for image in entry.get("images", []):
            if limit is not None and len(inputs) >= limit:
                break
            if not isinstance(image, dict):
                continue
            digest, saved_path = image.get("sha256"), image.get("saved_path")
            if not isinstance(digest, str) or not isinstance(saved_path, str):
                continue
            local_path = PROJECT_ROOT / saved_path
            if not local_path.is_file():
                continue
            inputs.append((dataset, source_path, image, local_path))

    paths_by_digest: dict[str, Path] = {}
    for _, _, image, local_path in inputs:
        paths_by_digest.setdefault(image["sha256"], local_path)
    if workers > 1 and len(paths_by_digest) > 1:
        # Threads keep this portable in restricted Windows sessions where the
        # OS denies the anonymous pipes required to start child processes.
        # Pillow/NumPy release the GIL during the expensive raster operations.
        with ThreadPoolExecutor(max_workers=workers) as executor:
            analyzed = executor.map(_feature_or_error, (str(path) for path in paths_by_digest.values()), chunksize=16)
            cache = dict(zip(paths_by_digest, analyzed, strict=True))
    else:
        cache = {digest: _feature_or_error(str(path)) for digest, path in paths_by_digest.items()}

    rows = []
    for dataset, source_path, image, local_path in inputs:
        digest = image["sha256"]
        features, error = cache[digest]
        if features is None:
            features = _failed_features()
            result = {"layout_type": "unknown", "layout_confidence": 0.0,
                      "classification_evidence": ["IMAGE_ANALYSIS_FAILED", error or "unknown error"],
                      "candidate_layout_type": "unknown", "warnings": ["LAYOUT_CLASSIFICATION_FAILED"]}
        else:
            try:
                result = classify(image, features)
            except Exception as exc:
                features = _failed_features()
                result = {"layout_type": "unknown", "layout_confidence": 0.0,
                          "classification_evidence": ["IMAGE_ANALYSIS_FAILED", str(exc)],
                          "candidate_layout_type": "unknown", "warnings": ["LAYOUT_CLASSIFICATION_FAILED"]}
        if result["layout_type"] not in LAYOUT_TYPES:
            raise ValueError("invalid layout type")
        rows.append({
            "source_path": source_path, "image_sha256": digest, "saved_path": image["saved_path"],
            "dataset": dataset, "source_url": image.get("source_url"), "ocr_status": image.get("status"),
            "current_warnings": image.get("warnings", []), "image_width": image.get("width"),
            "image_height": image.get("height"), **features, **result,
        })
    return rows


def write_jsonl(path: Path, rows: list[dict[str, Any]], *, replace: bool) -> None:
    if path.exists() and not replace:
        raise FileExistsError(f"Refusing to overwrite {path}; use --replace")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".part")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    temporary.replace(path)


def run_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    confidence = Counter("low" if row["layout_confidence"] < .60 else "medium" if row["layout_confidence"] < AUTO_ROUTE_THRESHOLD else "high" for row in rows)
    unknown = [row for row in rows if row["layout_type"] == "unknown"]
    return {
        "rows": len(rows), "threshold": AUTO_ROUTE_THRESHOLD,
        "layout_counts": dict(Counter(row["layout_type"] for row in rows)),
        "confidence_distribution": dict(confidence),
        "unknown_examples": [{"source_path": row["source_path"], "image_sha256": row["image_sha256"],
                              "candidate_layout_type": row["candidate_layout_type"], "layout_confidence": row["layout_confidence"]}
                             for row in unknown[:20]],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--classify-only", action="store_true", help="Required: never invokes OCR or rewrites OCR output")
    parser.add_argument("--dataset", action="append", help="Dataset name, repeatable; default: all")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--workers", type=int, default=min(4, os.cpu_count() or 1),
                        help="Parallel image-analysis workers; OCR is never invoked")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--html-report", action="store_true", help="Also write a browsable HTML report beside the JSONL")
    parser.add_argument("--replace", action="store_true")
    args = parser.parse_args()
    if not args.classify_only:
        parser.error("--classify-only is required")
    output = (PROJECT_ROOT / args.output).resolve() if not args.output.is_absolute() else args.output.resolve()
    rows = classify_entries(args.dataset, args.limit, workers=max(args.workers, 1))
    write_jsonl(output, rows, replace=args.replace)
    summary_path = DEFAULT_SUMMARY if output == DEFAULT_OUTPUT else output.with_suffix(".summary.json")
    summary_path.write_text(json.dumps(run_summary(rows), ensure_ascii=False, indent=2), encoding="utf-8")
    report_path = None
    if args.html_report:
        from scripts.preprocessing.body_image_layout_report import render_report
        report_path = output.with_suffix(".html")
        render_report(rows, report_path, input_path=output)
    print(json.dumps({"output": output.relative_to(PROJECT_ROOT).as_posix(), "summary": summary_path.relative_to(PROJECT_ROOT).as_posix(),
                      "html_report": report_path.relative_to(PROJECT_ROOT).as_posix() if report_path else None,
                      **run_summary(rows)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
