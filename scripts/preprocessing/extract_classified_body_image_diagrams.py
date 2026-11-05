"""Extract text nodes and visually verified connectors from diagram images.

Examples:
  python -m scripts.preprocessing.extract_classified_body_image_diagrams --dataset ce --limit 10
  python -m scripts.preprocessing.extract_classified_body_image_diagrams --dataset ce --source-path files/department/정보융합대학/컴퓨터·인공지능공학부/ce/output/json/학부안내/454c014f68226924.json --include-uncertain-layout
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
from scripts.preprocessing.extractors.diagram_ocr import VERSION, extract_diagram_image

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CLASSIFICATIONS = ROOT / 'files/_reviewed/body_image_ocr/layout_classifications.jsonl'
DEFAULT_OUTPUT = ROOT / 'files/_reviewed/body_image_ocr/diagram_extractions.jsonl'


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
    from scripts.crawlers.common.storage import get_dataset_root
    base = (get_dataset_root(ROOT, dataset) / 'output' / 'json').resolve()
    if source.is_relative_to(ROOT / 'files' / dataset):
        base = ROOT / 'files' / dataset / 'output' / 'json'
    try:
        relative = source.relative_to(base)
    except ValueError:
        return None
    return get_preprocessed_root(ROOT, dataset) / 'body_images' / relative


def _existing_ocr(row: dict) -> tuple[list[dict], str | None, str | None, bool]:
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


def _extract_one(row: dict, *, executable: str, language: str, tessdata: str | None,
                 uncertain_layout: bool) -> dict:
    dataset = str(row.get('dataset') or '')
    digest = str(row.get('image_sha256') or '').lower()
    warnings = ['LAYOUT_CLASSIFICATION_UNCERTAIN'] if uncertain_layout else []
    try:
        if Path(dataset).name != dataset or dataset in {'.', '..'}:
            raise ValueError('Invalid dataset in classification record')
        image_path = _safe_project_path(str(row.get('saved_path') or ''))
        from scripts.crawlers.common.storage import get_dataset_root
        dataset_images = (get_dataset_root(ROOT, dataset) / 'output' / 'images').resolve()
        legacy_images = (ROOT / 'files' / dataset / 'output' / 'images').resolve()
        if image_path.is_relative_to(legacy_images):
            dataset_images = legacy_images
        image_path.relative_to(dataset_images)
        if not image_path.is_file():
            raise FileNotFoundError('Classified image file is missing')
        if hashlib.sha256(image_path.read_bytes()).hexdigest() != digest:
            raise ValueError('Image SHA-256 does not match the classification record')
    except (OSError, ValueError) as exc:
        return {
            'dataset': dataset, 'source_path': row.get('source_path'),
            'image_sha256': row.get('image_sha256'), 'saved_path': row.get('saved_path'),
            'layout_type': row.get('layout_type'), 'layout_confidence': row.get('layout_confidence'),
            'status': 'needs_review', 'warnings': ['DIAGRAM_SOURCE_UNAVAILABLE'], 'error': str(exc),
            'existing_ocr_paragraphs': [],
            'result': {'paragraphs': [], 'diagram': {'nodes': [], 'edges': [], 'unverified_edges': [],
                                                      'unassigned_text_blocks': [], 'diagram_title': None}},
        }
    paragraphs, sidecar, existing_status, found = _existing_ocr(row)
    if not found:
        warnings.append('EXISTING_OCR_RESULT_NOT_FOUND')
    try:
        diagram = extract_diagram_image(image_path, executable=executable,
                                        language=language, tessdata=tessdata,
                                        existing_paragraphs=paragraphs)
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        diagram = {'nodes': [], 'edges': [], 'unverified_edges': [], 'unassigned_text_blocks': [],
                   'diagram_title': None, 'warnings': ['DIAGRAM_OCR_FAILED'], 'error': str(exc)}
    warnings.extend(diagram.get('warnings', []))
    classification_uncertain = uncertain_layout or row.get('layout_type') != 'diagram'
    status = 'needs_review' if warnings else 'extracted'
    return {
        'dataset': dataset, 'source_path': row.get('source_path'),
        'image_sha256': digest, 'saved_path': row.get('saved_path'),
        'source_url': row.get('source_url'), 'layout_type': row.get('layout_type'),
        'candidate_layout_type': row.get('candidate_layout_type'),
        'layout_confidence': row.get('layout_confidence'),
        'classification_evidence': row.get('classification_evidence', []),
        'layout_classification_uncertain': classification_uncertain,
        'existing_ocr_path': sidecar, 'existing_ocr_status': existing_status,
        'existing_ocr_paragraphs': paragraphs,
        'extractor': 'diagram_ocr', 'extractor_version': VERSION,
        'processed_at': datetime.now(timezone.utc).isoformat(),
        'status': status, 'warnings': sorted(set(warnings)),
        'result': {'paragraphs': paragraphs,
                   'diagram': {key: diagram.get(key) for key in (
                       'nodes', 'edges', 'unverified_edges', 'unassigned_text_blocks', 'diagram_title')}},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--classification-input', type=Path, default=DEFAULT_CLASSIFICATIONS)
    parser.add_argument('--output', type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument('--dataset', action='append', help='Repeatable; default: all matching datasets')
    parser.add_argument('--source-path', action='append', help='Repeatable source document path filter')
    parser.add_argument('--image-sha256', action='append', help='Repeatable image hash filter')
    parser.add_argument('--min-confidence', type=float, default=0.90)
    parser.add_argument('--limit', type=int, help='Maximum number of matching images to process')
    parser.add_argument('--include-uncertain-layout', action='store_true',
                        help='Allow explicitly selected candidate_layout_type=diagram records')
    parser.add_argument('--tesseract', default=shutil.which('tesseract') or
                        'C:/Program Files/Tesseract-OCR/tesseract.exe')
    parser.add_argument('--tessdata')
    parser.add_argument('--language', default='kor+eng')
    args = parser.parse_args()
    if not 0 <= args.min_confidence <= 1:
        parser.error('--min-confidence must be between 0 and 1')
    if args.limit is not None and args.limit < 1:
        parser.error('--limit must be positive')
    if args.include_uncertain_layout and not (args.dataset or args.source_path or args.image_sha256):
        parser.error('--include-uncertain-layout requires an explicit target filter')
    if not args.classification_input.is_file():
        parser.error(f'Classification JSONL not found: {args.classification_input}')
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
    for row in _read_jsonl(args.classification_input.resolve()):
        regular_diagram = row.get('layout_type') == 'diagram'
        candidate_override = (args.include_uncertain_layout and not regular_diagram and
                              row.get('candidate_layout_type') == 'diagram')
        if not (regular_diagram or candidate_override):
            continue
        try:
            confidence = float(row.get('layout_confidence'))
        except (TypeError, ValueError):
            confidence = 0.0
        if regular_diagram and confidence < args.min_confidence:
            continue
        if datasets and row.get('dataset') not in datasets:
            continue
        if sources and str(row.get('source_path', '')).replace('\\', '/') not in sources:
            continue
        if hashes and str(row.get('image_sha256', '')).lower() not in hashes:
            continue
        selected.append((row, candidate_override))
    if args.limit is not None:
        selected = selected[:args.limit]

    # A repeated asset can be referenced by several documents. Extract each hash
    # once and copy the result to each source/image key without another OCR pass.
    cache = {}
    results = []
    for row, uncertain in selected:
        digest = str(row.get('image_sha256') or '').lower()
        if digest not in cache:
            cache[digest] = _extract_one(row, executable=args.tesseract, language=args.language,
                                         tessdata=args.tessdata, uncertain_layout=uncertain)
        result = dict(cache[digest])
        paragraphs, sidecar, existing_status, found = _existing_ocr(row)
        result.update({'dataset': row.get('dataset'), 'source_path': row.get('source_path'),
                       'existing_ocr_path': sidecar, 'existing_ocr_status': existing_status,
                       'existing_ocr_paragraphs': paragraphs})
        result['result'] = {**result['result'], 'paragraphs': paragraphs}
        if not found:
            result['warnings'] = sorted(set(result.get('warnings', []) + ['EXISTING_OCR_RESULT_NOT_FOUND']))
            result['status'] = 'needs_review'
        results.append(result)

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

    nodes = [node for item in results for node in item.get('result', {}).get('diagram', {}).get('nodes', [])]
    edges = [edge for item in results for edge in item.get('result', {}).get('diagram', {}).get('edges', [])]
    unverified = [edge for item in results for edge in item.get('result', {}).get('diagram', {}).get('unverified_edges', [])]
    print(json.dumps({'processed_images': len(results), 'nodes': len(nodes),
                      'confirmed_edges': len(edges), 'unverified_edges': len(unverified),
                      'needs_review': sum(item.get('status') == 'needs_review' for item in results),
                      'output': output.relative_to(ROOT).as_posix()}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    sys.exit(main())
