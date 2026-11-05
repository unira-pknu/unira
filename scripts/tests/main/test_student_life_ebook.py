from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from scripts.crawlers.main.collectors import ebook as crawler
from scripts.crawlers.main.common.storage import PROJECT_ROOT
from scripts.crawlers.common.schema import validate_common_document


class StudentLifeEbookTests(unittest.TestCase):
    def test_html_fallback_collects_page_text_and_marks_unreadable_page(self) -> None:
        def response(text: str) -> SimpleNamespace:
            return SimpleNamespace(
                text=text,
                content=text.encode("utf-8"),
                raise_for_status=lambda: None,
            )

        def fake_fetch(_session: object, url: str) -> SimpleNamespace:
            if url.endswith("config.js"):
                return response("bookConfig.totalPageCount=3;")
            number = int(url.rsplit("page", 1)[1].split(".", 1)[0])
            text = {1: "수강신청 안내", 2: "\u04b4\u0740 1BSU", 3: "성적 관리 안내"}[number]
            return response(f'<div id="content-middle"><div class="text-container">'
                            f'<img src="../thumb/{number}.jpg"><pre><code>{text}</code></pre>'
                            "</div></div>")

        saved: list[dict] = []
        with patch.object(crawler, "discover_ebook_pdf_url", return_value=None), \
             patch.object(crawler, "fetch", side_effect=fake_fetch), \
             patch.object(crawler, "retire_legacy_ebook_metadata"), \
             patch.object(crawler, "save_json", side_effect=lambda doc, *_args: saved.append(doc)):
            state: dict = {"items": {}}
            stats = crawler.crawl_ebook(object(), state, False)

        self.assertEqual((stats.new, stats.failed), (1, 0))
        self.assertEqual(len(saved), 1)
        doc = saved[0]
        self.assertEqual(doc["content_source"], "ebook_basic_html")
        self.assertEqual(doc["subcategory"], "학사정보")
        self.assertEqual([page["text_status"] for page in doc["pages"]],
                         ["readable", "unreadable", "readable"])
        self.assertEqual(doc["pages"][0]["content"], "수강신청 안내")
        self.assertEqual(doc["pages"][1]["content"], "")
        self.assertEqual(doc["pages"][2]["content"], "성적 관리 안내")
        self.assertIn("[Page 1]\n수강신청 안내", doc["content"])
        self.assertIn("[Page 3]\n성적 관리 안내", doc["content"])
        self.assertNotIn("1BSU", doc["content"])
        self.assertIn("ebook_pages_without_readable_text:2", doc["crawl"]["warnings"])
        self.assertEqual(validate_common_document(doc, PROJECT_ROOT), [])

    def test_cleanup_removes_flipbook_noise_but_keeps_columns_and_bullets(self) -> None:
        raw = ("1BSU\n\u0cdf\u088e\u09b8\u0796  1-1. 수강신청 ········ 04\n"
               "\n\n  정규학기                 추가 신청 가능한 경우\n"
               "  ▪ 졸업학점 132학점: 18학점 이내 ( 629-5042 )\n"
               "04 대학생활 “E-하나로”\n")
        content = crawler.clean_ebook_page_text(raw, 4)
        self.assertIn("1-1. 수강신청 | 04", content)
        self.assertIn("정규학기 | 추가 신청 가능한 경우", content)
        self.assertIn("▪ 졸업학점 132학점: 18학점 이내 (629-5042)", content)
        self.assertNotIn("1BSU", content)
        self.assertNotIn("····", content)
        self.assertNotIn("대학생활 “E-하나로”", content)

    def test_missing_page_count_preserves_existing_document(self) -> None:
        response = SimpleNamespace(text="var bookConfig = {};", raise_for_status=lambda: None)
        with patch.object(crawler, "fetch", return_value=response), \
             patch.object(crawler, "save_json") as save:
            state = {"items": {"existing": {"content_hash": "old"}}}
            stats = crawler.crawl_ebook_html(object(), state, False)
        self.assertEqual(stats.failed, 1)
        self.assertEqual(stats.new + stats.updated, 0)
        save.assert_not_called()


if __name__ == "__main__":
    unittest.main()
