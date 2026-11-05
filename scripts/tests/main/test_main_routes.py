from __future__ import annotations

import unittest
import io
import json
from contextlib import nullcontext
from unittest.mock import patch

from scripts.crawlers.main import run as main_run
from scripts.crawlers.main import routes


class MainRouteTests(unittest.TestCase):
    def test_board_selection_and_all_page_scope_reach_execution(self) -> None:
        configured = {
            9001: routes.MainRoute(9001, "board", ("대학생활", "게시판")),
            9002: routes.MainRoute(9002, "notice", ("커뮤니티", "공지사항")),
            9003: routes.MainRoute(9003, "tuition", ("대학생활", "등록금")),
            9004: routes.MainRoute(9004, "static", ("대학생활", "안내")),
        }
        with (patch.object(main_run, "ROUTES", configured),
              patch("sys.argv", ["run", "--boards", "--all-board-pages"]),
              patch.object(main_run, "execute", return_value={"status": "completed", "routes": []}) as execute,
              patch("sys.stdout", io.StringIO())):
            self.assertEqual(main_run.main(), 0)
        self.assertEqual(execute.call_args.args[0], [9001, 9002, 9003])
        self.assertIsNone(execute.call_args.kwargs["board_pages"])
        self.assertFalse(execute.call_args.kwargs["full_resync"])

    def test_board_dry_run_does_not_execute_collectors(self) -> None:
        with (patch("sys.argv", ["run", "--boards", "--all-board-pages", "--dry-run"]),
              patch("sys.stdout", io.StringIO()) as output,
              patch.object(main_run, "execute") as execute):
            self.assertEqual(main_run.main(), 0)
        execute.assert_not_called()
        result = json.loads(output.getvalue())
        self.assertEqual(result["board_pages"], "all")
        self.assertTrue(result["routes"])
        self.assertTrue(all(item["handler"] in routes.BOARD_COLLECTION_HANDLERS
                            and item["status"] == "configured" for item in result["routes"]))

    def test_board_list_limits_are_mutually_exclusive(self) -> None:
        with (patch("sys.argv", ["run", "--boards", "--all-board-pages", "--board-pages", "2"]),
              patch("sys.stderr", io.StringIO()), patch.object(main_run, "execute") as execute):
            with self.assertRaises(SystemExit) as exc:
                main_run.main()
        self.assertEqual(exc.exception.code, 2)
        execute.assert_not_called()

    def test_archive_scope_reaches_categories_without_forcing_resync(self) -> None:
        def collect(items, **options):
            self.assertIsNone(options["board_pages"])
            self.assertTrue(options["all_board_pages"])
            self.assertFalse(options["full_resync"])
            for item in items:
                item["status"] = "completed"
            return {}

        with (patch.object(main_run, "_write_json"),
              patch.object(main_run, "category_executor", return_value=collect)):
            report = main_run.execute([32, 95, 102, 163], year=2026, months=[1], full_resync=False,
                                      board_pages=None, report_path=main_run.DEFAULT_REPORT)
        self.assertIsNone(report["board_pages"])
        self.assertFalse(report["full_resync"])
        self.assertTrue(all(item["status"] == "completed" for item in report["routes"]))

    def test_invalid_route_configuration_is_rejected_before_crawling(self) -> None:
        with patch.dict(routes.ROUTE_GROUPS, {"static": (*routes.STATIC_PAGE_IDS, 100)}):
            with self.assertRaisesRegex(ValueError, "excluded main pages"):
                routes._build_routes()
        with patch.object(routes, "NO_FOLLOW_LINK_PAGE_IDS", frozenset({999})):
            with self.assertRaisesRegex(ValueError, "no-follow pages must be static"):
                routes._build_routes()

    def test_runner_delegates_only_registered_pages(self) -> None:
        def collected(items, **options):
            self.assertTrue(all(item["category_path"][0] == options["category"]
                                for item in items))
            for item in items:
                item["status"] = "completed"
            return {"status": "completed"}

        with (patch.object(main_run, "_write_json") as write_json,
              patch("scripts.crawlers.main.categories.student_life.execute", side_effect=collected) as student_life,
              patch("scripts.crawlers.main.categories.community.execute", side_effect=collected) as community,
              patch("scripts.crawlers.main.categories.university_intro.execute", side_effect=collected) as intro):
            report = main_run.execute([95, 163, 434, 38, 39, 999], year=2026,
                                      months=list(range(1, 13)), full_resync=False,
                                      board_pages=1, report_path=main_run.DEFAULT_REPORT)

        student_life.assert_called_once()
        community.assert_called_once()
        intro.assert_called_once()
        self.assertEqual([item["page_id"] for item in student_life.call_args.args[0]], [95, 434])
        self.assertEqual([item["page_id"] for item in community.call_args.args[0]], [163])
        self.assertEqual([item["page_id"] for item in intro.call_args.args[0]], [38, 39])
        statuses = {row["page_id"]: row["status"] for row in report["routes"]}
        self.assertEqual(statuses, {95: "completed", 163: "completed", 434: "completed",
                                    38: "completed", 39: "completed", 999: "needs_review"})
        self.assertEqual(report["status"], "needs_review")
        write_json.assert_called_once()

    def test_curriculum_route_keeps_ocr_preview_out_of_console_report(self) -> None:
        result = {
            "status": "needs_review", "output": "files/pknu_main/대학생활/교육과정/json/pages/main_106.json",
            "attachment_count": 1,
            "attachments": [{"title": "안내서", "text_preview": "« OCR 결과", "text_status": "success"}],
        }
        with (patch.object(main_run, "_write_json"),
              patch("scripts.crawlers.main.categories.student_life.build_session", return_value=nullcontext(object())),
              patch("scripts.crawlers.main.collectors.curriculum_files.collect_curriculum_files",
                    return_value=result)):
            report = main_run.execute([106], year=2026, months=list(range(1, 13)),
                                      full_resync=False, board_pages=1,
                                      report_path=main_run.DEFAULT_REPORT)
        route = report["routes"][0]
        self.assertEqual(route["status"], "needs_review")
        self.assertNotIn("text_preview", route["attachments"][0])

    def test_page_result_reports_nested_warnings_and_attachment_failures(self) -> None:
        for extra, label, reason in (
            ({"stats": {"attachments_failed": 3}}, "검토 필요", "첨부 실패 3건"),
            ({"result": {"warnings": ["OCR_EXECUTABLE_MISSING"]}}, "검토 필요", "OCR_EXECUTABLE_MISSING"),
            ({"result": {"fallback_used": True}}, "FALLBACK", ""),
        ):
            with self.subTest(extra=extra), patch.object(main_run, "log_event") as event:
                main_run._log_page_result(object(), {
                    "page_id": 71, "category_path": ["대학원", "경영대학원"],
                    "url": "https://www.pknu.ac.kr/main/71", "status": "completed", **extra,
                })
                self.assertEqual(event.call_args.kwargs["status_label"], label)
                self.assertEqual(event.call_args.kwargs["reason"], reason)

    def test_runner_logs_each_page_when_a_category_fails(self) -> None:
        with (patch.object(main_run, "_write_json"),
              patch.object(main_run, "category_executor", side_effect=RuntimeError("broken category")),
              patch.object(main_run, "log_event") as event):
            # An exception must produce a page result rather than only a category error.
            main_run.execute([71, 72, 999], year=2026, months=[1], full_resync=False,
                             board_pages=1, report_path=main_run.DEFAULT_REPORT)
        results = [call.kwargs for call in event.call_args_list if call.args[2] == "page_result"]
        self.assertEqual({item["page_id"] for item in results}, {71, 72, 999})
        self.assertEqual(len(results), 3)
        failed = [item for item in results if item["page_id"] != 999]
        self.assertTrue(all(item["status"] == "failed" for item in failed))
        self.assertTrue(all("broken category" in item["reason"] for item in failed))


if __name__ == "__main__":
    unittest.main()
