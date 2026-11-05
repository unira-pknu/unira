"""Conservative text-node and connector extraction for diagram images."""
from __future__ import annotations

import csv
import io
import math
import subprocess
from collections import defaultdict

import numpy as np
from PIL import Image, ImageOps

from scripts.preprocessing.extractors.image_ocr import require_ocr_languages

VERSION = '1'


def _read_words(tsv: str) -> list[dict]:
    header = next(csv.reader(io.StringIO(tsv), delimiter='\t'), [])
    if 'level' not in header or 'text' not in header:
        raise ValueError('Tesseract did not return TSV data')
    words = []
    for row in csv.DictReader(io.StringIO(tsv), delimiter='\t', quoting=csv.QUOTE_NONE):
        if row.get('level') != '5' or not (row.get('text') or '').strip():
            continue
        x, y, width, height = (int(row[key]) for key in ('left', 'top', 'width', 'height'))
        confidence = float(row['conf'])
        if width <= 0 or height <= 0 or confidence < 0:
            continue
        words.append({'text': row['text'].strip(), 'bbox': [x, y, x + width, y + height],
                      'confidence': confidence,
                      'line': (row.get('block_num'), row.get('par_num'), row.get('line_num'))})
    return words


def _deduplicate_words(words: list[dict]) -> list[dict]:
    kept = []
    for word in sorted(words, key=lambda item: item['confidence'], reverse=True):
        x1, y1, x2, y2 = word['bbox']
        duplicate = False
        for prior in kept:
            a1, b1, a2, b2 = prior['bbox']
            intersection = max(0, min(x2, a2) - max(x1, a1)) * max(0, min(y2, b2) - max(y1, b1))
            area = min((x2 - x1) * (y2 - y1), (a2 - a1) * (b2 - b1))
            if area and intersection / area > .78:
                duplicate = True
                break
        if not duplicate:
            kept.append(word)
    return sorted(kept, key=lambda item: (item['bbox'][1], item['bbox'][0]))


def _lines(words: list[dict], median_height: float) -> list[dict]:
    bands: list[dict] = []
    for word in sorted(words, key=lambda item: ((item['bbox'][1] + item['bbox'][3]) / 2, item['bbox'][0])):
        x1, y1, x2, y2 = word['bbox']
        center = (y1 + y2) / 2
        matches = []
        for band in bands:
            overlap = max(0, min(y2, band['bottom']) - max(y1, band['top']))
            overlap_ratio = overlap / max(1, min(y2-y1, band['bottom']-band['top']))
            if overlap_ratio >= .25 or abs(center - band['center']) <= max(5, median_height * .48):
                matches.append((abs(center - band['center']), band))
        if matches:
            band = min(matches, key=lambda pair: pair[0])[1]
            band['words'].append(word)
            band['top'] = min(band['top'], y1)
            band['bottom'] = max(band['bottom'], y2)
            band['center'] = sum((item['bbox'][1] + item['bbox'][3]) / 2 for item in band['words']) / len(band['words'])
        else:
            bands.append({'top': y1, 'bottom': y2, 'center': center, 'words': [word]})

    fragments = []
    # Text on the same visual line is joined here; clear column gutters are
    # split later using the image's ink projection.
    max_gap = max(18, median_height * 1.25)
    for band in bands:
        current = []
        for word in sorted(band['words'], key=lambda item: item['bbox'][0]):
            if current and word['bbox'][0] - current[-1]['bbox'][2] > max_gap:
                fragments.append(_fragment(current))
                current = []
            current.append(word)
        if current:
            fragments.append(_fragment(current))
    return sorted(fragments, key=lambda item: (item['bbox'][1], item['bbox'][0]))


def _fragment(words: list[dict]) -> dict:
    return {'words': words, 'bbox': [min(w['bbox'][0] for w in words), min(w['bbox'][1] for w in words),
                                     max(w['bbox'][2] for w in words), max(w['bbox'][3] for w in words)]}


