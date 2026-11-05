"""Preprocess files from any dataset root into RAG chunk JSON.

This is a dataset-neutral CLI for attachment/file preprocessing. It preserves
the current CE output format by delegating to the existing file preprocessing
implementation while exposing generic input/output root arguments for future
department datasets.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.preprocessing.file_preprocessing import (  # noqa: E402
    DEFAULT_CHUNK_OVERLAP,
    DEFAULT_CHUNK_SIZE,
    DEFAULT_OCR_DPI,
    DEFAULT_OCR_LANGUAGE,
    DEFAULT_PDF_OCR_MODE,
    SUPPORTED_EXTS,
    parse_file_exts,
    run_batch,
)

LOG_DIR = PROJECT_ROOT / "logs"
LOG_FILE = LOG_DIR / "preprocess_files.log"


def configure_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(LOG_FILE, encoding="utf-8"),
        ],
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Preprocess files under an arbitrary dataset root into RAG chunk JSON."
    )
    parser.add_argument("--input-root", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument(
        "--output-json-root",
        type=Path,
        default=PROJECT_ROOT / "files" / "_none" / "output" / "json",
        help=(
            "Optional crawled JSON root used to attach source metadata. "
            "If omitted or missing, files are processed with attachment-only provenance."
        ),
    )
    parser.add_argument(
        "--failed-from-log",
        type=Path,
        default=None,
        help="Reprocess only failed paths parsed from a preprocessing log.",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--changed-only",
        action="store_true",
        help="Skip files whose preprocessed JSON is newer than the input file.",
    )
    parser.add_argument("--chunk-size", type=int, default=DEFAULT_CHUNK_SIZE)
    parser.add_argument("--chunk-overlap", type=int, default=DEFAULT_CHUNK_OVERLAP)
    parser.add_argument(
        "--pdf-ocr",
        choices=["auto", "never", "always"],
        default=DEFAULT_PDF_OCR_MODE,
        help="PDF OCR fallback mode.",
    )
    parser.add_argument("--ocr-language", default=DEFAULT_OCR_LANGUAGE)
    parser.add_argument("--ocr-dpi", type=int, default=DEFAULT_OCR_DPI)
    parser.add_argument(
        "--layout",
        choices=["by_ext", "flat"],
        default="by_ext",
        help="Output path layout. by_ext matches the existing attachment preprocessing layout.",
    )
    parser.add_argument(
        "--file-ext",
        "--file-exts",
        dest="file_exts",
        nargs="+",
        default=None,
        help="Limit preprocessing to these extensions, e.g. --file-ext pdf hwp or --file-ext pdf,hwp.",
    )
    args = parser.parse_args()

    configure_logging()
    file_exts = parse_file_exts(args.file_exts)
    unsupported_exts = sorted((file_exts or set()) - SUPPORTED_EXTS)
    if unsupported_exts:
        parser.error(
            "unsupported --file-ext value(s): "
            + ", ".join(unsupported_exts)
            + ". Supported: "
            + ", ".join(sorted(SUPPORTED_EXTS))
        )

    run_batch(
        input_root=args.input_root,
        output_root=args.output_root,
        output_json_root=args.output_json_root,
        project_root=PROJECT_ROOT,
        failed_from_log=args.failed_from_log.resolve() if args.failed_from_log else None,
        dry_run=args.dry_run,
        chunk_size=args.chunk_size,
        chunk_overlap=args.chunk_overlap,
        pdf_ocr_mode=args.pdf_ocr,
        ocr_language=args.ocr_language,
        ocr_dpi=args.ocr_dpi,
        layout=args.layout,
        file_exts=file_exts,
        changed_only=args.changed_only,
    )


if __name__ == "__main__":
    main()
