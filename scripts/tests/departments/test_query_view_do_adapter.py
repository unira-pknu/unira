from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bs4 import BeautifulSoup

from scripts.crawlers.common.schema import apply_common_schema, attachment_error, build_attachment, validate_common_document, normalize_attachment
from scripts.crawlers.departments import engine
from scripts.crawlers.departments.adapters import get_adapter
from scripts.crawlers.departments.config import get_department
from scripts.crawlers.departments.discovery import discover_from_html
from scripts.crawlers.departments.probe import analyze_site_html
from scripts.tests._paths import FIXTURES_ROOT, PROJECT_ROOT


ROOT = FIXTURES_ROOT / "departments" / "fishsci"


BASE_URL = "https://fishsci.pknu.ac.kr/"


class QueryViewDoAdapterTests(unittest.TestCase):
    def test_static_diagram_uses_subcontent_instead_of_outer_navigation(self):
        soup = BeautifulSoup('<main>전체 메뉴<div class="sub-content"><h2>교육과정</h2>'
                             '<img src="/diagram.png"></div></main>', 'lxml')
        parsed = get_adapter('query_view_do').parse_static(soup, fallback_title='교육과정')
        self.assertEqual('image_only', parsed['content_state'])
        self.assertNotIn('전체 메뉴', parsed['content'])

    @classmethod
    def setUpClass(cls) -> None:
        cls.adapter = get_adapter("query_view_do")
        cls.homepage = (ROOT / "homepage.html").read_text(encoding="utf-8")
        cls.board = (ROOT / "board.html").read_text(encoding="utf-8")

    def test_menu_discovery_uses_no_and_excludes_external_home_and_details(self) -> None:
        html = self.homepage.replace(
            "</nav>", '<a href="/kor/view.do?no=44&pgMode=View&idx=1262">상세</a>'
            '<a href="/kor/view.do?no=sitemap">SITEMAP</a>'
            '<a href="/kor/login.do">로그인</a></nav>',
        )
        menus = self.adapter.discover_menus(html, BASE_URL)
        self.assertEqual(["2", "44", "45", "46"], [menu["menu_no"] for menu in menus])
        self.assertTrue(all("pgMode" not in menu["url"] for menu in menus))

    def test_section_id_comes_from_menu_no(self) -> None:
        result = discover_from_html(
            site_key="fishsci.pknu.ac.kr", base_url=BASE_URL,
            homepage_html=self.homepage,
            section_html={"https://fishsci.pknu.ac.kr/kor/view.do?no=44": self.board},
            adapter_name="query_view_do",
        )
        notice = next(section for section in result.sections if section.id == "menu_44")
        self.assertEqual("board", notice.kind)
        self.assertIsNone(notice.bbs_id)
        self.assertEqual("/kor/view.do?no=44", notice.path)

    def test_list_uses_idx_as_source_id(self) -> None:
        items = self.adapter.parse_list(BeautifulSoup(self.board, "lxml"), BASE_URL + "kor/view.do?no=44")
        self.assertEqual(["1262", "1257"], [item["source_id"] for item in items])
        self.assertIn("idx=1262", items[0]["post_url"])

    def test_detail_urls_do_not_change_with_list_page_index(self) -> None:
        for name, fixture, base in (
            ("query_view_do", ROOT / "board.html", BASE_URL + "kor/view.do?no=44"),
            ("query_view_legacy", FIXTURES_ROOT / "departments/mpsm/board.html",
             "https://mpsm.pknu.ac.kr/view.do?no=303"),
        ):
            with self.subTest(adapter=name):
                adapter = get_adapter(name)
                soup = BeautifulSoup(fixture.read_text(encoding="utf-8"), "lxml")
                first = adapter.parse_list(soup, base + "&pageIndex=1")
                later = adapter.parse_list(soup, base + "&pageIndex=1200")
                self.assertEqual([item["source_id"] for item in first], [item["source_id"] for item in later])
                self.assertEqual([item["post_url"] for item in first], [item["post_url"] for item in later])
                self.assertTrue(all("pageIndex=" not in item["post_url"] for item in later))
                self.assertTrue(all("no=" in item["post_url"] and "idx=" in item["post_url"] for item in later))

    def test_numeric_adapter_remains_separate(self) -> None:
        self.assertEqual("numeric_cms", get_adapter("numeric_cms").name)
        self.assertEqual("query_view_do", self.adapter.name)

    def test_detail_extracts_canonical_fields_and_absolute_attachment(self) -> None:
        html = (ROOT / "detail_with_attachment.html").read_text(encoding="utf-8")
        url = BASE_URL + "kor/view.do?no=44&pgMode=View&idx=1262"
        parsed = self.adapter.parse_detail(
            BeautifulSoup(html, "lxml"), url, {"source_id": "1262", "is_notice": True},
            base_url=BASE_URL, site_prefix="kor",
        )
        self.assertEqual("fixture 첨부 공지", parsed["title"])
        self.assertEqual("관리자", parsed["author"])
        self.assertEqual("2026-07-23", parsed["date"])
        self.assertEqual("1262", parsed["source_id"])
        self.assertEqual("첨부파일이 있는 공지사항의 정제된 본문입니다.", parsed["body"])
        self.assertEqual("https://fishsci.pknu.ac.kr/boardDown.do?fKey=878", parsed["attachments"][0]["url"])

    def test_empty_body_removes_repeated_ui_without_failing(self) -> None:
        html = (ROOT / "empty_detail.html").read_text(encoding="utf-8")
        parsed = self.adapter.parse_detail(
            BeautifulSoup(html, "lxml"), BASE_URL + "kor/view.do?no=44&pgMode=View&idx=1300",
            {"source_id": "1300"}, base_url=BASE_URL, site_prefix="kor",
        )
        self.assertIsNotNone(parsed)
        self.assertEqual("", parsed["body"])

    def test_attachment_failure_remains_a_valid_document(self) -> None:
        html = (ROOT / "detail_with_attachment.html").read_text(encoding="utf-8")
        url = BASE_URL + "kor/view.do?no=44&pgMode=View&idx=1262"
        parsed = self.adapter.parse_detail(
            BeautifulSoup(html, "lxml"), url, {"source_id": "1262"},
            base_url=BASE_URL, site_prefix="kor",
        )
        with tempfile.TemporaryDirectory() as directory:
            failed = build_attachment(
                index=1, name=parsed["attachments"][0]["name"],
                url=parsed["attachments"][0]["url"], project_root=Path(directory),
                error=attachment_error("REQUEST_FAILED", "fixture failure", True),
            )
            document = apply_common_schema(
                {"title": parsed["title"], "url": url, "category": "공지사항",
                 "subcategory": "공지사항", "content": parsed["body"], "attachments": [failed]},
                source_dataset="fishsci", source_id=parsed["source_id"], source_site=BASE_URL,
                document_type="notice", content_source="query_view_do",
                published_at=parsed["date"], author=parsed["author"],
            )
            self.assertEqual([], validate_common_document(document, Path(directory)))
        self.assertEqual("fishsci:1262", document["id"])
        self.assertEqual("1262", document["source_id"])
        self.assertEqual("2026-07-23T00:00:00+09:00", document["published_at"])
        self.assertFalse(document["attachments"][0]["downloaded"])
        self.assertEqual("REQUEST_FAILED", document["attachments"][0]["error"]["code"])

    def test_five_pinned_items_are_requested_by_source_id(self) -> None:
        """Regression: fishsci used to report 5 discovered but 0 requested."""
        listing = (ROOT / "board_five_pinned.html").read_text(encoding="utf-8")
        detail = (ROOT / "detail_with_attachment.html").read_text(encoding="utf-8")
        section = {
            "id": "menu_44", "name": "공지사항", "url": BASE_URL + "kor/view.do?no=44",
            "bbs_id": "", "category": "공지사항", "type": "notice", "is_board": True,
        }
        captured: list[dict] = []

        class Response:
            def __init__(self, text: str) -> None:
                self.text = text

        def fake_fetch(_session, _url, **kwargs):
            return Response(listing if kwargs.get("params") is not None else detail)

        engine.configure_department(get_department("fishsci"))
        engine.log.disabled = True
        try:
            with (
                patch.object(engine, "fetch", side_effect=fake_fetch),
                patch.object(engine, "save_document", side_effect=lambda doc, _html: captured.append(doc)),
                patch.object(engine, "CRAWL_ALL_BOARD_PAGES", False),
                patch.object(engine, "INITIAL_MAX_PAGES", 1),
            ):
                stats, newest = engine.crawl_board(
                    object(), section, {"items": {}}, is_initial=True,
                    no_download_files=True,
                )
        finally:
            engine.configure_department(get_department("ce"))
            engine.log.disabled = False

        self.assertEqual(5, stats.discovered)
        self.assertEqual(5, stats.requested)
        self.assertEqual(5, stats.new)
        self.assertEqual(0, stats.failed)
        self.assertEqual(0, newest)
        self.assertEqual(["1262", "1261", "1260", "1259", "1258"], [doc["source_id"] for doc in captured])

    def test_unusable_adapter_items_are_reported(self) -> None:
        diagnostics = engine.CrawlDiagnostics()
        page = type("Response", (), {"text": "<html></html>"})()
        section = {
            "id": "broken", "name": "Broken", "url": "https://example.test/list",
            "bbs_id": "", "category": "공지사항", "type": "notice", "is_board": True,
        }
        engine.configure_department(get_department("fishsci"))
        engine.log.disabled = True
        try:
            with (
                patch.object(engine, "fetch", return_value=page),
                patch.object(engine, "parse_list_page", return_value=[{"post_url": "", "source_id": ""}]),
            ):
                stats, _ = engine.crawl_board(
                    object(), section, {"items": {}}, is_initial=True,
                    diagnostics=diagnostics,
                )
        finally:
            engine.configure_department(get_department("ce"))
            engine.log.disabled = False

        self.assertEqual(1, stats.discovered)
        self.assertEqual(0, stats.requested)
        self.assertEqual(1, stats.skipped)
        self.assertEqual(1, stats.failed)
        self.assertEqual("PARSER_MISMATCH", diagnostics.errors[0]["code"])


