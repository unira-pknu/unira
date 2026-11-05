"""Create a balanced, human-label-only sample for body-image layout routing."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from scripts.preprocessing.storage import get_preprocessed_root
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT = PROJECT_ROOT / "files" / "_reviewed" / "body_image_ocr" / "layout_labels.jsonl"
LAYOUT_TYPES = {"prose", "table", "diagram", "mixed", "no_text", "unknown"}
KNOWN_DIAGRAM_URLS = {"https://ce.pknu.ac.kr/ce/7086"}


def _bucket_size(image: dict[str, Any]) -> str:
    longest = max(int(image.get("width") or 0), int(image.get("height") or 0))
    if longest < 800:
        return "small"
    if longest < 1600:
        return "medium"
    return "large"


def _confidence(image: dict[str, Any]) -> float | None:
    blocks = image.get("blocks") if isinstance(image.get("blocks"), list) else []
    values = [block.get("confidence") for block in blocks if isinstance(block, dict) and isinstance(block.get("confidence"), (int, float))]
    return round(sum(values) / len(values), 3) if values else None


def _bucket_confidence(value: float | None) -> str:
    if value is None:
        return "no_text"
    if value < 60:
        return "low"
    if value < 85:
        return "medium"
    return "high"


def _warning_bucket(warnings: list[str]) -> str:
    if not warnings:
        return "none"
    if "TABLE_LAYOUT_UNVERIFIED" in warnings:
        return "layout_unverified"
    return warnings[0]


def _suggest_layout(entry: dict[str, Any], image: dict[str, Any]) -> tuple[str, str]:
    result = image.get("result") if isinstance(image.get("result"), dict) else {}
    paragraphs = result.get("paragraphs") if isinstance(result.get("paragraphs"), list) else []
    tables = result.get("tables") if isinstance(result.get("tables"), list) else []
    if entry.get("url") in KNOWN_DIAGRAM_URLS:
        return "diagram", "known_curriculum_diagram_seed"
    if tables and paragraphs:
        return "mixed", "existing_table_and_paragraph_blocks"
    if tables:
        return "table", "existing_table_blocks"
    if image.get("status") == "empty" or not image.get("blocks"):
        return "no_text", "ocr_returned_no_blocks"
    if "TABLE_LAYOUT_UNVERIFIED" not in image.get("warnings", []):
        return "prose", "paragraph_blocks_without_layout_warning"
    return "unknown", "layout_requires_human_classification"


def _image_only(source_path: str) -> bool:
    source = PROJECT_ROOT / source_path
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return "IMAGE_ONLY_REQUIRES_OCR" in payload.get("crawl", {}).get("warnings", [])


def collect_candidates(datasets: list[str] | None = None) -> list[dict[str, Any]]:
    candidates = []
    from scripts.crawlers.common.storage import get_dataset_root, iter_dataset_roots
    roots = [get_preprocessed_root(PROJECT_ROOT, dataset) / "body_images" for dataset in datasets] if datasets else [
        get_preprocessed_root(PROJECT_ROOT, path.name) / "body_images" for path in iter_dataset_roots(PROJECT_ROOT)
        if (get_preprocessed_root(PROJECT_ROOT, path.name) / "body_images").is_dir()
    ]
    for root in sorted(roots):
        if not root.is_dir():
            continue
        dataset = root.parent.name
        for path in sorted(root.rglob("*.json")):
            try:
                entry = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            source_path = entry.get("source_path")
            if not isinstance(source_path, str):
                continue
            is_image_only = _image_only(source_path)
            for image in entry.get("images", []):
                if not isinstance(image, dict) or image.get("status") not in {"needs_review", "empty"}:
                    continue
                digest = image.get("sha256")
                saved_path = image.get("saved_path")
                if not isinstance(digest, str) or not isinstance(saved_path, str) or not (PROJECT_ROOT / saved_path).is_file():
                    continue
                confidence = _confidence(image)
                warnings = [warning for warning in image.get("warnings", []) if isinstance(warning, str)]
                suggested, reason = _suggest_layout(entry, image)
                if suggested not in LAYOUT_TYPES:
                    raise ValueError(f"invalid suggested layout type: {suggested}")
                candidates.append({
                    "source_path": source_path,
                    "image_sha256": digest,
                    "saved_path": saved_path,
                    "image_url": image.get("source_url") or image.get("final_url"),
                    "source_url": image.get("source_url"),
                    "current_warnings": warnings,
                    "suggested_layout_type": suggested,
                    "reviewer_layout_type": None,
                    "reviewer_notes": None,
                    "expected_text_or_structure": None,
                    "dataset": dataset,
                    "ocr_status": image.get("status"),
                    "image_width": image.get("width"),
                    "image_height": image.get("height"),
                    "ocr_confidence": confidence,
                    "image_only_requires_ocr": is_image_only,
                    "sampling_stratum": {
                        "size": _bucket_size(image),
                        "confidence": _bucket_confidence(confidence),
                        "warnings": _warning_bucket(warnings),
                        "image_only": "yes" if is_image_only else "no",
                    },
                    "suggestion_reason": reason,
                })
    return candidates


def balanced_sample(candidates: list[dict[str, Any]], size: int) -> list[dict[str, Any]]:
    """Balance each observable stratum, while keeping selection deterministic."""
    if size < 1:
        raise ValueError("sample size must be positive")
    remaining = sorted(candidates, key=lambda item: hashlib.sha256(
        f"{item['source_path']}#{item['image_sha256']}".encode("utf-8")).hexdigest())
    selected: list[dict[str, Any]] = []
    counts: dict[tuple[str, str], int] = {}
    seen_hashes = set()

    def add_counts(item: dict[str, Any]) -> None:
        stratum = item["sampling_stratum"]
        for key, value in (("dataset", item["dataset"]), ("size", stratum["size"]),
                           ("confidence", stratum["confidence"]), ("warnings", stratum["warnings"]),
                           ("image_only", stratum["image_only"])):
            counts[key, value] = counts.get((key, value), 0) + 1

    def choose(options: list[dict[str, Any]]) -> dict[str, Any]:
        def score(item: dict[str, Any]) -> tuple[int, int, int, int, int, str]:
            stratum = item["sampling_stratum"]
            return (
                counts.get(("dataset", item["dataset"]), 0),
                counts.get(("size", stratum["size"]), 0),
                counts.get(("confidence", stratum["confidence"]), 0),
                counts.get(("warnings", stratum["warnings"]), 0),
                counts.get(("image_only", stratum["image_only"]), 0),
                item["image_sha256"],
            )
        return min(options, key=score)

    # IMAGE_ONLY is rare in the source corpus, so reserve 20% of the review
    # sample (or every available candidate below that threshold).
    image_only_target = min(sum(item["image_only_requires_ocr"] for item in remaining), max(1, size // 5))
    for _ in range(image_only_target):
        options = [item for item in remaining if item["image_only_requires_ocr"] and item["image_sha256"] not in seen_hashes]
        if not options:
            break
        item = choose(options)
        selected.append(item)
        seen_hashes.add(item["image_sha256"])
        add_counts(item)

    # Reserve one representative of each safe existing suggestion when available.
    for layout_type in ("table", "mixed", "diagram", "prose", "no_text"):
        options = [item for item in remaining if item["suggested_layout_type"] == layout_type and item["image_sha256"] not in seen_hashes]
        if options and len(selected) < size:
            item = options[0]
            selected.append(item)
            seen_hashes.add(item["image_sha256"])
            add_counts(item)
    while remaining and len(selected) < size:
        eligible = [item for item in remaining if item["image_sha256"] not in seen_hashes]
        if not eligible:
            break
        item = choose(eligible)
        selected.append(item)
        seen_hashes.add(item["image_sha256"])
        add_counts(item)
    return selected


def write_jsonl(path: Path, items: list[dict[str, Any]]) -> None:
    if path.exists():
        raise FileExistsError(f"Refusing to overwrite existing labels: {path}. Use --replace after preserving reviews.")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".part")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        for item in items:
            handle.write(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n")
    temporary.replace(path)


def summary(items: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    dimensions = {"dataset": {}, "suggested_layout_type": {}, "size": {}, "confidence": {}, "warnings": {}, "image_only": {}}
    for item in items:
        stratum = item["sampling_stratum"]
        values = {
            "dataset": item["dataset"], "suggested_layout_type": item["suggested_layout_type"],
            "size": stratum["size"], "confidence": stratum["confidence"],
            "warnings": stratum["warnings"], "image_only": stratum["image_only"],
        }
        for dimension, value in values.items():
            dimensions[dimension][value] = dimensions[dimension].get(value, 0) + 1
    return dimensions


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", action="append", help="Dataset name, repeatable; default: all")
    parser.add_argument("--sample-size", type=int, default=120)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--replace", action="store_true", help="Replace an existing labels file")
    args = parser.parse_args()
    output = (PROJECT_ROOT / args.output).resolve() if not args.output.is_absolute() else args.output.resolve()
    if output.exists() and args.replace:
        output.unlink()
    candidates = collect_candidates(args.dataset)
    selected = balanced_sample(candidates, min(args.sample_size, len(candidates)))
    write_jsonl(output, selected)
    print(json.dumps({"output": output.relative_to(PROJECT_ROOT).as_posix(), "candidates": len(candidates),
                      "selected": len(selected), "distribution": summary(selected)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