def _component_from_words(words: list[dict], median_height: float) -> dict:
    fragments = _lines(words, median_height)
    bands = []
    for fragment in fragments:
        center = (fragment['bbox'][1] + fragment['bbox'][3]) / 2
        band = next((item for item in bands
                     if abs(center - item['center']) <= max(5, median_height * .48)), None)
        if band is None:
            bands.append({'center': center, 'fragments': [fragment]})
        else:
            band['fragments'].append(fragment)
            counts = [len(part['words']) for part in band['fragments']]
            band['center'] = sum((part['bbox'][1] + part['bbox'][3]) / 2 * count
                                 for part, count in zip(band['fragments'], counts)) / sum(counts)
    text_lines = []
    for band in sorted(bands, key=lambda item: item['center']):
        parts = [' '.join(word['text'] for word in part['words'])
                 for part in sorted(band['fragments'], key=lambda item: item['bbox'][0])]
        text_lines.append(' '.join(parts))
    return {'words': words,
            'bbox': [min(w['bbox'][0] for w in words), min(w['bbox'][1] for w in words),
                     max(w['bbox'][2] for w in words), max(w['bbox'][3] for w in words)],
            'text': '\n'.join(text_lines),
            'confidence': sum(w['confidence'] for w in words) / len(words)}


def _components(fragments: list[dict], median_height: float) -> list[dict]:
    parent = list(range(len(fragments)))

    def find(index):
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left, right):
        left, right = find(left), find(right)
        if left != right:
            parent[right] = left

    max_vertical_gap = max(18, median_height * 1.55)
    max_start_delta = max(24, median_height * .95)
    for i, first in enumerate(fragments):
        ax1, ay1, ax2, ay2 = first['bbox']
        for j in range(i + 1, len(fragments)):
            second = fragments[j]
            bx1, by1, bx2, by2 = second['bbox']
            if by1 - ay2 > max_vertical_gap:
                break
            if abs((ay1 + ay2) / 2 - (by1 + by2) / 2) < median_height * .60:
                continue
            vertical_gap = max(0, max(ay1, by1) - min(ay2, by2))
            horizontal_overlap = max(0, min(ax2, bx2) - max(ax1, bx1))
            overlap_ratio = horizontal_overlap / max(1, min(ax2-ax1, bx2-bx1))
            aligned = abs(ax1 - bx1) <= max_start_delta
            if vertical_gap <= max_vertical_gap and (overlap_ratio >= .25 or aligned):
                union(i, j)

    groups = defaultdict(list)
    for index, fragment in enumerate(fragments):
        groups[find(index)].extend(fragment['words'])
    result = [_component_from_words(words, median_height) for words in groups.values()]
    return sorted(result, key=lambda item: (item['bbox'][1], item['bbox'][0]))


def _line_mask(image: Image.Image) -> np.ndarray:
    rgb = np.asarray(image.convert('RGB')).astype(np.int16)
    gray = np.asarray(image.convert('L'))
    spread = rgb.max(axis=2) - rgb.min(axis=2)
    colored_stroke = (spread > 65) & (rgb.max(axis=2) > 140) & (rgb.min(axis=2) < 180)
    return (gray < 105) | colored_stroke


def _split_components_at_gutters(components: list[dict], mask: np.ndarray,
                                 median_height: float, split_y: int) -> list[dict]:
    """Split OCR clusters bridged across a clear, ink-free column gutter."""
    result = []
    image_width = mask.shape[1]
    for component in components:
        pending = [component]
        while pending:
            current = pending.pop()
            x1, y1, x2, y2 = current['bbox']
            if y2 <= split_y or x2 - x1 < image_width * .30:
                result.append(current)
                continue
            counts = mask[y1:y2, x1:x2].sum(axis=0)
            low = counts <= 1
            runs, start = [], None
            for index, value in enumerate(list(low) + [False]):
                if value and start is None:
                    start = index
                elif not value and start is not None:
                    if index - start >= max(8, median_height * .55):
                        runs.append((start, index))
                    start = None
            split = None
            for left, right in sorted(runs, key=lambda pair: pair[1] - pair[0], reverse=True):
                if left < 4 or right > len(counts) - 4:
                    continue
                boundary = x1 + (left + right) // 2
                left_words = [word for word in current['words'] if (word['bbox'][0] + word['bbox'][2]) / 2 < boundary]
                right_words = [word for word in current['words'] if (word['bbox'][0] + word['bbox'][2]) / 2 >= boundary]
                if len(left_words) >= 2 and len(right_words) >= 2:
                    split = (left_words, right_words)
                    break
            if not split:
                result.append(current)
                continue
            for words in split:
                pending.append(_component_from_words(words, median_height))
    return sorted(result, key=lambda item: (item['bbox'][1], item['bbox'][0]))


