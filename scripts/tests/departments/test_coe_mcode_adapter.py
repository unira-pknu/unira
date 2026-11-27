from scripts.tests._paths import FIXTURES_ROOT

from pathlib import Path
import unittest

from bs4 import BeautifulSoup

from scripts.crawlers.departments.adapters import get_adapter
from scripts.crawlers.departments.discovery import discover_from_html
from scripts.crawlers.departments.probe import analyze_site_html, mcode_menu_links


FIXTURES = FIXTURES_ROOT / "departments" / "coe"
BASE_URL = "https://coe.pknu.ac.kr/"


class QueryMcodeAdapterTests(unittest.TestCase):
    def test_static_diagram_under_heading_is_not_complete_text_content(self):
        soup = BeautifulSoup('<div id="container-wrap">메뉴<div class="page-cont">'
                             '<h2>교육과정</h2><img data-original="/diagram.png"></div></div>', 'lxml')
        parsed = get_adapter('query_mcode').parse_static(soup, fallback_title='교육과정')
        self.assertEqual('image_only', parsed['content_state'])
        self.assertEqual(['/diagram.png'], parsed['content_images'])
        self.assertNotIn('메뉴', parsed['content'])

    @classmethod
    def setUpClass(cls) -> None:
        cls.adapter = get_adapter("query_mcode")
        cls.home = (FIXTURES / "homepage.html").read_text(encoding="utf-8")
        cls.static = (FIXTURES / "static.html").read_text(encoding="utf-8")
        cls.board = (FIXTURES / "board.html").read_text(encoding="utf-8")
        cls.detail = (FIXTURES / "detail.html").read_text(encoding="utf-8")

    def test_probe_and_menu_discovery_are_pattern_based_and_deduplicated(self) -> None:
        links = mcode_menu_links(self.home, BASE_URL)
        self.assertGreaterEqual(len(links), 3)
        menus = self.adapter.discover_menus(self.home, BASE_URL)
        self.assertEqual(["0401010000", "0406010000", "0406020000"], [m["mcode"] for m in menus])
        probe = analyze_site_html(site_key="fixture", requested_url=BASE_URL, html=self.home)
        self.assertTrue(probe.compatible)
        self.assertEqual(4, probe.fingerprints["mcode_menu_links"])

    def test_discovery_distinguishes_static_and_board(self) -> None:
        menus = self.adapter.discover_menus(self.home, BASE_URL)
        html = {m["url"]: self.board if m["mcode"].startswith("0406") else self.static for m in menus}
        result = discover_from_html(site_key="fixture", base_url=BASE_URL, homepage_html=self.home,
                                    section_html=html, adapter_name="query_mcode")
        by_id = {s.id: s for s in result.sections}
        self.assertEqual("static_page", by_id["mcode_0401010000"].kind)
        self.assertEqual("board", by_id["mcode_0406010000"].kind)
        self.assertIsNone(by_id["mcode_0406010000"].bbs_id)

    def test_list_pagination_and_stable_ids(self) -> None:
        url = BASE_URL + "?mcode=0406010000"
        request_url, params = self.adapter.list_request(url, 2)
        self.assertEqual("https://coe.pknu.ac.kr/index.Pknu", request_url)
        self.assertEqual({"menucode": "0406010000", "mode": "1", "page": 2}, params)
        items = self.adapter.parse_list(BeautifulSoup(self.board, "lxml"), url)
        self.assertEqual(["691", "690"], [item["source_id"] for item in items])
        self.assertTrue(items[0]["is_notice"])
        self.assertIn("no=691", items[0]["post_url"])

    def test_detail_body_metadata_and_attachment(self) -> None:
        url = BASE_URL + "?menucode=0406010000&mode=2&no=691&page=1"
        parsed = self.adapter.parse_detail(BeautifulSoup(self.detail, "lxml"), url,
                                           {"source_id": "691", "date": ""},
                                           base_url=BASE_URL, site_prefix="coe")
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual("691", parsed["source_id"])
        self.assertEqual("공과대학", parsed["author"])
        self.assertEqual("2026-09-10", parsed["date"])
        self.assertEqual("수집할 상세 본문입니다.", parsed["body"])
        self.assertEqual("https://coe.pknu.ac.kr/_Inc/download.asp?gb=brd&f_idx=206",
                         parsed["attachments"][0]["url"])


if __name__ == "__main__":
    unittest.main()
