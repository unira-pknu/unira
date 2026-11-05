"""Extract ruled tables from high-confidence body-image layout classifications.

Examples:
  python -m scripts.preprocessing.extract_classified_body_image_tables --dataset ce --limit 10
  python -m scripts.preprocessing.extract_classified_body_image_tables --image-sha256 <sha256>
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys

from scripts.preprocessing.storage import get_preprocessed_root
from scripts.preprocessing.extractors.image_ocr import VERSION, extract_table_image
from scripts.crawlers.common.storage import get_dataset_root

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CLASSIFICATIONS = ROOT / 'files/_reviewed/body_image_ocr/layout_classifications.jsonl'
DEFAULT_OUTPUT = ROOT / 'files/_reviewed/body_image_ocr/table_extractions.jsonl'


def _read_jsonl(path: Path) -> list[dict]:
    rows = []
    with path.open(encoding='utf-8') as handle:
        for line_number, line in enumerate(handle, start=1):
            if line.strip():
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError as exc:
                    raise ValueError(f'Invalid JSONL at {path}:{line_number}: {exc}') from exc
    return rows


def _safe_project_path(value: str) -> Path:
    path = (ROOT / value).resolve()
    path.relative_to(ROOT)
    return path


def _ocr_sidecar_path(source_path: str, dataset: str) -> Path | None:
    source = _safe_project_path(source_path)
    base = (get_dataset_root(ROOT, dataset) / 'output' / 'json').resolve()
    if source.is_relative_to(ROOT / 'files' / dataset):
        base = ROOT / 'files' / dataset / 'output' / 'json'
    try:
        relative = source.relative_to(base)
    except ValueError:
        return None
    return get_preprocessed_root(ROOT, dataset) / 'body_images' / relative


def _existing_paragraphs(row: dict) -> tuple[list[dict], str | None, str | None, bool]:
    dataset = str(row.get('dataset') or '')
    sidecar = _ocr_sidecar_path(str(row.get('source_path') or ''), dataset)
    if sidecar is None or not sidecar.is_file():
        return [], None, None, False
    try:
        entry = json.loads(sidecar.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        return [], sidecar.relative_to(ROOT).as_posix(), None, False
    image = next((item for item in entry.get('images', [])
                  if item.get('sha256') == row.get('image_sha256')), None)
    if image is None:
        return [], sidecar.relative_to(ROOT).as_posix(), None, False
    result = image.get('result') if isinstance(image.get('result'), dict) else {}
    paragraphs = result.get('paragraphs')
    if not isinstance(paragraphs, list) or not paragraphs:
        paragraphs = [block for block in image.get('blocks', [])
                      if block.get('type') == 'ocr_paragraph']
    return list(paragraphs), sidecar.relative_to(ROOT).as_posix(), image.get('status'), True


def _extract_one(row: dict, *, executable: str, language: str, tessdata: str | None) -> dict:
    dataset = str(row.get('dataset') or '')
    digest = str(row.get('image_sha256') or '').lower()
    warnings: list[str] = []
    try:
        if Path(dataset).name != dataset or dataset in {'.', '..'}:
            raise ValueError('Invalid dataset in classification record')
        image_path = _safe_project_path(str(row.get('saved_path') or ''))
        dataset_images = (get_dataset_root(ROOT, dataset) / 'output' / 'images').resolve()
        legacy_images = (ROOT / 'files' / dataset / 'output' / 'images').resolve()
        if image_path.is_relative_to(legacy_images):
            dataset_images = legacy_images
        image_path.relative_to(dataset_images)
        if not image_path.is_file():
            raise FileNotFoundError('Classified image file is missing')
        actual_digest = hashlib.sha256(image_path.read_bytes()).hexdigest()
        if actual_digest != digest:
            raise ValueError('Image SHA-256 does not match the classification record')
    except (OSError, ValueError) as exc:
        warnings.append('TABLE_SOURCE_UNAVAILABLE')
        return {
            'dataset': dataset, 'source_path': row.get('source_path'),
            'image_sha256': row.get('image_sha256'), 'source_url': row.get('source_url'),
            'saved_path': row.get('saved_path'), 'layout_type': 'table',
            'layout_confidence': row.get('layout_confidence'), 'status': 'needs_review',
            'warnings': warnings, 'error': str(exc), 'existing_ocr_paragraphs': [],
            'result': {'paragraphs': [], 'tables': []},
        }

    paragraphs, sidecar_path, existing_status, found = _existing_paragraphs(row)
    if not found:
        warnings.append('EXISTING_OCR_RESULT_NOT_FOUND')
    try:
        extracted = extract_table_image(image_path, executable=executable,
                                        language=language, tessdata=tessdata)
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        extracted = {'tables': [], 'paragraphs': [], 'status': 'needs_review',
                     'warnings': ['TABLE_OCR_FAILED'], 'error': str(exc)}
    warnings.extend(extracted.get('warnings', []))
    status = 'needs_review' if warnings else extracted.get('status', 'needs_review')
    table_result = {
        'dataset': dataset, 'source_path': row.get('source_path'), 'image_sha256': digest,
        'saved_path': row.get('saved_path'), 'source_url': row.get('source_url'),
        'layout_type': 'table', 'layout_confidence': row.get('layout_confidence'),
        'classification_evidence': row.get('classification_evidence', []),
        'existing_ocr_path': sidecar_path,
        'existing_ocr_status': existing_status,
        'existing_ocr_paragraphs': paragraphs,
        'extractor': 'table_ocr', 'extractor_version': VERSION,
        'processed_at': datetime.now(timezone.utc).isoformat(),
        'status': status, 'warnings': sorted(set(warnings)),
        'result': {'paragraphs': paragraphs, 'tables': extracted.get('tables', [])},
    }
    return table_result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--classification-input', type=Path, default=DEFAULT_CLASSIFICATIONS)
    parser.add_argument('--output', type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument('--dataset', action='append', help='Repeatable; default: all matching datasets')
    parser.add_argument('--source-path', action='append', help='Repeatable source document path filter')
    parser.add_argument('--image-sha256', action='append', help='Repeatable image hash filter')
    parser.add_argument('--min-confidence', type=float, default=0.90)
    parser.add_argument('--limit', type=int, help='Maximum number of matching images to process')
    parser.add_argument('--tesseract', default=shutil.which('tesseract') or
                        'C:/Program Files/Tesseract-OCR/tesseract.exe')
    parser.add_argument('--tessdata', help='Tesseract data directory')
    parser.add_argument('--language', default='kor+eng')
    args = parser.parse_args()
    if not 0 <= args.min_confidence <= 1:
        parser.error('--min-confidence must be between 0 and 1')
    if args.limit is not None and args.limit < 1:
        parser.error('--limit must be positive')
    classifications = args.classification_input.resolve()
    if not classifications.is_file():
        parser.error(f'Classification JSONL not found: {classifications}')
    try:
        command = [args.tesseract, '--list-langs']
        if args.tessdata:
            command += ['--tessdata-dir', args.tessdata]
        installed = subprocess.run(command, capture_output=True, text=True, check=True).stdout.splitlines()
        missing = set(args.language.split('+')) - set(installed)
        if missing:
            parser.error('Missing Tesseract languages: ' + ', '.join(sorted(missing)))
    except (OSError, subprocess.CalledProcessError) as exc:
        parser.error(f'Tesseract unavailable: {exc}')

    datasets = set(args.dataset or [])
    sources = {value.replace('\\', '/') for value in args.source_path or []}
    hashes = {value.lower() for value in args.image_sha256 or []}
    selected = []
    for row in _read_jsonl(classifications):
        if row.get('layout_type') != 'table':
            continue
        try:
            confidence = float(row.get('layout_confidence'))
        except (TypeError, ValueError):
            continue
        if confidence < args.min_confidence:
            continue
        if datasets and row.get('dataset') not in datasets:
            continue
        if sources and str(row.get('source_path', '')).replace('\\', '/') not in sources:
            continue
        if hashes and str(row.get('image_sha256', '')).lower() not in hashes:
            continue
        selected.append(row)
    if args.limit is not None:
        selected = selected[:args.limit]

    results = [_extract_one(row, executable=args.tesseract, language=args.language, tessdata=args.tessdata)
               for row in selected]
    output = args.output.resolve()
    try:
        output.relative_to(ROOT)
    except ValueError:
        parser.error('Output must be inside the repository')
    prior = _read_jsonl(output) if output.is_file() else []
    merged = {(str(item.get('source_path')), str(item.get('image_sha256'))): item for item in prior}
    merged.update({(str(item.get('source_path')), str(item.get('image_sha256'))): item for item in results})
    output.parent.mkdir(parents=True, exist_ok=True)
    temp = output.with_suffix(output.suffix + '.part')
    temp.write_text(''.join(json.dumps(item, ensure_ascii=False) + '\n'
                              for item in merged.values()), encoding='utf-8')
    temp.replace(output)

    tables = [table for item in results for table in item.get('result', {}).get('tables', [])]
    spans = [table for table in tables if any(
        cell.get('rowspan', 1) > 1 or cell.get('colspan', 1) > 1
        for row in table.get('rows', []) for cell in row)]
    summary = {
        'processed_images': len(results), 'extracted_tables': len(tables),
        'tables_with_rowspan_or_colspan': len(spans),
        'images_needing_review': sum(item.get('status') == 'needs_review' for item in results),
        'output': output.relative_to(ROOT).as_posix(),
    }
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    sys.exit(main())
