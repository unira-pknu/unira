"""Merge reviewed image-layout labels into the human label JSONL file."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_TARGET = PROJECT_ROOT / "files" / "_reviewed" / "body_image_ocr" / "layout_labels.jsonl"
LAYOUT_TYPES = {"prose", "table", "diagram", "mixed", "no_text", "unknown"}


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows = []
    with path.open(encoding="utf-8") as handle:
        for number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(f"{path}:{number} must contain a JSON object")
            rows.append(value)
    return rows


def merge_labels(input_path: Path, target_path: Path) -> dict[str, int]:
    incoming = _read_jsonl(input_path)
    if not incoming:
        raise ValueError("review export is empty")
    existing = _read_jsonl(target_path)
    by_key: dict[tuple[str, str], dict[str, Any]] = {}
    order: list[tuple[str, str]] = []
    for row in existing:
        key = (str(row.get("source_path", "")), str(row.get("image_sha256", "")))
        if not all(key) or key in by_key:
            raise ValueError("target labels contain a missing or duplicate source_path/image_sha256 key")
        by_key[key] = row
        order.append(key)

    added = updated = 0
    seen: set[tuple[str, str]] = set()
    for row in incoming:
        source_path, image_sha256 = row.get("source_path"), row.get("image_sha256")
        if not isinstance(source_path, str) or not source_path.startswith("files/"):
            raise ValueError("each reviewed label needs a files/-relative source_path")
        if not isinstance(image_sha256, str) or not image_sha256:
            raise ValueError("each reviewed label needs image_sha256")
        reviewer_type = row.get("reviewer_layout_type")
        if reviewer_type not in LAYOUT_TYPES:
            raise ValueError(f"invalid reviewer_layout_type: {reviewer_type!r}")
        key = (source_path, image_sha256)
        if key in seen:
            raise ValueError("review export contains duplicate image keys")
        seen.add(key)
        values = {
            "source_path": source_path,
            "image_sha256": image_sha256,
            "saved_path": row.get("saved_path"),
            "image_url": row.get("image_url") or row.get("source_url"),
            "current_warnings": row.get("current_warnings", []),
            "suggested_layout_type": row.get("suggested_layout_type", "unknown"),
            "reviewer_layout_type": reviewer_type,
            "reviewer_notes": row.get("reviewer_notes", ""),
            "expected_text_or_structure": row.get("expected_text_or_structure", ""),
        }
        if not isinstance(values["current_warnings"], list):
            raise ValueError("current_warnings must be an array")
        if values["suggested_layout_type"] not in LAYOUT_TYPES:
            values["suggested_layout_type"] = "unknown"
        if key in by_key:
            by_key[key].update(values)
            updated += 1
        else:
            by_key[key] = values
            order.append(key)
            added += 1

    target_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = target_path.with_suffix(target_path.suffix + ".part")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        for key in order:
            handle.write(json.dumps(by_key[key], ensure_ascii=False, sort_keys=True) + "\n")
    temporary.replace(target_path)
    return {"reviewed": len(incoming), "added": added, "updated": updated}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True, help="JSONL downloaded from the HTML review page")
    parser.add_argument("--target", type=Path, default=DEFAULT_TARGET, help="Human layout-label JSONL")
    args = parser.parse_args()
    input_path = args.input if args.input.is_absolute() else PROJECT_ROOT / args.input
    target_path = args.target if args.target.is_absolute() else PROJECT_ROOT / args.target
    counts = merge_labels(input_path, target_path)
    print(json.dumps({"target": target_path.relative_to(PROJECT_ROOT).as_posix(), **counts}, ensure_ascii=False))


if __name__ == "__main__":
    main()
