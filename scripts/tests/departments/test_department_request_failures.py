from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

import requests

from scripts.crawlers.common.schema import CrawlStats, RunResult
from scripts.crawlers.departments import engine
from scripts.crawlers.departments.adapters import get_adapter
from scripts.crawlers.departments.access import ACCESS_BLOCKED
from scripts.crawlers.departments.config import get_department
from scripts.crawlers.departments.tls import NETWORK_REQUEST_FAILED, TLS_CERTIFICATE_VERIFY_FAILED


def response(status: int = 200, *, url: str = "https://example.test/list", text: str = "<html></html>"):
    item = requests.Response()
    item.status_code = status
    item.url = url
    item._content = text.encode("utf-8")
    return item


class DepartmentRequestFailureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        engine.configure_department(get_department("ce"))
        engine.log.disabled = True

    @classmethod
    def tearDownClass(cls) -> None:
        engine.log.disabled = False

    def test_http_404_is_non_retryable(self) -> None:
        with (
            patch.object(engine.time, "sleep"),
            patch.object(engine, "get_with_tls_policy", return_value=(response(404), False)),
        ):
            self.assertIsNone(engine.fetch(Mock(), "https://example.test/list"))
        failure = engine._LAST_FETCH_FAILURE
        self.assertEqual("HTTP_ERROR", failure.code)
        self.assertFalse(failure.retryable)

    def test_network_and_tls_errors_are_distinguished(self) -> None:
        cases = (
            (requests.ConnectionError("offline"), NETWORK_REQUEST_FAILED, True),
            (requests.exceptions.SSLError("CERTIFICATE_VERIFY_FAILED"), TLS_CERTIFICATE_VERIFY_FAILED, False),
        )
        for exc, code, retryable in cases:
            with self.subTest(code=code):
                with (
                    patch.object(engine.time, "sleep"),
                    patch.object(engine, "get_with_tls_policy", side_effect=exc),
                ):
                    self.assertIsNone(engine.fetch(Mock(), "https://example.test/list"))
                    self.assertEqual(code, engine._LAST_FETCH_FAILURE.code)
                    self.assertEqual(retryable, engine._LAST_FETCH_FAILURE.retryable)

    def test_http_200_denial_page_is_access_blocked(self) -> None:
        denied = response(
            url="https://example.test/common/deny.jsp",
            text="<html><title>Access denied</title><body>Request blocked</body></html>",
        )
        with (
            patch.object(engine.time, "sleep"),
            patch.object(engine, "get_with_tls_policy", return_value=(denied, False)),
        ):
            self.assertIsNone(engine.fetch(Mock(), "https://example.test/list"))
        self.assertEqual(ACCESS_BLOCKED, engine._LAST_FETCH_FAILURE.code)
        self.assertFalse(engine._LAST_FETCH_FAILURE.retryable)

    def test_initial_list_failure_increments_failed_and_records_error(self) -> None:
        section = engine.SECTIONS[0]
        diagnostics = engine.CrawlDiagnostics()
        failure = engine.RequestFailure("HTTP_ERROR", section["url"], "HTTP 404", False)
        with (
            patch.object(engine, "fetch", return_value=None),
            patch.object(engine, "_LAST_FETCH_FAILURE", failure),
        ):
            stats, _ = engine.crawl_board(
                Mock(), section, {"items": {}}, is_initial=True, diagnostics=diagnostics
            )
        self.assertEqual(1, stats.failed)
        self.assertEqual(0, diagnostics.sections_initialized)
        self.assertEqual("HTTP_ERROR", diagnostics.errors[0]["code"])
        self.assertFalse(diagnostics.errors[0]["retryable"])

    def test_pagination_failure_is_counted_after_first_page_succeeds(self) -> None:
        section = engine.SECTIONS[0]
        diagnostics = engine.CrawlDiagnostics()
        item = {
            "is_notice": False,
            "post_no": 7,
            "post_url": "https://example.test/view/7",
            "source_id": "7",
        }
        detail = {
            "slug": "ignored",
            "title": "fixture",
            "date": "2026-09-12",
            "url": item["post_url"],
            "is_notice": False,
            "body": "fixture content comments",
            "attachments": [],
        }
        page = response(text="<html></html>", url=item["post_url"])
        failure = engine.RequestFailure("HTTP_ERROR", section["url"], "HTTP 503", True)
        with (
            patch.object(engine, "fetch", side_effect=[page, page, None]),
            patch.object(engine, "_LAST_FETCH_FAILURE", failure),
            patch.object(engine, "parse_list_page", return_value=[item]),
            patch.object(engine, "parse_view_page", return_value=detail),
            patch.object(engine, "save_document"),
            patch.object(engine, "load_existing_attachments", return_value=[]),
            patch.object(engine, "save_attachments", return_value=[]),
            patch.object(engine, "CRAWL_ALL_BOARD_PAGES", True),
            patch.object(engine, "REUSE_EXISTING_ATTACHMENTS", False),
        ):
            stats, _ = engine.crawl_board(
                Mock(), section, {"items": {}}, is_initial=True, diagnostics=diagnostics
            )
        self.assertEqual(1, stats.requested)
        self.assertEqual(1, stats.failed)
        self.assertEqual(1, diagnostics.sections_initialized)
        self.assertEqual("HTTP_ERROR", diagnostics.errors[0]["code"])

    def test_failed_section_does_not_stop_following_section(self) -> None:
        sections = [
            {"id": "one", "name": "One", "url": "https://example.test/1", "is_board": False},
            {"id": "two", "name": "Two", "url": "https://example.test/2", "is_board": False},
        ]
        diagnostics = engine.CrawlDiagnostics()

        def fake_static(_session, section, diagnostics=None, no_download_files=False,
                        max_items=None, visited_urls=None, parent_url=None):
            if section["id"] == "one":
                diagnostics.errors.append({
                    "code": "HTTP_ERROR", "source_id": "one", "url": section["url"],
                    "message": "HTTP 503", "retryable": True,
                })
                return CrawlStats(failed=1)
            diagnostics.sections_initialized += 1
            return CrawlStats(requested=1, unchanged=1)

        with (
            patch.object(engine, "SECTIONS", sections),
            patch.object(engine, "load_state", return_value={"items": {}}),
            patch.object(engine, "save_state"),
            patch.object(engine, "build_session", return_value=Mock()),
            patch.object(engine, "crawl_static", side_effect=fake_static) as crawl_static,
        ):
            stats = engine.run_crawl(diagnostics=diagnostics)
        self.assertEqual(2, crawl_static.call_count)
        self.assertEqual(1, stats.failed)
        self.assertEqual(1, stats.unchanged)
        self.assertEqual(1, diagnostics.sections_initialized)
        self.assertEqual("HTTP_ERROR", diagnostics.errors[0]["code"])

    def test_run_status_is_partial_when_any_section_initialized(self) -> None:
        result = RunResult(dataset="ce", mode="incremental", stats=CrawlStats(failed=1))
        result.add_error("HTTP_ERROR", "HTTP 500", retryable=True)
        engine.finish_run_result(
            result, engine.CrawlDiagnostics(sections_selected=2, sections_initialized=1)
        )
        self.assertEqual("partial_success", result.status)

    def test_run_status_is_failed_when_no_section_initialized(self) -> None:
        result = RunResult(dataset="ce", mode="incremental", stats=CrawlStats(failed=2))
        result.add_error("NETWORK_REQUEST_FAILED", "offline", retryable=True)
        engine.finish_run_result(
            result, engine.CrawlDiagnostics(sections_selected=2, sections_initialized=0)
        )
        self.assertEqual("failed", result.status)

    def test_empty_list_and_parser_mismatch_are_distinguished(self) -> None:
        section = engine.SECTIONS[0]
        page = response(text='<a href="?action=view&bbsId=123&nttId=9">candidate</a>')
        diagnostics = engine.CrawlDiagnostics()
        with (
            patch.object(engine, "fetch", return_value=page),
            patch.object(engine, "parse_list_page", return_value=[]),
            patch.object(engine, "log_event") as log_event,
        ):
            stats, _ = engine.crawl_board(
                Mock(), section, {"items": {}}, is_initial=True, diagnostics=diagnostics,
            )
        self.assertEqual(1, stats.failed)
        self.assertEqual("PARSER_MISMATCH", diagnostics.errors[0]["code"])
        self.assertFalse(diagnostics.errors[0]["retryable"])
        self.assertEqual(0, diagnostics.empty_sections)
        self.assertTrue(any(
            call.kwargs.get("reason") == "parser_mismatch"
            for call in log_event.call_args_list
        ))

        diagnostics = engine.CrawlDiagnostics(sections_selected=1)
        with (
            patch.object(engine, "fetch", return_value=response(text="<table><tbody></tbody></table>")),
            patch.object(engine, "parse_list_page", return_value=[]),
        ):
            stats, _ = engine.crawl_board(
                Mock(), section, {"items": {}}, is_initial=True, diagnostics=diagnostics,
            )
        result = RunResult(dataset="ce", mode="incremental", stats=stats)
        engine.finish_run_result(result, diagnostics)
        self.assertEqual(1, diagnostics.empty_sections)
        self.assertEqual("partial_success", result.status)
        self.assertEqual("NO_DOCUMENTS_VERIFIED", result.errors[0].code)
        self.assertFalse(result.errors[0].retryable)

    def test_output_path_failure_is_non_retryable_and_next_section_runs(self) -> None:
        sections = [
            {"id": "one", "name": "One", "url": "https://example.test/1", "is_board": False},
            {"id": "two", "name": "Two", "url": "https://example.test/2", "is_board": False},
        ]
        diagnostics = engine.CrawlDiagnostics()

        def fake_static(_session, section, diagnostics=None, no_download_files=False,
                        max_items=None, visited_urls=None, parent_url=None):
            if section["id"] == "one":
                raise OSError("output directory is not writable")
            diagnostics.sections_initialized += 1
            return CrawlStats(requested=1, unchanged=1)

        with (
            patch.object(engine, "SECTIONS", sections),
            patch.object(engine, "load_state", return_value={"items": {}}),
            patch.object(engine, "save_state"),
            patch.object(engine, "build_session", return_value=Mock()),
            patch.object(engine, "crawl_static", side_effect=fake_static) as crawl_static,
        ):
            stats = engine.run_crawl(diagnostics=diagnostics)
        self.assertEqual(2, crawl_static.call_count)
        self.assertEqual(1, stats.failed)
        self.assertEqual(1, stats.unchanged)
        self.assertEqual("OUTPUT_PATH_ERROR", diagnostics.errors[0]["code"])
        self.assertFalse(diagnostics.errors[0]["retryable"])

    def test_source_id_variant_stops_when_next_page_repeats(self) -> None:
        section = engine.SECTIONS[0]
        item = {
            "source_id": "card-A", "post_url": "https://example.test/view/card-A",
            "post_no": None, "is_notice": False, "date": "2026-09-15",
        }
        detail = {
            "title": "Card fixture", "date": "2026-09-15", "url": item["post_url"],
            "is_notice": False, "body": "content", "attachments": [],
        }
        page = Mock(text="<html></html>")
        with (
            patch.object(engine, "fetch", return_value=page) as fetch,
            patch.object(engine, "parse_list_page", return_value=[item]),
            patch.object(engine, "parse_view_page", return_value=detail),
            patch.object(engine, "save_document"),
            patch.object(engine, "CRAWL_ALL_BOARD_PAGES", True),
        ):
            stats, _ = engine.crawl_board(
                Mock(), section, {"items": {}}, is_initial=True, no_download_files=True,
            )
        self.assertEqual(1, stats.requested)
        self.assertEqual(3, fetch.call_count)  # page 1, detail, repeated page 2

    def test_page_index_changes_do_not_repeat_pinned_posts_or_hide_new_posts(self) -> None:
        section = engine.SECTIONS[0]

        def item(source_id, page, pinned=False):
            return {"source_id": source_id, "post_no": None if pinned else int(source_id),
                    "post_url": f"https://example.test/view?idx={source_id}&pageIndex={page}",
                    "is_notice": pinned, "date": "2026-10-27"}

        pages = [[item("90", 1, True), item("2", 1)],
                 [item("90", 2, True), item("1", 2)], [item("90", 3, True)]]
        requested = []

        def fetch(_session, url, **options):
            if options.get("params") is not None:
                if options["params"]["pageIndex"] > 3:
                    raise AssertionError("crawler passed the pinned-only end of the board")
            else:
                requested.append(url)
            return response(url=url)

        def parse(_soup, url, original):
            return {"title": "fixture", "date": original["date"], "url": url,
                    "is_notice": original["is_notice"], "body": "fixture article content", "attachments": []}

        with (
            TemporaryDirectory() as directory,
            patch.object(engine, "ACTIVE_ADAPTER", get_adapter("query_view_do")),
            patch.object(engine, "fetch", side_effect=fetch),
            patch.object(engine, "parse_list_page", side_effect=pages),
            patch.object(engine, "parse_view_page", side_effect=parse),
            patch.object(engine, "existing_document_path", return_value=Path(directory) / "missing.json"),
            patch.object(engine, "save_document"), patch.object(engine, "log_event") as event,
            patch.object(engine, "CRAWL_ALL_BOARD_PAGES", True),
        ):
            stats, _ = engine.crawl_board(Mock(), section, {"items": {}}, True, no_download_files=True)
        self.assertEqual(len(requested), 3)
        self.assertEqual(stats.requested, 3)
        self.assertEqual(stats.failed, 0)
        self.assertEqual(sum("idx=90" in url for url in requested), 1)
        stopped = [call for call in event.call_args_list if call.args[2] == "pagination_stopped"]
        self.assertEqual(stopped[0].kwargs["page"], 3)

    def test_success_and_unchanged_status_are_unchanged(self) -> None:
        result = RunResult(dataset="ce", mode="incremental", stats=CrawlStats(unchanged=3))
        engine.finish_run_result(
            result, engine.CrawlDiagnostics(sections_selected=1, sections_initialized=1)
        )
        self.assertEqual("success", result.status)
        self.assertEqual(3, result.stats.unchanged)
        self.assertEqual(0, result.stats.failed)


if __name__ == "__main__":
    unittest.main()
