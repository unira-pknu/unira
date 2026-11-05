"""Collect registered main-site research institute pages with shared handlers."""

from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.crawlers.main.paths import main_root
from scripts.crawlers.main.routes import ROUTES, plan
from scripts.crawlers.main.run import execute

INSTITUTE_CATEGORY = ("연구_산학", "연구기관")
DEFAULT_REPORT = main_root(PROJECT_ROOT) / "_runs" / "research_institutes_run_report.json"


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description=__doc__)
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--page-ids", nargs="+", type=int, metavar="ID")
    selection.add_argument("--all", action="store_true", help="Collect all registered research institutes")
    selection.add_argument("--list", action="store_true", help="List registered research institutes")
    parser.add_argument("--dry-run", action="store_true", help="Show routing without requests or writes")
    parser.add_argument("--board-pages", type=int, default=1, help="Limit board list pages (default: 1)")
    parser.add_argument("--full-resync", action="store_true")
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    args = parser.parse_args(argv)

    registered = {page_id for page_id, route in ROUTES.items()
                  if route.category_path[:2] == INSTITUTE_CATEGORY}
    page_ids = sorted(registered) if args.all or args.list else list(dict.fromkeys(args.page_ids))
    outside_scope = sorted(set(page_ids) - registered)
    if outside_scope:
        parser.error(f"page IDs are not registered research institutes: {outside_scope}")
    if not page_ids:
        parser.error("no research institute pages are registered")
    if args.board_pages < 1:
        parser.error("board-pages must be >= 1")
    if args.list or args.dry_run:
        print(json.dumps({"scope": "연구/산학/연구기관", "routes": plan(page_ids)},
                         ensure_ascii=False, indent=2))
        return 0

    report_path = args.report.resolve()
    try:
        report_path.relative_to(PROJECT_ROOT)
    except ValueError:
        parser.error("report must be inside this repository")
    report = execute(page_ids, year=datetime.now(ZoneInfo("Asia/Seoul")).year,
                     months=list(range(1, 13)), full_resync=args.full_resync,
                     board_pages=args.board_pages, report_path=report_path)
    print(json.dumps({"report": report_path.relative_to(PROJECT_ROOT).as_posix(),
                      "status": report["status"], "routes": report["routes"]}, ensure_ascii=False))
    return int(report["status"] == "failed")


if __name__ == "__main__":
    raise SystemExit(main())