def _merge_adjacent_components(components: list[dict], median_height: float,
                               split_y: int) -> list[dict]:
    """Rejoin fragments from one text region while keeping column gutters split."""
    parent = list(range(len(components)))

    def find(index):
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    for i, first in enumerate(components):
        ax1, ay1, ax2, ay2 = first['bbox']
        for j in range(i+1, len(components)):
            second = components[j]
            bx1, by1, bx2, by2 = second['bbox']
            if by1 - ay2 > max(20, median_height * 1.2):
                break
            if ay2 <= split_y or by2 <= split_y:
                continue
            vertical_overlap = max(0, min(ay2, by2)-max(ay1, by1))
            min_height = max(1, min(ay2-ay1, by2-by1))
            horizontal_gap = max(0, max(ax1, bx1)-min(ax2, bx2))
            horizontal_overlap = max(0, min(ax2,bx2)-max(ax1,bx1))
            min_width = max(1, min(ax2-ax1, bx2-bx1))
            if vertical_overlap / min_height >= .30 and (
                    horizontal_overlap / min_width >= .08 or horizontal_gap <= max(8, median_height*.35)):
                left, right = find(i), find(j)
                if left != right:
                    parent[right] = left
    groups = defaultdict(list)
    for index, component in enumerate(components):
        groups[find(index)].extend(component['words'])
    merged = [_component_from_words(words, median_height) for words in groups.values()]
    return sorted(merged, key=lambda item: (item['bbox'][1], item['bbox'][0]))


def _runs(values: np.ndarray, max_gap: int = 4) -> list[tuple[int, int]]:
    indices = np.flatnonzero(values)
    if not len(indices):
        return []
    result, start, previous = [], int(indices[0]), int(indices[0])
    for value in indices[1:]:
        value = int(value)
        if value - previous > max_gap + 1:
            result.append((start, previous))
            start = value
        previous = value
    result.append((start, previous))
    return result


def _merge_nearby_segments(segments: list[tuple[int, int, int]], tolerance: int = 3) -> list[tuple[int, int, int]]:
    grouped = []
    for position, start, end in sorted(segments):
        match = next((group for group in grouped if abs(group[0] - position) <= tolerance and
                      min(group[2], end) >= max(group[1], start) - tolerance), None)
        if match:
            match[0] = round((match[0] * match[3] + position) / (match[3] + 1))
            match[1] = min(match[1], start)
            match[2] = max(match[2], end)
            match[3] += 1
        else:
            grouped.append([position, start, end, 1])
    return [(int(p), int(s), int(e)) for p, s, e, _ in grouped]


def _colored_rectangles(image: Image.Image) -> list[list[int]]:
    """Find boxes with long, saturated-color outlines (common in infographics)."""
    rgb = np.asarray(image.convert('RGB')).astype(np.int16)
    spread = rgb.max(axis=2) - rgb.min(axis=2)
    mask = (spread > 80) & (rgb.max(axis=2) > 140) & (rgb.min(axis=2) < 100)
    height, width = mask.shape
    horizontal, vertical = [], []
    min_h, min_v = max(55, round(width * .04)), max(45, round(height * .035))
    for y, row in enumerate(mask):
        horizontal.extend((y, x1, x2) for x1, x2 in _runs(row) if x2 - x1 + 1 >= min_h)
    for x, column in enumerate(mask.T):
        vertical.extend((x, y1, y2) for y1, y2 in _runs(column) if y2 - y1 + 1 >= min_v)
    horizontal = _merge_nearby_segments(horizontal)
    vertical = _merge_nearby_segments(vertical)
    boxes = []
    for index, (x1, top1, bottom1) in enumerate(vertical):
        for x2, top2, bottom2 in vertical[index+1:]:
            if x2 - x1 < max(80, width * .08):
                continue
            if abs(top1-top2) > 7 or abs(bottom1-bottom2) > 9:
                continue
            top, bottom = round((top1+top2)/2), round((bottom1+bottom2)/2)
            width_px = x2 - x1
            bottom_coverage = mask[max(0,bottom-2):min(height,bottom+3), x1:x2+1].any(axis=0).mean()
            top_coverage = mask[max(0,top-2):min(height,top+3), x1:x2+1].any(axis=0).mean()
            if bottom - top < 30 or bottom_coverage < .70 or top_coverage < .18:
                continue
            if not any(abs(box[0]-x1) < 5 and abs(box[1]-top) < 8 and abs(box[2]-x2) < 5 for box in boxes):
                boxes.append([x1, top, x2, bottom])
    return sorted(boxes, key=lambda box: (box[1], box[0]))


