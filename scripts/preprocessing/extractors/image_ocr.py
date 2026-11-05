"""Tesseract TSV layout extraction; conservative, fully ruled tables only."""
from __future__ import annotations

import csv
import io
import re
import subprocess
from collections import defaultdict
from functools import lru_cache

import numpy as np
from PIL import Image, ImageOps

VERSION = '2'


@lru_cache(maxsize=16)
def require_ocr_languages(executable, language='kor+eng', tessdata=None):
    """Reject missing models before Tesseract silently falls back to one language."""
    command = [str(executable), '--list-langs']
    if tessdata:
        command += ['--tessdata-dir', str(tessdata)]
    result = subprocess.run(command, capture_output=True, text=True, timeout=15, check=True)
    installed = {line.strip() for line in result.stdout.splitlines()}
    missing = set(language.split('+')) - installed
    if missing:
        raise RuntimeError('Missing Tesseract languages: ' + ', '.join(sorted(missing)))


def _centers(indices):
    groups = []
    for value in indices:
        if not groups or value > groups[-1][-1] + 1:
            groups.append([])
        groups[-1].append(int(value))
    return [round(sum(group) / len(group)) for group in groups]


def _max_dark_run(values):
    longest = current = 0
    for value in values:
        current = current + 1 if value else 0
        longest = max(longest, current)
    return longest


def _coverage(dark, *, horizontal, at, start, end):
    if horizontal:
        return dark[max(0, at - 2):at + 3, start:end + 1].any(axis=0).mean()
    return dark[start:end + 1, max(0, at - 2):at + 3].any(axis=1).mean()


def _merge_cells(dark, xs, ys, *, conservative=False):
    """Return rectangular cells and their spans from partially ruled borders."""
    columns, rows = len(xs) - 1, len(ys) - 1
    parent = list(range(rows * columns))
    uncertain = False

    def find(index):
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left, right):
        left, right = find(left), find(right)
        if left != right:
            parent[right] = left

    # A missing inner border means that two elementary cells form one span.
    for row in range(rows):
        for column in range(1, columns):
            coverage = _coverage(dark, horizontal=False, at=xs[column], start=ys[row], end=ys[row + 1])
            if coverage < (.05 if conservative else .60):
                union(row * columns + column - 1, row * columns + column)
            elif conservative and coverage < .95:
                uncertain = True
    for row in range(1, rows):
        for column in range(columns):
            coverage = _coverage(dark, horizontal=True, at=ys[row], start=xs[column], end=xs[column + 1])
            if coverage < (.05 if conservative else .60):
                union((row - 1) * columns + column, row * columns + column)
            elif conservative and coverage < .95:
                uncertain = True
    groups = defaultdict(list)
    for row in range(rows):
        for column in range(columns):
            groups[find(row * columns + column)].append((row, column))
    cells, lookup = [], {}
    for members in groups.values():
        top, bottom = min(r for r, _ in members), max(r for r, _ in members)
        left, right = min(c for _, c in members), max(c for _, c in members)
        # Do not claim a non-rectangular cluster is a table cell.
        if len(members) != (bottom - top + 1) * (right - left + 1):
            return (None, None, True) if conservative else None
        cell = {'row': top, 'column': left, 'rowspan': bottom - top + 1, 'colspan': right - left + 1,
                'bbox': [xs[left], ys[top], xs[right + 1], ys[bottom + 1]]}
        cells.append(cell)
        for member in members:
            lookup[member] = (top, left)
    result = sorted(cells, key=lambda cell: (cell['row'], cell['column'])), lookup
    return (*result, uncertain) if conservative else result


def detect_grid(image, *, conservative=False):
    """Detect a fully enclosed ruled table, including ordinary row/col spans."""
    dark = np.asarray(image.convert('L')) < 150
    ys = _centers(np.flatnonzero(dark.mean(axis=1) > .65))
    if len(ys) < 3:
        return None
    height = ys[-1] - ys[0] + 1
    vertical = np.array([_max_dark_run(dark[ys[0]:ys[-1] + 1, x]) >= height * .20 for x in range(dark.shape[1])])
    xs = _centers(np.flatnonzero(vertical))
    if len(xs) < 3 or min(np.diff(xs)) < 12 or min(np.diff(ys)) < 12:
        return None
    # Row boundaries and the outer borders must run across the full table.
    for y in ys:
        if _coverage(dark, horizontal=True, at=y, start=xs[0], end=xs[-1]) < .95:
            return None
    for x in (xs[0], xs[-1]):
        if _coverage(dark, horizontal=False, at=x, start=ys[0], end=ys[-1]) < .95:
            return None
    merged = _merge_cells(dark, xs, ys, conservative=conservative)
    uncertain = False
    if conservative:
        cells, lookup, uncertain = merged
        if uncertain:
            return {'xs': xs, 'ys': ys, 'cells': [], 'lookup': {}, 'verified': False}
        merged = (cells, lookup)
    if merged is None:
        return None
    cells, lookup = merged
    return {'xs': xs, 'ys': ys, 'cells': cells, 'lookup': lookup, 'verified': True}


