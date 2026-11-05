"""Chunk reviewed body-image OCR data by layout and load it into a local TEST store.

Safety: this script never connects to Supabase or touches ``rag_chunks``. It
writes only to ``files/_test_db/test_layout_rag_YYYYMMDD[_suffix]/`` and
embeds with the local sentence-transformers model (no API cost).

Example:
  python -m scripts.vectorization.ingest_layout_data
  python -m scripts.vectorization.ingest_layout_data --input "files/_reviewed/body_image_ocr/*.reviewed.jsonl" --limit 50
"""
from __future__ import annotations

import argparse
from collections import Counter
import glob
import json
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.vectorization.layout_rag import (  # noqa: E402
    DEFAULT_CHUNK_CHARS,
    DEFAULT_CHUNK_OVERLAP,
    REVIEWED_ROOT,
    LocalVectorStore,
    collect_chunks,
    default_store_name,
    embed_texts,
    embedding_text,
    load_embedder,
    load_extraction_index,
    _relative,
)

HARD_MAX_IMAGES = 50
HARD_MAX_CHUNKS = 500


def _resolve(value: str | Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else PROJECT_ROOT / path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--input", action="append",
                        help="Glob of *.reviewed.jsonl files (repeatable). "
                             "Default: files/_reviewed/body_image_ocr/*.reviewed.jsonl")
    parser.add_argument("--table-extractions", default=str(REVIEWED_ROOT / "table_extractions.jsonl"))
    parser.add_argument("--diagram-extractions", default=str(REVIEWED_ROOT / "diagram_extractions.jsonl"))
    parser.add_argument("--store", default=default_store_name(),
                        help="Test store name, must match test_layout_rag_YYYYMMDD[_suffix]")
    parser.add_argument("--limit", type=int, default=HARD_MAX_IMAGES,
                        help=f"Max reviewed images to ingest (capped at {HARD_MAX_IMAGES})")
    parser.add_argument("--chunk-chars", type=int, default=DEFAULT_CHUNK_CHARS)
    parser.add_argument("--chunk-overlap", type=int, default=DEFAULT_CHUNK_OVERLAP)
    parser.add_argument("--replace", action="store_true", help="Rebuild the test store if it already exists")
    parser.add_argument("--dry-run", action="store_true", help="Chunk only; do not embed or write the store")
    args = parser.parse_args(argv)
    if args.limit < 1:
        parser.error("--limit must be positive")
    limit = min(args.limit, HARD_MAX_IMAGES)

    patterns = args.input or [str(REVIEWED_ROOT / "*.reviewed.jsonl")]
    files = sorted({Path(p) for pattern in patterns for p in glob.glob(str(_resolve(pattern)))})
    if not files:
        print(json.dumps({"status": "no_input", "patterns": patterns}, ensure_ascii=False))
        return 2

    chunks, report = collect_chunks(
        files,
        load_extraction_index(_resolve(args.table_extractions)),
        load_extraction_index(_resolve(args.diagram_extractions)),
        limit=limit, max_chars=args.chunk_chars, overlap=args.chunk_overlap,
    )
    if len(chunks) > HARD_MAX_CHUNKS:
        chunks = chunks[:HARD_MAX_CHUNKS]
    summary = {
        "store": args.store,
        "input_files": [_relative(f) for f in files],
        "images_seen": len(report),
        "images_ingested": sum(1 for r in report if r["chunks"]),
        "images_skipped": Counter(r["skip_reason"] for r in report if r["skip_reason"]),
        "chunks": len(chunks),
        "chunks_by_layout": Counter(c["layout_type"] for c in chunks),
    }
    if args.dry_run or not chunks:
        summary["status"] = "dry_run" if args.dry_run else "nothing_to_ingest"
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0 if args.dry_run else 1

    model_name, model = load_embedder()
    vectors = embed_texts(model, [embedding_text(c) for c in chunks])
    store = LocalVectorStore.create(
        args.store, chunks, vectors, replace=args.replace,
        manifest_extra={"embedding_model": model_name, "input_files": summary["input_files"],
                        "images": report},
    )
    summary.update(status="ok", store_path=store.path.relative_to(PROJECT_ROOT).as_posix(),
                   embedding_model=model_name, dimensions=store.manifest["dimensions"])
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
