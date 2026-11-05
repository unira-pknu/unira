"""Run shared page handlers without assigning them to a site category."""

from copy import deepcopy
from scripts.crawlers.common.schema import CrawlStats, build_attachment
from scripts.crawlers.main.collectors.boards import crawl_board_page
from scripts.crawlers.main.collectors.files import crawl_file_page
from scripts.crawlers.main.collectors.navigation import crawl_linked_pages, inspect_link_hub, inspect_redirect
from scripts.crawlers.main.collectors.organization import crawl_intro_page
from scripts.crawlers.main.collectors.static_pages import crawl_static_pages
from scripts.crawlers.main.common.http import build_session
from scripts.crawlers.main.common.storage import (
    BASE_URL,
    PROJECT_ROOT,
    load_state,
    retire_legacy_static_route,
    save_state,
    write_json,
)
from scripts.crawlers.main.paths import main_root
from scripts.crawlers.main.routes import ROUTES
import json


PAGE_HANDLERS = {"static", "board", "file", "link_hub", "redirect", "linked_pages", "organization_intro"}


def _collect(session, state, page_id, full_resync, board_pages):
    handler = ROUTES[page_id].handler
    if handler == "board":
        return crawl_board_page(session, state, page_id, full_resync, board_pages)
    if handler == "file":
        return crawl_file_page(session, state, page_id, full_resync)
    if handler == "linked_pages":
        return crawl_linked_pages(session, state, page_id, full_resync)
    if handler == "organization_intro":
        return crawl_intro_page(session, page_id, full_resync)
    detail = {"link_hub": inspect_link_hub, "redirect": inspect_redirect}[handler](session, page_id)
    stats = CrawlStats(discovered=1, requested=1)
    if detail["status"] in {"success", "reachable"}:
        stats.skipped = 1
    else:
        stats.failed = 1
    return stats, detail


def _link_files(details):
    files = {item["page_id"]: item for item in details if item.get("route") == "file"}
    for item in details:
        if item.get("route") != "static" or not item.get("output"):
            continue
        linked = [files[page_id] for page_id in item.get("linked_file_page_ids", []) if page_id in files]
        if not linked:
            continue
        item["linked_files"] = linked
        attachments = [build_attachment(
            index=index, name=entry["filename"], url=entry["url"], final_url=entry["url"],
            saved_path=PROJECT_ROOT / entry["saved_path"], downloaded=True,
            content_type="application/pdf", project_root=PROJECT_ROOT,
        ) for index, entry in enumerate(linked, 1)
            if entry.get("saved_path") and entry["status"] in {"new", "updated", "unchanged"}]
        if attachments:
            path = PROJECT_ROOT / item["output"]
            document = json.loads(path.read_text(encoding="utf-8"))
            document["attachments"] = attachments
            write_json(path, document)


def collect_pages(page_ids, *, full_resync=False, board_pages=1, handlers=None,
                  report_path=None, category=None):
    """Collect a category's pages, retaining state and document identities."""
    selected = set(page_ids)
    custom = handlers or {}
    for page_id in selected:
        route = ROUTES.get(page_id)
        if not route or (category and route.category_path[0] != category):
            raise ValueError(f"page {page_id} is outside category {category}")
        if route.handler not in PAGE_HANDLERS | custom.keys():
            raise ValueError(f"no page handler for {route.handler}")
    state = load_state()
    before = deepcopy(state)
    details = []
    try:
        with build_session(BASE_URL) as session:
            static_ids = tuple(sorted(i for i in selected if ROUTES[i].handler == "static"))
            stats = crawl_static_pages(session, state, full_resync, static_ids, route_results=details)
            selected.update(file_id for item in details if ROUTES[item["page_id"]].follow_links
                            for file_id in item.get("linked_file_page_ids", [])
                            if file_id in ROUTES and ROUTES[file_id].handler == "file"
                            and ROUTES[file_id].category_path[0] == ROUTES[item["page_id"]].category_path[0])
            for page_id in sorted(selected - set(static_ids)):
                route = ROUTES[page_id]
                if route.handler in custom:
                    current, detail = custom[route.handler](session, page_id, full_resync)
                else:
                    current, detail = _collect(session, state, page_id, full_resync, board_pages)
                stats.add(current)
                if route.handler in PAGE_HANDLERS and detail["status"] in {
                        "success", "partial_success", "reachable", "new", "updated", "unchanged"}:
                    detail["legacy_static_state_retired"] = retire_legacy_static_route(state, page_id)
                details.append(detail)
            _link_files(details)
    finally:
        if state != before:
            save_state(state)
    if report_path:
        write_json(report_path, {"selected_page_ids": sorted(selected),
                                "static_page_ids": list(static_ids), "routes": details,
                                "stats": stats.to_dict()})
    return stats, details


def execute(items, *, category, full_resync, board_pages, **_options):
    report_path = main_root(PROJECT_ROOT) / "_runs" / "category_routes" / f"{category}.json"
    stats, details = collect_pages([item["page_id"] for item in items],
                                  full_resync=full_resync, board_pages=board_pages,
                                  category=category, report_path=report_path)
    by_id = {detail["page_id"]: detail for detail in details}
    for item in items:
        detail = by_id[item["page_id"]]
        item.update(status=detail["status"], result=detail)
    return {"pages": {"status": "failed" if stats.failed else "completed",
                      "stats": stats.to_dict(),
                      "report_path": report_path.relative_to(PROJECT_ROOT).as_posix()}}