MPSM_FIXTURES = FIXTURES_ROOT / "departments" / "mpsm"


MPSM_BASE_URL = "https://mpsm.pknu.ac.kr/"


class MpsmFixtureTests(unittest.TestCase):

    def test_query_menu_discovery_is_reusable(self) -> None:
        html = (MPSM_FIXTURES / "homepage.html").read_text(encoding="utf-8")
        menus = get_adapter("query_view_do").discover_menus(html, MPSM_BASE_URL)
        self.assertEqual(["194", "303"], [menu["menu_no"] for menu in menus])

    def test_query_menu_fingerprint_is_supported_by_probe(self) -> None:
        html = "<main>content</main>" + "".join(
            f'<a href="/view.do?no={number}">menu</a>' for number in (194, 303, 304)
        )
        result = analyze_site_html(
            site_key="mpsm.pknu.ac.kr", requested_url=MPSM_BASE_URL,
            final_url=MPSM_BASE_URL, html=html, adapter_name="query_view_legacy",
        )
        self.assertTrue(result.compatible)
        self.assertEqual("compatible", result.status)

    def test_query_discovery_uses_host_alias_as_site_prefix(self) -> None:
        homepage = (MPSM_FIXTURES / "homepage.html").read_text(encoding="utf-8")
        menus = get_adapter("query_view_legacy").discover_menus(homepage, MPSM_BASE_URL)
        section_html = {
            menus[0]["url"]: (MPSM_FIXTURES / "static.html").read_text(encoding="utf-8"),
            menus[1]["url"]: (MPSM_FIXTURES / "board.html").read_text(encoding="utf-8"),
        }
        result = discover_from_html(
            site_key="mpsm.pknu.ac.kr",
            base_url=MPSM_BASE_URL,
            homepage_html=homepage,
            section_html=section_html,
            adapter_name="query_view_legacy",
        )
        self.assertEqual("mpsm", result.site_prefix)

    def test_static_page_uses_existing_content_selector(self) -> None:
        soup = BeautifulSoup((MPSM_FIXTURES / "static.html").read_text(encoding="utf-8"), "lxml")
        parsed = get_adapter("query_view_do").parse_static(soup, fallback_title="학부안내")
        self.assertEqual("학부안내", parsed["title"])
        self.assertIn("안내 본문", parsed["content"])

    def test_legacy_table_and_album_lists_use_idx(self) -> None:
        adapter = get_adapter("query_view_legacy")
        listing = BeautifulSoup((MPSM_FIXTURES / "board.html").read_text(encoding="utf-8"), "lxml")
        album = BeautifulSoup((MPSM_FIXTURES / "album.html").read_text(encoding="utf-8"), "lxml")
        table_items = adapter.parse_list(listing, MPSM_BASE_URL + "view.do?no=303")
        album_items = adapter.parse_list(album, MPSM_BASE_URL + "view.do?no=307")
        self.assertEqual("24367", table_items[0]["source_id"])
        self.assertIn("view=view", table_items[0]["post_url"])
        self.assertEqual("24354", album_items[0]["source_id"])

    def test_legacy_pagination_preserves_menu_and_uses_page_index(self) -> None:
        url, params = get_adapter("query_view_legacy").list_request(
            MPSM_BASE_URL + "view.do?no=303", 2
        )
        self.assertEqual(MPSM_BASE_URL + "view.do?no=303", url)
        self.assertEqual({"pageIndex": 2}, params)

    def test_legacy_detail_and_downfile_are_parsed(self) -> None:
        adapter = get_adapter("query_view_legacy")
        detail = BeautifulSoup((MPSM_FIXTURES / "detail.html").read_text(encoding="utf-8"), "lxml")
        parsed = adapter.parse_detail(
            detail, MPSM_BASE_URL + "view.do?no=303&idx=24367&view=view",
            {"source_id": "24367"}, base_url=MPSM_BASE_URL, site_prefix="mpsm",
        )
        self.assertEqual("24367", parsed["source_id"])
        self.assertEqual("관리자", parsed["author"])
        self.assertEqual("2026-05-26", parsed["date"])
        self.assertIn("재입학 희망자", parsed["body"])
        attachment_url = parsed["attachments"][0]["url"]
        self.assertTrue(attachment_url.startswith(
            "https://mpsm.pknu.ac.kr/base/portal/bbs/downFile.do?"
        ))
        self.assertIn("boardId=BRD0001", attachment_url)
        self.assertIn("idx=24367", attachment_url)
        self.assertIn("fidx=18643", attachment_url)

        canonical_attachments = [
            normalize_attachment(item, index=index, project_root=PROJECT_ROOT)
            for index, item in enumerate(parsed["attachments"], start=1)
        ]
        document = apply_common_schema(
            {
                **parsed, "content": parsed["body"], "category": "커뮤니티",
                "attachments": canonical_attachments,
            },
            source_dataset="mpsm", source_id=parsed["source_id"],
            source_site=MPSM_BASE_URL, document_type="notice",
            content_source="query_view_legacy_html", author=parsed["author"],
            published_at=parsed["date"],
        )
        self.assertEqual([], validate_common_document(document))
        self.assertEqual("mpsm:24367", document["id"])
        self.assertTrue(document["published_at"].endswith("+09:00"))


if __name__ == "__main__":
    unittest.main()
