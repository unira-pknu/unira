import base64
import hashlib
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image, ImageDraw
from bs4 import BeautifulSoup

from scripts.crawlers.common.body_images import collect_body_images, save_body_images, image_info
from scripts.preprocessing.extractors.image_ocr import detect_grid, layout_blocks


def png():
    output = BytesIO()
    Image.new('RGB', (40, 50), 'white').save(output, format='PNG')
    return output.getvalue()


class BodyImageTests(unittest.TestCase):
    def test_svg_original_validation(self):
        self.assertEqual(image_info(b'<svg xmlns="http://www.w3.org/2000/svg" width="40" height="50"/>')[:2], ('svg', 'image/svg+xml'))
        with self.assertRaises(ValueError):
            image_info(b'<?xml version="1.0"?><!DOCTYPE svg [<!ENTITY x "test">]><svg/>')

    def test_body_scope_decorations_and_lazy_images(self):
        soup = BeautifulSoup('''<header><img src="outside.png"></header><div class="bdvEdit">
        <nav><img src="menu.png"></nav><div class="share"><img src="x.png"></div>
        <img src="/images/logo_school.png"><img src="icon.gif"><img src="spacer.gif">
        <img width="1" height="1" src="pixel.gif"><img src="poster.png" alt="공지">
        <img src="placeholder.gif" data-src="real.jpg"></div><footer><img src="foot.png"></footer>''', 'lxml')
        records = collect_body_images(soup)
        self.assertEqual([r['src'] for r in records], ['poster.png', 'real.jpg'])
        self.assertEqual([r['order'] for r in records], [1, 2])

    def test_no_body_does_not_collect_layout(self):
        self.assertEqual(collect_body_images(BeautifulSoup('<body><img src="a.png"></body>', 'lxml')), [])

    def test_inline_and_remote_share_original_hash(self):
        payload = png()
        data = 'data:image/png;base64,' + base64.b64encode(payload).decode()
        response = SimpleNamespace(url='https://example.org/image.png', raise_for_status=lambda:None,
                                   iter_content=lambda size:iter([payload]), close=lambda:None)
        with tempfile.TemporaryDirectory() as temp, patch('scripts.crawlers.common.body_images.get_with_tls_policy', return_value=(response, False)) as fetch:
            root = Path(temp)
            records = save_body_images([{'order':1,'src':data,'alt':''}, {'order':2,'src':'image.png','alt':''},
                                       {'order':3,'src':'image.png','alt':''}, {'order':4,'src':'data:image/png;base64,bad!','alt':''}],
                session=object(),page_url='https://example.org/page',output_dir=root/'images',project_root=root)
            self.assertEqual([r['status'] for r in records], ['saved','saved','saved','failed'])
            self.assertEqual(records[0]['sha256'], hashlib.sha256(payload).hexdigest())
            self.assertEqual(records[0]['saved_path'], records[1]['saved_path'])
            self.assertEqual((root/records[0]['saved_path']).read_bytes(),payload)
            self.assertEqual(len(list((root/'images').iterdir())),1)
            self.assertEqual(fetch.call_count,1)
            self.assertIsNone(records[0]['source_url'])
            self.assertEqual(records[1]['source_url'],'https://example.org/image.png')

    def test_html_response_rejected_and_no_download_honored(self):
        response=SimpleNamespace(url='https://example.org/a.png', raise_for_status=lambda:None,
            iter_content=lambda size:iter([b'<html>login</html>']), close=lambda:None)
        with tempfile.TemporaryDirectory() as temp, patch('scripts.crawlers.common.body_images.get_with_tls_policy', return_value=(response,False)) as fetch:
            args=dict(session=object(),page_url='https://example.org',output_dir=Path(temp)/'images',project_root=Path(temp))
            items=[{'order':1,'src':'/a.png','alt':''}]
            records=save_body_images(items,**args,download=False)
            fetch.assert_not_called()
            self.assertEqual(records[0]['status'],'not_attempted')
            self.assertEqual(save_body_images(items,**args)[0]['status'],'failed')
            self.assertFalse((Path(temp)/'images').exists())


class ImageOCRTests(unittest.TestCase):
    def test_grid_and_paragraph_separation(self):
        image = Image.new('RGB', (200, 160), 'white')
        draw = ImageDraw.Draw(image)
        for y in (50, 100, 150):
            draw.line((10, y, 190, y), fill='black', width=2)
        for x in (10, 100, 190):
            draw.line((x, 50, x, 150), fill='black', width=2)
        grid = detect_grid(image)
        self.assertIsNotNone(grid)
        header = 'level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n'
        tsv = header + '5\t1\t1\t1\t1\t1\t20\t10\t20\t10\t90\t본문\n'
        tsv += '5\t1\t2\t1\t1\t1\t20\t60\t20\t10\t85\t셀\n'
        blocks = layout_blocks(tsv, grid)
        self.assertEqual(blocks[0]['type'], 'ocr_paragraph')
        self.assertEqual(blocks[1]['rows'][0][0]['text'], '셀')
        self.assertEqual(blocks[1]['rows'][1][1]['text'], '')

    def test_blank_not_table(self):
        self.assertIsNone(detect_grid(Image.new('RGB', (200, 200), 'white')))

    def test_colspan_is_preserved(self):
        image = Image.new('RGB', (200, 140), 'white')
        draw = ImageDraw.Draw(image)
        for y in (10, 70, 130):
            draw.line((10, y, 190, y), fill='black', width=2)
        draw.line((10, 10, 10, 130), fill='black', width=2)
        draw.line((190, 10, 190, 130), fill='black', width=2)
        draw.line((100, 70, 100, 130), fill='black', width=2)
        grid = detect_grid(image)
        self.assertEqual(grid['cells'][0]['colspan'], 2)
        self.assertEqual(grid['cells'][1]['row'], 1)
        header = 'level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n'
        tsv = header + '5\t1\t1\t1\t1\t1\t20\t25\t20\t10\t90\t제목\n'
        tsv += '5\t1\t2\t1\t1\t1\t20\t85\t20\t10\t85\t왼쪽\n'
        tsv += '5\t1\t2\t1\t1\t2\t120\t85\t20\t10\t85\t오른쪽\n'
        table = next(block for block in layout_blocks(tsv, grid) if block['type'] == 'ocr_table')
        self.assertEqual(table['rows'][0][0]['colspan'], 2)
        self.assertEqual(table['rows'][0][0]['text'], '제목')
        self.assertEqual(table['rows'][1][1]['text'], '오른쪽')

    def test_empty_ocr(self):
        self.assertEqual(layout_blocks('level\ttext\n'), [])

    def test_rejects_non_tsv_result(self):
        with self.assertRaisesRegex(ValueError, 'TSV'):
            layout_blocks('plain OCR text')


if __name__ == "__main__":
    unittest.main()