def layout_blocks(tsv, grid=None):
    header = next(csv.reader(io.StringIO(tsv), delimiter='\t'), [])
    if 'level' not in header or 'text' not in header:
        raise ValueError('Tesseract did not return TSV data')
    lines = defaultdict(list)
    cells = defaultdict(list)
    for word in csv.DictReader(io.StringIO(tsv), delimiter='\t', quoting=csv.QUOTE_NONE):
        if not word.get('level'):
            raise ValueError('Tesseract did not return TSV data')
        if word['level'] != '5' or not (word.get('text') or '').strip():
            continue
        x, y, w, h = (int(word[key]) for key in ('left', 'top', 'width', 'height'))
        item = {'text': word['text'], 'bbox': [x, y, x+w, y+h], 'confidence': float(word['conf'])}
        if grid:
            xs, ys = grid['xs'], grid['ys']
            cx, cy = x+w/2, y+h/2
            col, row = np.searchsorted(xs, cx)-1, np.searchsorted(ys, cy)-1
            if 0 <= col < len(xs)-1 and 0 <= row < len(ys)-1:
                cells[grid['lookup'][int(row), int(col)]].append(item)
                continue
        # A visual line keeps normal prose readable and prevents two separate
        # sentences in one Tesseract paragraph from being merged together.
        lines[tuple(word[k] for k in ('page_num', 'block_num', 'par_num', 'line_num'))].append(item)
    blocks = []
    for words in lines.values():
        boxes = [word['bbox'] for word in words]
        blocks.append({'type': 'ocr_paragraph', 'text': ' '.join(w['text'] for w in words),
                       'bbox': [min(b[0] for b in boxes), min(b[1] for b in boxes),
                                max(b[2] for b in boxes), max(b[3] for b in boxes)],
                       'confidence': sum(w['confidence'] for w in words)/len(words)})
    if grid:
        xs, ys = grid['xs'], grid['ys']
        rows = []
        for row in range(len(ys)-1):
            current = []
            for cell in (item for item in grid['cells'] if item['row'] == row):
                cell_words = cells[cell['row'], cell['column']]
                confidence = (sum(word['confidence'] for word in cell_words) / len(cell_words)
                              if cell_words else None)
                current.append({**cell, 'text': ' '.join(word['text'] for word in cell_words),
                                'ocr_confidence': confidence})
            if current:
                rows.append(current)
        blocks.append({'type': 'ocr_table', 'rows': rows,
                       'bbox': [xs[0], ys[0], xs[-1], ys[-1]],
                       'text': '\n'.join(' | '.join(c['text'] for c in row) for row in rows)})
    return sorted(blocks, key=lambda b: (b['bbox'][1], b['bbox'][0]))


def extract_image(path, *, executable, language='kor+eng', tessdata=None, psm=6,
                  restore_spacing=False, group_regions=False):
    require_ocr_languages(executable, language, tessdata)
    if psm not in {6, 11}:
        raise ValueError('Image OCR supports PSM 6 or 11')
    with Image.open(path) as original:
        image = ImageOps.exif_transpose(original).convert('RGB')
        grid = detect_grid(image)
        # Send exactly the oriented raster used by grid detection to Tesseract.
        data = io.BytesIO()
        image.save(data, format='PNG')
    # Keep PSM 6 as the existing default; use PSM 11 for sparse infographics.
    # Table cells are assigned by coordinates, never by text reading order.
    command = [str(executable), 'stdin', 'stdout', '-l', language, '--psm', str(psm)]
    if tessdata:
        command += ['--tessdata-dir', str(tessdata)]
    # Windows installers can omit the named ``tsv`` config from the search path.
    # The explicit variable is portable and guarantees the documented TSV schema.
    result = subprocess.run(command + ['-c', 'tessedit_create_tsv=1'], input=data.getvalue(), capture_output=True,
                            timeout=180, check=True)
    blocks = layout_blocks(result.stdout.decode('utf-8-sig'), grid)
    low_confidence = sum(block.get('confidence', 100) < 40 for block in blocks)
    output = {'blocks': blocks, 'status': 'needs_review' if blocks else 'empty',
              'warnings': ['OCR_REVIEW_REQUIRED'] + ([] if grid else ['TABLE_LAYOUT_UNVERIFIED']),
              'width': image.width, 'height': image.height,
              'low_confidence_blocks': low_confidence}
    if blocks and low_confidence / len(blocks) >= .1:
        output['warnings'].append('OCR_LOW_CONFIDENCE_BLOCKS')
    if restore_spacing:
        plain = subprocess.run(command, input=data.getvalue(), capture_output=True,
                               timeout=180, check=True).stdout.decode('utf-8-sig')
        lines = defaultdict(list)
        for line in plain.splitlines():
            line = line.strip()
            if line:
                lines[re.sub(r'\s+', '', line)].append(line)
        restored = 0
        for block in blocks:
            if block['type'] != 'ocr_paragraph':
                continue
            matches = lines.get(re.sub(r'\s+', '', block['text']))
            if matches:
                block['text'] = matches.pop(0)
                restored += 1
        output.update(ocr_text=plain.strip(), spacing_restored_blocks=restored)
    if group_regions:
        from scripts.preprocessing.extractors.image_regions import group_ocr_regions

        output['ocr_regions'] = group_ocr_regions(
            image, blocks, executable=executable, language=language, tessdata=tessdata)
        output['warnings'].extend(output['ocr_regions']['warnings'])
    return output


