"""Create selected RAG preprocessing outputs using body-image review decisions.

Example:
  python -m scripts.preprocessing.preprocess_reviewed_body_images --dataset ce \
    --source files/department/정보융합대학/컴퓨터·인공지능공학부/ce/output/json/category/document.json
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.preprocessing.storage import get_preprocessed_root
from scripts.preprocessing.file_preprocessing import (  # noqa: E402
    DEFAULT_CHUNK_OVERLAP,
    DEFAULT_CHUNK_SIZE,
    DEFAULT_OCR_DPI,
    DEFAULT_OCR_LANGUAGE,
    DEFAULT_PDF_OCR_MODE,
    save_preprocessed_file,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", default="ce", help="Dataset name (default: ce)")
    parser.add_argument("--source", action="append", required=True,
                        help="Project-relative crawler JSON path; repeat for selected documents")
    parser.add_argument("--output-root", type=Path,
                        help="Output root (default: files/_reviewed/body_image_ocr/rag_inputs/<dataset>)")
    parser.add_argument("--chunk-size", type=int, default=DEFAULT_CHUNK_SIZE)
    parser.add_argument("--chunk-overlap", type=int, default=DEFAULT_CHUNK_OVERLAP)
    args = parser.parse_args()
    if args.chunk_size < 1 or args.chunk_overlap < 0:
        parser.error("chunk size must be positive and overlap must be non-negative")

    from scripts.crawlers.common.storage import get_dataset_paths
    input_root = get_dataset_paths(PROJECT_ROOT, args.dataset).json.resolve()
    output_root = (args.output_root if args.output_root else
                   get_preprocessed_root(PROJECT_ROOT, args.dataset) / "json").resolve()
    sources: list[Path] = []
    for value in args.source:
        path = (PROJECT_ROOT / value).resolve()
        try:
            path.relative_to(input_root)
        except ValueError:
            parser.error(f"source must be inside {input_root}")
        if not path.is_file() or path.suffix.lower() != ".json":
            parser.error(f"source JSON does not exist: {path}")
        sources.append(path)

    report = []
    failures = 0
    for source in sorted(dict.fromkeys(sources)):
        try:
            saved, output_text = save_preprocessed_file(
                input_file=source,
                input_root=input_root,
                output_root=output_root,
                project_root=PROJECT_ROOT,
                attachment_index={},
                chunk_size=args.chunk_size,
                chunk_overlap=args.chunk_overlap,
                pdf_ocr_mode=DEFAULT_PDF_OCR_MODE,
                ocr_language=DEFAULT_OCR_LANGUAGE,
                ocr_dpi=DEFAULT_OCR_DPI,
                layout="flat",
            )
            if not saved:
                failures += 1
                report.append({"source_path": source.relative_to(PROJECT_ROOT).as_posix(),
                               "status": "skipped", "reason": output_text})
                continue
            output_path = Path(output_text)
            if not output_path.is_absolute():
                output_path = PROJECT_ROOT / output_path
            output = json.loads(output_path.read_text(encoding="utf-8"))
            images = output.get("body_image_provenance", [])
            report.append({
                "source_path": output.get("source_path"),
                "status": "created",
                "output_path": output_path.relative_to(PROJECT_ROOT).as_posix(),
                "included_images": sum(bool(item.get("included")) for item in images),
                "excluded_images": sum(not item.get("included") for item in images),
                "needs_user_review": sum(
                    item.get("review_decision") is None
                    or item.get("review_decision") in {"reprocess", "needs_review"}
                    for item in images
                ),
                "body_image_provenance": images,
            })
        except Exception as exc:  # keep a bad selected document isolated
            failures += 1
            report.append({"source_path": source.relative_to(PROJECT_ROOT).as_posix(),
                           "status": "failed", "reason": f"{type(exc).__name__}: {exc}"})

    print(json.dumps({"processed_documents": len(report) - failures,
                      "failed_or_skipped": failures,
                      "output_root": output_root.relative_to(PROJECT_ROOT).as_posix(),
                      "documents": report}, ensure_ascii=False, indent=2))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
