"""Collect body images in DOM order and store original bytes by SHA-256."""
from __future__ import annotations

import base64
import hashlib
from io import BytesIO
from pathlib import Path
import re
from xml.etree import ElementTree

from bs4 import BeautifulSoup
from PIL import Image

from scripts.crawlers.departments.urls import resolve_url
from scripts.crawlers.departments.tls import get_with_tls_policy

MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_IMAGES = 100
EXCLUDED = 'nav, header, footer, script, .logo, .menu, .share, .sns, .breadcrumb, .btn-wrap, .c_bdvNav, .c_bdvBtn, [role=navigation]'
DECORATION = re.compile(r'(?:^|[\s_./-])(?:logo|icon|ico|btn|button|share|sns|spacer|tracking)(?:[\s_./-]|$)', re.I)
ROOTS = ('.bdvEdit', '.bdvTxt', '.editor-data-box', '#sbCont', '.boardRead .contents', '.sub-content', 'main')


def image_info(payload):
    if payload.lstrip(b'\xef\xbb\xbf\t\r\n ').startswith((b'<svg', b'<?xml')):
        if b'<!DOCTYPE' in payload.upper() or b'<!ENTITY' in payload.upper():
            raise ValueError('SVG entity declarations are not supported')
        root = ElementTree.fromstring(payload)
        if root.tag not in {'svg', '{http://www.w3.org/2000/svg}svg'}:
            raise ValueError('XML response is not an SVG image')
        return 'svg', 'image/svg+xml', root.get('width'), root.get('height')
    with Image.open(BytesIO(payload)) as image:
        format_name = image.format
        width, height = image.size
        image.verify()
    extension = {'PNG': 'png', 'JPEG': 'jpg', 'GIF': 'gif', 'WEBP': 'webp', 'BMP': 'bmp', 'TIFF': 'tiff'}.get(format_name)
    if not extension:
        raise ValueError(f'unsupported image format: {format_name}')
    return extension, Image.MIME.get(format_name), width, height


def select_body_root(soup: BeautifulSoup):
    return next((node for selector in ROOTS if (node := soup.select_one(selector)) is not None), None)


def collect_body_images(soup: BeautifulSoup, *, root=None) -> list[dict]:
    root = root if root is not None else select_body_root(soup)
    if root is None:
        return []  # Never collect the entire page chrome as a fallback.
    clone = BeautifulSoup(str(root), 'lxml')
    for node in list(clone.select(EXCLUDED)):
        if node.parent is not None:
            node.decompose()
    images = []
    for img in clone.select('img'):
        src = str(img.get('data-src') or img.get('data-original') or img.get('src') or '').strip()
        if not src:
            continue
        label = ' '.join([str(img.get('id', '')), ' '.join(img.get('class', [])), str(img.get('alt', ''))])
        path = '' if src.lower().startswith('data:') else src.split('?', 1)[0]
        if DECORATION.search(label + ' ' + path) or any(token in label for token in ('로고', '공유 버튼', '메뉴 아이콘')):
            continue
        try:
            if 0 < int(img.get('width', 0)) <= 32 and 0 < int(img.get('height', 0)) <= 32:
                continue
        except ValueError:
            pass
        record = {'order': len(images) + 1, 'src': src, 'alt': str(img.get('alt') or '')}
        # Curriculum diagrams often link a small preview to the original image.
        anchor = img.find_parent('a', href=True)
        original = str(anchor.get('href') or '').strip() if anchor else ''
        if (re.search(r'\.(?:png|jpe?g|gif|webp|bmp|tiff?)(?:[?#]|$)', original, re.I)
                and not re.match(r'(?i)^(?:javascript|data|file):', original)):
            if original != src:
                record.update(src=original, display_src=src)
        images.append(record)
    return images


def save_body_images(images, *, session, page_url: str, output_dir: Path, project_root: Path,
                     download: bool = True, system_trust_fallback: bool = False) -> list[dict]:
    results = []
    cache = {}
    for item in images:
        src = item['src']
        inline = src.lower().startswith('data:')
        record = {'order': item['order'], 'alt': item['alt'], 'source_kind': 'data_uri' if inline else 'url',
                  'source_url': None, 'source_page_url': page_url, 'saved_path': None, 'sha256': None,
                  'size_bytes': None, 'status': 'not_attempted', 'error': None}
        response = None
        try:
            if not inline:
                record['source_url'] = resolve_url(src, page_url)
            if item.get('display_src') and not item['display_src'].lower().startswith('data:'):
                record['display_url'] = resolve_url(item['display_src'], page_url)
            if not download:
                results.append(record)
                continue
            if len(results) >= MAX_IMAGES:
                raise ValueError('image count limit exceeded')
            key = hashlib.sha256(src.encode()).hexdigest() if inline else record['source_url']
            if key in cache:
                record.update(cache[key])
                results.append(record)
                continue
            if inline:
                header, encoded = src.split(',', 1)
                if not re.fullmatch(r'data:image/[a-z0-9.+-]+;base64', header, re.I):
                    raise ValueError('unsupported image data URI')
                if len(encoded) > MAX_IMAGE_BYTES * 4 // 3 + 1024:
                    raise ValueError('image exceeds byte limit')
                payload = base64.b64decode(re.sub(r'\s+', '', encoded), validate=True)
            else:
                response, _ = get_with_tls_policy(session, record['source_url'], timeout=20, stream=True,
                    headers={'Referer': page_url}, system_trust_fallback=system_trust_fallback)
                record['final_url'] = response.url
                response.raise_for_status()
                payload_buffer = bytearray()
                for chunk in response.iter_content(8192):
                    if len(payload_buffer) + len(chunk) > MAX_IMAGE_BYTES:
                        raise ValueError('image exceeds byte limit')
                    payload_buffer.extend(chunk)
                payload = bytes(payload_buffer)
            if len(payload) > MAX_IMAGE_BYTES:
                raise ValueError('image exceeds byte limit')
            extension, content_type, width, height = image_info(payload)
            digest = hashlib.sha256(payload).hexdigest()
            output_dir.mkdir(parents=True, exist_ok=True)
            path = output_dir / f'{digest}.{extension}'
            if not path.exists() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                temp = path.with_suffix(path.suffix + '.part')
                temp.write_bytes(payload)
                temp.replace(path)
            stored = {'saved_path': path.resolve().relative_to(project_root.resolve()).as_posix(),
                      'sha256': digest, 'size_bytes': len(payload), 'width': width, 'height': height,
                      'content_type': content_type, 'status': 'saved'}
            record.update(stored)
            cache[key] = stored
        except Exception as exc:
            record.update(status='failed', error={'code': 'BODY_IMAGE_FAILED', 'message': str(exc)})
        finally:
            if response is not None:
                response.close()
        results.append(record)
    return results
