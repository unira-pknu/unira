"""Student-life collection: academic guides, curriculum, tuition and eBook."""

from scripts.crawlers.common.schema import CrawlStats
from scripts.crawlers.main.common.http import build_session
from scripts.crawlers.main.common.pages import PAGE_HANDLERS, collect_pages
from scripts.crawlers.main.common.storage import (
    EBOOK_INDEX_URL, GUIDE_URL, PROJECT_ROOT, load_state, save_state, write_json,
)
from scripts.crawlers.main.paths import main_root, page_json
from scripts.crawlers.main.routes import ROUTES, STUDENT_LIFE_PAGE_IDS


CATEGORY = "대학생활"
LEGACY_PAGE_IDS = frozenset(i for i in STUDENT_LIFE_PAGE_IDS
                           if ROUTES[i].handler in PAGE_HANDLERS | {"tuition"})
ROUTE_REPORT = main_root(PROJECT_ROOT) / "_runs" / "student_life_route_inventory.json"


def _tuition(session, _page_id, _full_resync):
    from scripts.crawlers.main.collectors.tuition import crawl_tuition_page
    return crawl_tuition_page(session)


def _document_collection(mode, full_resync, limit):
    from scripts.crawlers.main.collectors.ebook import crawl_ebook
    from scripts.crawlers.main.collectors.guides import crawl_guide

    state = load_state()
    try:
        with build_session(GUIDE_URL if mode == "guide" else EBOOK_INDEX_URL) as session:
            return (crawl_guide(session, state, full_resync, limit) if mode == "guide"
                    else crawl_ebook(session, state, full_resync))
    finally:
        save_state(state)


def run(mode, full_resync=False, limit=None, page_ids=None, board_pages=1, route_report=None):
    """Support the existing student-life CLI without collecting other categories."""
    if mode not in {"guide", "pages", "ebook", "all"}:
        raise ValueError(f"unknown student-life mode: {mode}")
    selected = tuple(sorted(LEGACY_PAGE_IDS)) if page_ids is None else tuple(page_ids)
    if set(selected) - LEGACY_PAGE_IDS:
        raise ValueError("use scripts.crawlers.main.run for pages outside the student-life CLI")
    total = CrawlStats()
    if mode in {"guide", "all"}:
        total.add(_document_collection("guide", full_resync, limit if mode == "guide" else None))
    if mode in {"pages", "all"}:
        stats, _ = collect_pages(selected, full_resync=full_resync, board_pages=board_pages,
                                category=CATEGORY, handlers={"tuition": _tuition},
                                report_path=route_report or ROUTE_REPORT)
        total.add(stats)
    if mode in {"ebook", "all"}:
        total.add(_document_collection("ebook", full_resync, None))
    return total


def _calendar_path(page_id, year, months):
    suffix = "" if set(months) == set(range(1, 13)) else "_m" + "-".join(
        f"{month:02d}" for month in sorted(set(months)))
    return page_json(PROJECT_ROOT, page_id, f"main_{page_id}_{year}{suffix}.json")


def execute(items, *, category, year, months, full_resync, board_pages, **_options):
    collectors = {}
    generic = [item for item in items if item["handler"] in PAGE_HANDLERS | {"tuition"}]
    if generic:
        stats, details = collect_pages(
            [item["page_id"] for item in generic], full_resync=full_resync,
            board_pages=board_pages, category=category, handlers={"tuition": _tuition},
            report_path=ROUTE_REPORT,
        )
        by_id = {detail["page_id"]: detail for detail in details}
        for item in generic:
            detail = by_id[item["page_id"]]
            item.update(status=detail["status"], result=detail)
        collectors["pages"] = {"status": "failed" if stats.failed else "completed",
                               "stats": stats.to_dict(),
                               "report_path": ROUTE_REPORT.relative_to(PROJECT_ROOT).as_posix()}
    for item in items:
        if item in generic:
            continue
        handler = item["handler"]
        try:
            if handler == "guide":
                stats = run("guide", full_resync, None)
                item.update(status="failed" if stats.failed else "completed", stats=stats.to_dict())
            elif handler == "academic_calendar":
                from scripts.crawlers.main.collectors.academic_calendar import collect_calendar
                with build_session(item["url"]) as session:
                    data = collect_calendar(session, year, months)
                target = _calendar_path(item["page_id"], year, months)
                write_json(target, data)
                item.update(status=data["status"], output=target.relative_to(PROJECT_ROOT).as_posix(),
                            event_count=data["event_count"], rejected_count=data["rejected_count"])
            elif handler == "major_program":
                from scripts.crawlers.main.collectors.major_program import collect_major_program
                with build_session(item["url"]) as session:
                    data = collect_major_program(session, item["page_id"])
                target = page_json(PROJECT_ROOT, item["page_id"])
                write_json(target, data)
                item.update(status=data["status"], output=target.relative_to(PROJECT_ROOT).as_posix(),
                            image_count=data["image_count"], ocr_block_count=data["ocr_block_count"])
            elif handler == "curriculum_files":
                from scripts.crawlers.main.collectors.curriculum_files import collect_curriculum_files
                with build_session(item["url"]) as session:
                    data = collect_curriculum_files(session, item["page_id"], full_resync=full_resync)
                item.update(status=data["status"], output=data["output"],
                            attachment_count=data["attachment_count"],
                            attachments=[{key: value for key, value in attachment.items() if key != "text_preview"}
                                         for attachment in data["attachments"]])
            else:
                raise ValueError(f"unsupported student-life handler: {handler}")
        except Exception as exc:
            item.update(status="failed", error=f"{type(exc).__name__}: {exc}")
        summary = collectors.setdefault(handler, {"page_ids": [], "status": "completed"})
        summary["page_ids"].append(item["page_id"])
        if item["status"] == "failed":
            summary["status"] = "failed"
        elif item["status"] in {"needs_review", "partial_success"} and summary["status"] != "failed":
            summary["status"] = "needs_review"
    return collectors