def _run_ocr(image: Image.Image, *, executable: str, language: str, tessdata: str | None,
             psm: int = 11) -> str:
    payload = io.BytesIO()
    image.save(payload, format='PNG')
    command = [str(executable), 'stdin', 'stdout', '-l', language, '--psm', str(psm)]
    if tessdata:
        command += ['--tessdata-dir', str(tessdata)]
    result = subprocess.run(command + ['-c', 'tessedit_create_tsv=1'], input=payload.getvalue(),
                            capture_output=True, timeout=180, check=True)
    return result.stdout.decode('utf-8-sig')


def _run_plain_ocr(image: Image.Image, *, executable: str, language: str, tessdata: str | None,
                   psm: int) -> str:
    payload = io.BytesIO()
    image.save(payload, format='PNG')
    command = [str(executable), 'stdin', 'stdout', '-l', language, '--psm', str(psm)]
    if tessdata:
        command += ['--tessdata-dir', str(tessdata)]
    result = subprocess.run(command, input=payload.getvalue(), capture_output=True,
                            timeout=180, check=True)
    return result.stdout.decode('utf-8-sig')


def _words_text_by_line(words: list[dict], min_confidence: float = 20) -> tuple[str, list[dict]]:
    groups = defaultdict(list)
    usable = []
    for word in words:
        text = word['text']
        if word['confidence'] < min_confidence or not any(
                character.isalnum() or '\uac00' <= character <= '\ud7a3' for character in text):
            continue
        usable.append(word)
        groups[word.get('line')].append(word)
    lines = []
    for group in groups.values():
        lines.append(' '.join(item['text'] for item in sorted(group, key=lambda item: item['bbox'][0])))
    return '\n'.join(lines), usable


def _refine_node_ocr(image: Image.Image, node: dict, *, executable: str,
                     language: str, tessdata: str | None) -> dict:
    x1, y1, x2, y2 = node['bbox']
    padding = max(6, round(min(x2-x1, y2-y1) * .025))
    crop = image.crop((max(0,x1-padding), max(0,y1-padding),
                       min(image.width,x2+padding), min(image.height,y2+padding)))
    enlarged = crop.resize((crop.width*2, crop.height*2))
    words = _read_words(_run_ocr(enlarged, executable=executable, language=language,
                                 tessdata=tessdata, psm=6))
    text, usable = _words_text_by_line(words)
    if text:
        return {**node, 'text': text,
                'confidence': round(sum(item['confidence'] for item in usable)/len(usable), 3)}
    return node


