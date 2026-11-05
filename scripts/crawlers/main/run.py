'Main website collection routed by navigation category.'

from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
from zoneinfo import ZoneInfo

from scripts.crawlers.main.routes import BOARD_COLLECTION_HANDLERS, ROUTES, plan
from scripts.crawlers.main.paths import main_root
from scripts.crawlers.main.categories.registry import SITE_CATEGORIES, category_executor
from scripts.crawlers.main.common.storage import write_json as _write_json
from scripts.crawlers.common.logging import configure_crawler_logging, log_event

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_REPORT = main_root(ROOT) / '_runs' / 'main_run_report.json'


def _log_page_result(log: logging.Logger, item: dict) -> None:
    status = item['status']
    labels = {
        'new': '신규 저장', 'updated': '갱신', 'unchanged': '변경 없음',
        'success': '성공', 'completed': '완료', 'collected': '수집 완료',
        'reachable': '연결 확인', 'failed': '실패', 'needs_review': '검토 필요',
        'partial_success': '부분 성공', 'excluded': '제외', 'skipped': '건너뜀',
        'configured': '미완료', 'fallback': 'FALLBACK',
    }
    label = labels.get(status, status)
    level = (logging.ERROR if status == 'failed' else logging.WARNING
             if status in {'needs_review', 'partial_success', 'configured', 'fallback'} else logging.INFO)
    reasons = []
    detail = item.get('result') or {}
    sources = [item, detail]
    for source in sources:
        for key in ('error', 'reason'):
            if source.get(key):
                reasons.append(str(source[key]))
        reasons.extend(str(warning) for warning in (source.get('warnings') or []))
        for key, description in (('rejected_count', '해석 보류'), ('failed_count', '실패')):
            if source.get(key):
                reasons.append(f"{description} {source[key]}건")
        for key, description in (('failed', '문서 실패'), ('attachments_failed', '첨부 실패')):
            count = (source.get('stats') or {}).get(key, 0)
            if count:
                reasons.append(f"{description} {count}건")
    if reasons and level < logging.WARNING:
        label, level = '검토 필요', logging.WARNING
    fallback = status == 'fallback' or any(source.get('fallback_used') for source in sources)
    fallback = fallback or any('fallback' in str(warning).lower()
                              for source in sources for warning in (source.get('warnings') or []))
    if fallback and status != 'failed':
        label, level = 'FALLBACK', logging.WARNING
    log_event(
        log, level, 'page_result', page_id=item['page_id'],
        title=' > '.join(item.get('category_path', [])) or '미등록 페이지',
        url=item['url'], status=status, status_label=label,
        reason='; '.join(dict.fromkeys(reasons)), fallback_used=bool(fallback),
    )


def _inside_repo(path: Path) -> Path:
    resolved = path.resolve()
    resolved.relative_to(ROOT)
    return resolved


def execute(page_ids: list[int], *, year: int, months: list[int], full_resync: bool,
            board_pages: int | None, report_path: Path) -> dict:
    routes = plan(page_ids)
    log, _context = configure_crawler_logging("pknu_main", ROOT)
    report = dict(generated_at=datetime.now(timezone.utc).isoformat(),
                  requested_page_ids=list(dict.fromkeys(page_ids)), year=year,
                  board_pages=board_pages, full_resync=full_resync,
                  routes=routes, collectors={})
    grouped = defaultdict(list)
    for item in routes:
        if item['status'] == 'configured':
            grouped[item['category_path'][0]].append(item)
        else:
            _log_page_result(log, item)
    log_event(log, logging.INFO, "batch_started", categories=len(grouped), pages=len(routes))
    for index, (category, items) in enumerate(grouped.items(), 1):
        log_event(log, logging.INFO, "section_started", category=category,
                  current=index, total=len(grouped), page_ids=[item['page_id'] for item in items])
        try:
            collectors = category_executor(category)(
                items, category=category, year=year, months=months,
                full_resync=full_resync, board_pages=board_pages,
                all_board_pages=board_pages is None)
            report['collectors'][category] = collectors
        except Exception as exc:
            for item in items:
                item.update(status='failed', error=f'{type(exc).__name__}: {exc}')
            report['collectors'][category] = dict(status='failed', error=items[0]['error'])
            log_event(log, logging.ERROR, "request_failed", category=category, error=str(exc), exc_info=True)
        for item in items:
            _log_page_result(log, item)
        log_event(log, logging.INFO, "section_finished", category=category,
                  current=index, total=len(grouped),
                  results=[{'page_id': item['page_id'], 'status': item['status']} for item in items])
    statuses = {item['status'] for item in routes}
    report['status'] = ('failed' if 'failed' in statuses else 'needs_review'
                        if statuses & {'needs_review', 'partial_success'} else 'completed')
    _write_json(report_path, report)
    log_event(log, logging.INFO, "batch_finished", status=report['status'], report=report_path)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--page-ids", nargs="+", type=int, metavar="ID")
    selection.add_argument("--all", action="store_true", help="Run every registered page")
    selection.add_argument("--boards", action="store_true",
                           help="Select registered boards, notices and the tuition FAQ")
    selection.add_argument("--list", action="store_true", help="List registered routes")
    selection.add_argument('--category', choices=tuple(SITE_CATEGORIES),
                           help='Collect registered pages in one navigation category')
    parser.add_argument("--dry-run", action="store_true", help="Show routing without requests or writes")
    parser.add_argument("--year", type=int, default=datetime.now(ZoneInfo("Asia/Seoul")).year)
    parser.add_argument("--months", nargs="+", type=int, metavar="MONTH")
    board_scope = parser.add_mutually_exclusive_group()
    board_scope.add_argument("--board-pages", type=int, default=1,
                             help="Limit general board list pages (default: 1)")
    board_scope.add_argument("--all-board-pages", action="store_true",
                             help="Collect all list pages, including notices, reusing existing results")
    parser.add_argument("--full-resync", action="store_true")
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    args = parser.parse_args()
    if args.year < 2000 or args.year > 2100 or args.board_pages < 1:
        parser.error("year must be 2000-2100 and board-pages must be >= 1")
    months = args.months or list(range(1, 13))
    if any(not 1 <= month <= 12 for month in months):
        parser.error("months must be 1-12")
    if args.category:
        directory = SITE_CATEGORIES[args.category]['directory']
        page_ids = sorted(i for i, route in ROUTES.items() if route.category_path[0] == directory)
    elif args.boards:
        page_ids = sorted(i for i, route in ROUTES.items() if route.handler in BOARD_COLLECTION_HANDLERS)
    else:
        page_ids = sorted(ROUTES) if args.all or args.list else args.page_ids
    if args.months and 31 not in page_ids:
        parser.error("--months requires page 31")
    if args.list or args.dry_run:
        print(json.dumps({"routes": plan(page_ids),
                          "board_pages": "all" if args.all_board_pages else args.board_pages},
                         ensure_ascii=False, indent=2))
        return 0
    try:
        report_path = _inside_repo(args.report)
    except ValueError:
        parser.error("report must be inside this repository")
    report = execute(page_ids, year=args.year, months=months,
                     full_resync=args.full_resync,
                     board_pages=None if args.all_board_pages else args.board_pages,
                     report_path=report_path)
    print(json.dumps({"report": report_path.relative_to(ROOT).as_posix(),
                      "status": report["status"], "routes": report["routes"]},
                     ensure_ascii=False))
    return int(report["status"] == "failed")


if __name__ == '__main__':
    raise SystemExit(main())
