"""University introduction pages and organization charts."""

from scripts.crawlers.main.collectors.organization import crawl_org_page
from scripts.crawlers.main.collectors.campus_buildings import crawl_campus_buildings
from scripts.crawlers.main.collectors.facility_rental import crawl_facility_rental
from scripts.crawlers.main.common.pages import collect_pages
from scripts.crawlers.main.common.storage import PROJECT_ROOT
from scripts.crawlers.main.paths import main_root


def execute(items, *, category, full_resync, board_pages, **_options):
    report_path = main_root(PROJECT_ROOT) / "_runs" / "category_routes" / "university_intro.json"
    stats, details = collect_pages(
        [item["page_id"] for item in items], full_resync=full_resync,
        board_pages=board_pages, category=category, report_path=report_path,
        handlers={"organization": crawl_org_page, "campus_buildings": crawl_campus_buildings,
                  "facility_rental": crawl_facility_rental},
    )
    by_id = {detail["page_id"]: detail for detail in details}
    for item in items:
        detail = by_id[item["page_id"]]
        item.update(status=detail["status"], result=detail)
    return {"pages": {"status": "failed" if stats.failed else "completed",
                      "stats": stats.to_dict(),
                      "report_path": report_path.relative_to(PROJECT_ROOT).as_posix()}}