def _nearby_labels(paragraphs, table_bbox, image_height):
    """Find separate OCR lines directly above (title) or below (caption) a table."""
    left, top, right, bottom = table_bbox
    max_gap = max(32, image_height * .08)
    titles, captions = [], []
    for block in paragraphs:
        x1, y1, x2, y2 = block['bbox']
        if y2 <= top:
            gap = top - y2
            target = titles
        elif y1 >= bottom:
            gap = y1 - bottom
            target = captions
        else:
            continue
        overlap = max(0, min(right, x2) - max(left, x1)) / max(1, min(right-left, x2-x1))
        centered = abs((x1+x2)/2 - (left+right)/2) <= (right-left)*.20
        if gap <= max_gap and (overlap >= .35 or centered):
            target.append((gap, block['text']))
    title = min(titles, default=(None, None))[1]
    caption = min(captions, default=(None, None))[1]
    return title, caption


def extract_table_image(path, *, executable, language='kor+eng', tessdata=None):
    """Extract a table only when its ruled cell geometry is conservatively verified."""
    require_ocr_languages(executable, language, tessdata)
    with Image.open(path) as original:
        image = ImageOps.exif_transpose(original).convert('RGB')
        grid = detect_grid(image, conservative=True)
        data = io.BytesIO()
        image.save(data, format='PNG')
    if not grid or not grid.get('verified'):
        bbox = ([int(grid['xs'][0]), int(grid['ys'][0]), int(grid['xs'][-1]), int(grid['ys'][-1])]
                if grid else None)
        return {'tables': [], 'paragraphs': [], 'status': 'needs_review',
                'warnings': ['TABLE_LAYOUT_UNVERIFIED'], 'candidate_table_bbox': bbox,
                'width': image.width, 'height': image.height}

    command = [str(executable), 'stdin', 'stdout', '-l', language, '--psm', '6']
    if tessdata:
        command += ['--tessdata-dir', str(tessdata)]
    result = subprocess.run(command + ['-c', 'tessedit_create_tsv=1'], input=data.getvalue(), capture_output=True,
                            timeout=180, check=True)
    blocks = layout_blocks(result.stdout.decode('utf-8-sig'), grid)
    table_blocks = [block for block in blocks if block.get('type') == 'ocr_table']
    paragraphs = [block for block in blocks if block.get('type') == 'ocr_paragraph']
    warnings = []
    tables = []
    for block in table_blocks:
        cells = [cell for row in block['rows'] for cell in row]
        low_confidence = [cell for cell in cells if cell['text'] and
                          (cell['ocr_confidence'] is None or cell['ocr_confidence'] < 60)]
        if low_confidence:
            warnings.append('CELL_OCR_LOW_CONFIDENCE')
        title, caption = _nearby_labels(paragraphs, block['bbox'], image.height)
        column_count = max((cell['column'] + cell['colspan'] for cell in cells), default=0)
        if not title and block['rows']:
            first_row = block['rows'][0]
            if (len(first_row) == 1 and first_row[0]['row'] == 0 and
                    first_row[0]['column'] == 0 and first_row[0]['colspan'] == column_count):
                title = first_row[0]['text'] or None
        tables.append({**block, 'title': title, 'caption': caption,
                       'ocr_confidence_scale': 'tesseract_0_to_100',
                       'low_confidence_threshold': 60,
                       'structure_confidence': 0.95,
                       'structure_evidence': ['CLOSED_OUTER_BORDER', 'CELL_BOUNDARIES_VERIFIED']})
    if not tables:
        warnings.append('TABLE_LAYOUT_UNVERIFIED')
    status = 'needs_review' if warnings else 'extracted'
    return {'tables': tables, 'paragraphs': paragraphs, 'status': status,
            'warnings': sorted(set(warnings)), 'width': image.width, 'height': image.height}