def _crop_text_nodes(image: Image.Image, boxes: list[list[int]], *, executable: str,
                     language: str, tessdata: str | None) -> tuple[list[dict], list[dict]]:
    nodes, unassigned = [], []
    for box in boxes:
        x1, y1, x2, y2 = box
        pad_top = round((y2-y1) * .72)
        crop_box = (max(0, x1-20), max(0, y1-pad_top), min(image.width, x2+15), min(image.height, y2+8))
        crop = image.crop(crop_box)
        enlarged = crop.resize((crop.width*2, crop.height*2))
        words = _read_words(_run_ocr(enlarged, executable=executable, language=language,
                                     tessdata=tessdata, psm=6))
        text, usable = _words_text_by_line(words)
        for word in words:
            if word not in usable:
                if any(character.isalnum() or '\uac00' <= character <= '\ud7a3' for character in word['text']):
                    bx1, by1, bx2, by2 = word['bbox']
                    unassigned.append({'text': word['text'],
                                       'bbox': [crop_box[0]+bx1//2, crop_box[1]+by1//2,
                                                crop_box[0]+(bx2+1)//2, crop_box[1]+(by2+1)//2],
                                       'confidence': round(word['confidence'], 3),
                                       'reason': 'LOW_CONFIDENCE_CROP_OCR'})
        if usable:
            nodes.append({'text': text, 'bbox': box,
                          'confidence': round(sum(word['confidence'] for word in usable)/len(usable), 3)})
    return nodes, unassigned


def _title_from_existing_paragraphs(paragraphs: list[dict], image_height: int) -> str | None:
    candidates = []
    for block in paragraphs:
        bbox = block.get('bbox')
        text = str(block.get('text') or '').strip()
        try:
            x1, y1, x2, y2 = bbox
            confidence = float(block.get('confidence') or 0)
        except (TypeError, ValueError):
            continue
        if not text or y2 > image_height * .22 or confidence < 65:
            continue
        candidates.append({'bbox': [x1, y1, x2, y2], 'text': text.replace('|', '').strip(), 'confidence': confidence})
    selected = []
    for candidate in sorted(candidates, key=lambda item: item['confidence'], reverse=True):
        x1, y1, x2, y2 = candidate['bbox']
        duplicate = False
        for prior in selected:
            a1, b1, a2, b2 = prior['bbox']
            overlap = max(0, min(x2,a2)-max(x1,a1)) * max(0, min(y2,b2)-max(y1,b1))
            area = min(max(1,(x2-x1)*(y2-y1)), max(1,(a2-a1)*(b2-b1)))
            if overlap / area >= .45:
                duplicate = True
                break
        if not duplicate:
            selected.append(candidate)
    selected.sort(key=lambda item: (item['bbox'][1], item['bbox'][0]))
    return ' '.join(item['text'] for item in selected) or None


def _nearest_boundary_points(a: list[int], b: list[int]) -> tuple[tuple[int, int], tuple[int, int]] | None:
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    if ax2 <= bx1:
        x1, x2 = ax2, bx1
        y = int((max(ay1, by1) + min(ay2, by2)) / 2) if max(ay1, by1) < min(ay2, by2) else None
        if y is not None:
            return (x1, y), (x2, y)
    if bx2 <= ax1:
        x1, x2 = ax1, bx2
        y = int((max(ay1, by1) + min(ay2, by2)) / 2) if max(ay1, by1) < min(ay2, by2) else None
        if y is not None:
            return (x1, y), (x2, y)
    if ay2 <= by1:
        y1, y2 = ay2, by1
        x = int((max(ax1, bx1) + min(ax2, bx2)) / 2) if max(ax1, bx1) < min(ax2, bx2) else None
        if x is not None:
            return (x, y1), (x, y2)
    if by2 <= ay1:
        y1, y2 = ay1, by2
        x = int((max(ax1, bx1) + min(ax2, bx2)) / 2) if max(ax1, bx1) < min(ax2, bx2) else None
        if x is not None:
            return (x, y1), (x, y2)
    # Diagonal connectors: use nearest corners only when neither axis overlaps.
    if ax2 < bx1 and ay2 < by1:
        return (ax2, ay2), (bx1, by1)
    if bx2 < ax1 and ay2 < by1:
        return (ax1, ay2), (bx2, by1)
    if ax2 < bx1 and by2 < ay1:
        return (ax2, ay1), (bx1, by2)
    if bx2 < ax1 and by2 < ay1:
        return (ax1, ay1), (bx2, by2)
    return None


def _trace(mask: np.ndarray, start: tuple[int, int], end: tuple[int, int]) -> tuple[float, bool, bool]:
    x1, y1 = start
    x2, y2 = end
    length = max(abs(x2-x1), abs(y2-y1)) + 1
    if length < 10:
        return 0.0, False, False
    xs = np.rint(np.linspace(x1, x2, length)).astype(int)
    ys = np.rint(np.linspace(y1, y2, length)).astype(int)
    hits = []
    height, width = mask.shape
    for x, y in zip(xs, ys):
        hits.append(bool(mask[max(0,y-1):min(height,y+2), max(0,x-1):min(width,x+2)].any()))
    endpoint_start = any(hits[:min(4, length)])
    endpoint_end = any(hits[-min(4, length):])
    return sum(hits) / len(hits), endpoint_start, endpoint_end


def _arrowhead(mask: np.ndarray, tip: tuple[int, int], toward_source: tuple[float, float]) -> bool:
    x, y = tip
    ux, uy = toward_source
    norm = math.hypot(ux, uy) or 1
    ux, uy = ux / norm, uy / norm
    px, py = -uy, ux
    height, width = mask.shape
    arms = []
    for sign in (-1, 1):
        found = False
        for distance in (5, 8, 11, 14):
            sx = round(x + ux * distance + sign * px * distance * .55)
            sy = round(y + uy * distance + sign * py * distance * .55)
            if 0 <= sx < width and 0 <= sy < height and mask[max(0, sy-2):min(height, sy+3), max(0, sx-2):min(width, sx+3)].any():
                found = True
                break
        arms.append(found)
    return all(arms)


def _edges(nodes: list[dict], mask: np.ndarray) -> tuple[list[dict], list[dict]]:
    confirmed, unverified = [], []
    for i, first in enumerate(nodes):
        for second in nodes[i+1:]:
            points = _nearest_boundary_points(first['bbox'], second['bbox'])
            if not points:
                continue
            start, end = points
            coverage, touches_start, touches_end = _trace(mask, start, end)
            if coverage >= .88 and touches_start and touches_end:
                direction = 'undirected'
                if _arrowhead(mask, end, (start[0]-end[0], start[1]-end[1])):
                    direction = 'from_to'
                elif _arrowhead(mask, start, (end[0]-start[0], end[1]-start[1])):
                    direction = 'to_from'
                confirmed.append({'from': first['id'], 'to': second['id'],
                                  'direction': direction, 'confidence': round(coverage, 3)})
            elif coverage >= .45 and (touches_start or touches_end):
                unverified.append({'from': first['id'] if touches_start else None,
                                   'to': second['id'] if touches_end else None,
                                   'direction': None, 'confidence': round(coverage, 3),
                                   'bbox': [min(start[0], end[0]), min(start[1], end[1]),
                                            max(start[0], end[0]), max(start[1], end[1])],
                                   'reason': 'CONNECTOR_NOT_VERIFIED_AT_BOTH_NODE_BOUNDARIES'})
    return confirmed, unverified


def extract_diagram_image(path, *, executable, language='kor+eng', tessdata=None,
                          existing_paragraphs: list[dict] | None = None):
    """OCR sparse diagram labels and only accept visibly continuous node-to-node strokes."""
    require_ocr_languages(executable, language, tessdata)
    with Image.open(path) as original:
        image = ImageOps.exif_transpose(original).convert('RGB')
    tsv = _run_ocr(image, executable=executable, language=language, tessdata=tessdata, psm=11)
    words = _deduplicate_words(_read_words(tsv))
    median_height = float(np.median([word['bbox'][3] - word['bbox'][1] for word in words])) if words else 12.0
    fragments = _lines(words, median_height)
    mask = _line_mask(image)
    split_y = round(image.height * .22)
    components = _split_components_at_gutters(_components(fragments, median_height), mask,
                                              median_height, split_y)
    components = _merge_adjacent_components(components, median_height, split_y)
    title_components = [item for item in components if item['bbox'][3] <= image.height * .22]
    title_crop = image.crop((0, 0, image.width, max(1, round(image.height * .18))))
    title_text = _run_plain_ocr(title_crop.resize((title_crop.width*2, title_crop.height*2)),
                                executable=executable, language=language,
                                tessdata=tessdata, psm=6)
    title = (' '.join(part.strip() for part in title_text.splitlines() if part.strip()) or
             ' '.join(item['text'].replace('\n', ' ') for item in title_components) or None)
    existing_title = _title_from_existing_paragraphs(existing_paragraphs or [], image.height)
    if existing_title and (not title or len(''.join(existing_title.split())) > len(''.join(title.split()))):
        title = existing_title
    card_boxes = _colored_rectangles(image)
    card_nodes, card_unassigned = _crop_text_nodes(image, card_boxes, executable=executable,
                                                   language=language, tessdata=tessdata)
    remaining = [item for item in components if item['bbox'][3] > image.height * .22]
    nodes, unassigned = [], list(card_unassigned)
    for component in remaining:
        bx1, by1, bx2, by2 = component['bbox']
        if any(bx1 < box[2]+18 and bx2 > box[0]-22 and
               by1 < box[3]+10 and by2 > box[1]-(box[3]-box[1])*.78 for box in card_boxes):
            continue
        text = component['text'].strip()
        if len(text) < 2 or component['confidence'] < 25:
            unassigned.append({'text': text, 'bbox': component['bbox'],
                               'confidence': round(component['confidence'], 3),
                               'reason': 'LOW_CONFIDENCE_OR_FRAGMENTARY_TEXT'})
            continue
        node = {'text': text, 'bbox': component['bbox'],
                'confidence': round(component['confidence'], 3)}
        nodes.append(_refine_node_ocr(image, node, executable=executable,
                                      language=language, tessdata=tessdata))
    nodes.extend(card_nodes)
    nodes = sorted(nodes, key=lambda item: (item['bbox'][1], item['bbox'][0]))
    for index, node in enumerate(nodes, start=1):
        node['id'] = f'n{index}'
    edges, unverified = _edges(nodes, mask)
    warnings = []
    if unassigned:
        warnings.append('TEXT_BLOCKS_UNASSIGNED')
    if unverified:
        warnings.append('EDGE_RELATION_UNVERIFIED')
    return {'nodes': nodes, 'edges': edges, 'unverified_edges': unverified,
            'unassigned_text_blocks': unassigned, 'diagram_title': title,
            'warnings': warnings, 'width': image.width, 'height': image.height}
