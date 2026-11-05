'Compatibility CLI for student-life collection; use scripts.crawlers.main.run for all main-site categories.'

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.crawlers.common.schema import RunResult, log_run_result
from scripts.crawlers.common.logging import log_event, set_run_id
from scripts.crawlers.common.storage import empty_state, save_state_atomic
from scripts.crawlers.main.common.storage import STATE_FILE, log, log_context
from scripts.crawlers.main.categories.student_life import LEGACY_PAGE_IDS as CONFIGURED_PAGE_IDS, run


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="부경대 대학생활 가이드 + E-하나로 크롤러")
    parser.add_argument(
        "--mode",
        choices=["guide", "pages", "ebook", "all"],
        default="all",
        help="guide=/main/434, pages=형제 안내페이지, ebook=col_life, all=전부",
    )
    parser.add_argument("--full-resync", action="store_true", help="content_hash 무시하고 재수집")
    parser.add_argument("--reset-state", action="store_true", help="files/pknu_main/_state/pknu_student_life.json 초기화")
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="guide 모드에서 처리할 PDF 최대 건수 (스모크 테스트용)",
    )
    parser.add_argument("--page-ids", help="pages 모드에서 수집할 /main/ 번호 (쉼표 구분)")
    parser.add_argument("--board-pages", type=int, default=1,
                        help="게시판에서 수집할 최신 목록 페이지 수 (기본 1)")
    parser.add_argument("--route-report", type=Path,
                        help="경로 검사 결과 JSON 경로 (기본: files/pknu_main/_runs/student_life_route_inventory.json)")
    return parser.parse_args()


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    args = parse_args()
    if args.page_ids and args.mode != "pages":
        raise SystemExit("--page-ids requires --mode pages")
    if args.board_pages < 1:
        raise SystemExit("--board-pages must be >= 1")
    try:
        page_ids = tuple(dict.fromkeys(int(value.strip()) for value in args.page_ids.split(","))) if args.page_ids else None
    except ValueError as exc:
        raise SystemExit("--page-ids must be comma-separated integers") from exc
    if page_ids is not None and (not page_ids or set(page_ids) - CONFIGURED_PAGE_IDS):
        raise SystemExit(f"unconfigured page ids: {sorted(set(page_ids or ()) - CONFIGURED_PAGE_IDS)}")
    limit = args.limit
    if args.mode == "ebook":
        limit = None

    run_mode = "smoke" if args.limit is not None else ("full" if args.full_resync else "incremental")
    result = RunResult(dataset="pknu_student_life", mode=run_mode)
    set_run_id(log_context, result.run_id)
    log_event(log, logging.INFO, "run_started", mode=run_mode, source_mode=args.mode)
    try:
        if args.reset_state:
            save_state_atomic(STATE_FILE, empty_state("pknu_student_life"), "pknu_student_life")
            log_event(log, logging.INFO, "state_saved", path=STATE_FILE, action="reset")
        result.stats = run(args.mode, args.full_resync, limit,
                           page_ids=page_ids, board_pages=args.board_pages,
                           route_report=args.route_report)
        if result.stats.failed:
            result.add_error("DOCUMENT_FAILURES", f"{result.stats.failed} document(s) failed", retryable=True)
        if result.stats.attachments_failed:
            result.add_error("ATTACHMENT_FAILURES", f"{result.stats.attachments_failed} attachment(s) failed", retryable=True)
        result.finish()
    except KeyboardInterrupt:
        result.finish("cancelled")
    except Exception as exc:
        result.add_error("RUN_INITIALIZATION_FAILED", exc, retryable=True)
        result.finish("failed")
        log_event(log, logging.CRITICAL, "run_finished", status="failed", exc_info=True)
    path = result.save(PROJECT_ROOT)
    if result.status != "failed":
        log_event(log, logging.INFO, "run_finished", status=result.status, stats=result.stats.to_dict(), result_path=path)
    log_run_result(log, result, path)
    return result.exit_code


if __name__ == '__main__':
    raise SystemExit(main())
