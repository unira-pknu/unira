"""Community notices and first-page university news collection."""

import requests

from scripts.crawlers.main.common.storage import PROJECT_ROOT


def execute(items, *, full_resync, all_board_pages=False, **_options):
    collectors = {}
    notices = [item for item in items if item["handler"] == "notice"]
    if notices:
        from scripts.crawlers import pknu_notice
        try:
            stats = pknu_notice.run_crawl(
                full_resync=full_resync, recent_only=None, only_cd=None, all_pages=all_board_pages,
            )
            status = "failed" if stats.failed else "completed"
            for item in notices:
                item.update(status=status, stats=stats.to_dict())
            collectors["notice"] = {"status": status, "stats": stats.to_dict()}
        except Exception as exc:
            for item in notices:
                item.update(status="failed", error=f"{type(exc).__name__}: {exc}")
            collectors["notice"] = {"status": "failed", "error": notices[0]["error"]}
    today = [item for item in items if item["handler"] == "today"]
    if today:
        from scripts.crawlers.main.collectors.pknu_today import OUTPUT, collect_first_page
        with requests.Session() as session:
            session.headers.update({"User-Agent": "campus-rag/1.0 (+https://www.pknu.ac.kr)"})
            for item in today:
                try:
                    data = collect_first_page(session, item["page_id"])
                    manifest = OUTPUT / "_runs" / "pknu_today" / f'main_{item["page_id"]}_page1.json'
                    item.update(status=data["status"], output=manifest.relative_to(PROJECT_ROOT).as_posix(),
                                post_count=data["saved_count"], failed_count=data["failed_count"])
                except Exception as exc:
                    item.update(status="failed", error=f"{type(exc).__name__}: {exc}")
        collectors["today"] = {
            "status": "failed" if any(item["status"] == "failed" for item in today)
            else "needs_review" if any(item["status"] == "needs_review" for item in today)
            else "completed", "page_ids": [item["page_id"] for item in today],
            "list_pages_per_section": 1,
        }
    other = [item for item in items if item["handler"] not in {"notice", "today"}]
    if other:
        from scripts.crawlers.main.common.pages import execute as execute_pages
        collectors.update(execute_pages(other, full_resync=full_resync, **_options))
    return collectors
