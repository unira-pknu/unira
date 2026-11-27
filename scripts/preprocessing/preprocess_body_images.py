"""OCR department body images without modifying crawl state.

python -m scripts.preprocessing.preprocess_body_images --dataset ce
python -m scripts.preprocessing.preprocess_body_images --all-body-images
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import subprocess

from bs4 import BeautifulSoup
from scripts.preprocessing.storage import get_preprocessed_root
from scripts.crawlers.common.body_images import collect_body_images, save_body_images
from scripts.crawlers.departments.engine import build_session
from scripts.crawlers.departments.adapters import get_adapter
from scripts.crawlers.departments.config import load_registry
from scripts.crawlers.departments.urls import resolve_url
from scripts.preprocessing.extractors.image_ocr import VERSION, extract_image
from scripts.preprocessing.body_image_layout import enrich_entry
from scripts.crawlers.common.storage import get_dataset_paths, get_dataset_root, iter_dataset_roots

ROOT = Path(__file__).resolve().parents[2]


def saved_image_candidates(soup, doc, config=None):
    if config is None:
        return collect_body_images(soup)
    root = get_adapter(config.adapter).select_body_root(
        soup, page_url=doc.get('url', ''), base_url=config.base_url, detail=doc.get('type') == 'notice',
    )
    return collect_body_images(soup, root=root) if root is not None else []


def matching_originals(candidates, records, page_url):
    if len(candidates) != len(records):
        return False
    for candidate, record in zip(candidates, records):
        if not record.get('saved_path') or not record.get('sha256'):
            return False
        try:
            if (candidate['order'] != record.get('order')
                    or resolve_url(candidate['src'], page_url) != record.get('source_url')):
                return False
        except ValueError:
            # Inline images can be decoded locally again without a request.
            return False
    return True


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.json.part')
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dataset', action='append', help='Repeatable; default: all datasets')
    parser.add_argument(
        '--all-body-images', action='store_true',
        help='Process image candidates from every saved departmental HTML body. Default: only IMAGE_ONLY_REQUIRES_OCR documents.',
    )
    parser.add_argument('--limit', type=int)
    parser.add_argument('--tesseract', default=shutil.which('tesseract') or 'C:/Program Files/Tesseract-OCR/tesseract.exe')
    parser.add_argument('--tessdata')
    parser.add_argument('--language', default='kor+eng')
    parser.add_argument('--force', action='store_true')
    args = parser.parse_args()
    command = [args.tesseract, '--list-langs']
    if args.tessdata:
        command += ['--tessdata-dir', args.tessdata]
    try:
        installed = subprocess.run(command, capture_output=True, text=True, check=True).stdout.splitlines()
        missing = set(args.language.split('+')) - set(installed)
        if missing:
            parser.error('Missing Tesseract languages: ' + ', '.join(sorted(missing)))
    except (OSError, subprocess.CalledProcessError) as exc:
        parser.error(f'Tesseract unavailable: {exc}')
    datasets = args.dataset or sorted({p.name for p in iter_dataset_roots(ROOT) if (p/'output/json').is_dir()})
    registry = load_registry()
    report = []
    # Match crawler request behavior: direct, normal browser headers and no
    # inherited dead proxy. This does not loosen TLS verification or bypass
    # access controls.
    with build_session() as session:
        for dataset in datasets:
            if Path(dataset).name != dataset or dataset in {'.', '..'}:
                parser.error('Invalid dataset')
            output = get_dataset_paths(ROOT, dataset).output
            for path in sorted((output/'json').rglob('*.json')):
                doc = json.loads(path.read_text(encoding='utf-8'))
                if not args.all_body_images and 'IMAGE_ONLY_REQUIRES_OCR' not in doc.get('crawl', {}).get('warnings', []):
                    continue
                if args.limit is not None and len(report) >= args.limit:
                    break
                target = get_preprocessed_root(ROOT, dataset) / 'body_images'/path.relative_to(output/'json')
                entry = {'source_path': path.relative_to(ROOT).as_posix(), 'url': doc.get('url'), 'images': [],
                         'processed_at': datetime.now(timezone.utc).isoformat(), 'ocr_version': VERSION,
                         'language': args.language, 'status': 'needs_review'}
                try:
                    html = (output/'html'/path.relative_to(output/'json')).with_suffix('.html')
                    old = json.loads(target.read_text(encoding='utf-8')) if target.exists() else {}
                    records = doc.get('metadata', {}).get('body_images', [])
                    if html.exists():
                        candidates = saved_image_candidates(
                            BeautifulSoup(html.read_text(encoding='utf-8'), 'html.parser'), doc, registry.get(dataset),
                        )
                        prior = old.get('images', [])
                        # Reuse byte-addressed originals from a prior OCR run.
                        # Match the snapshot's image order and URLs as well as count;
                        # each reused file is hash-verified below before OCR.
                        if matching_originals(candidates, records, doc['url']):
                            pass
                        elif matching_originals(candidates, prior, doc['url']):
                            records = prior
                        else:
                            records = save_body_images(candidates, session=session, page_url=doc['url'],
                                                       output_dir=output/'images', project_root=ROOT)
                    if args.all_body_images and not records:
                        # Normal text-only pages are not OCR targets and do not
                        # get a misleading empty preprocessing result.
                        continue
                    cached = {
                        r.get('sha256'): r for r in old.get('images', [])
                        if r.get('blocks') and r.get('status') == 'needs_review'
                    }
                    for record in records:
                        record = dict(record)
                        try:
                            if record.get('status') != 'saved' and not (record.get('saved_path') and record.get('sha256')):
                                raise ValueError(str(record.get('error') or 'Image not saved'))
                            image_path = (ROOT/record['saved_path']).resolve()
                            image_path.relative_to(output.resolve())
                            digest = hashlib.sha256(image_path.read_bytes()).hexdigest()
                            if digest != record['sha256']:
                                raise ValueError('Image hash mismatch')
                            if not args.force and old.get('ocr_version') == VERSION and old.get('language') == args.language and digest in cached:
                                record = cached[digest]
                            else:
                                record.pop('ocr_error', None)
                                record.update(extract_image(image_path, executable=args.tesseract,
                                                            language=args.language, tessdata=args.tessdata))
                        except Exception as exc:
                            record.update(status='failed', ocr_error=str(exc))
                        entry['images'].append(record)
                    if not records or any(r['status'] in {'failed', 'empty'} for r in entry['images']):
                        entry['status'] = 'partial_failure' if any(r.get('blocks') for r in entry['images']) else 'failed'
                except Exception as exc:
                    entry.update(status='failed', error=str(exc))
                enrich_entry(entry)
                write_json(target, entry)
                report.append({'source_path': entry['source_path'], 'status': entry['status'],
                               'images': len(entry['images']),
                               'blocks': sum(len(r.get('blocks', [])) for r in entry['images'])})
                print(json.dumps(report[-1], ensure_ascii=False), flush=True)
    write_json(ROOT/'var/reviewed/body_image_ocr/latest.json', report)


if __name__ == '__main__':
    main()
