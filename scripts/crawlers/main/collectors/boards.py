'Main website collectors boards.'

from __future__ import annotations
from bs4 import BeautifulSoup, NavigableString
from scripts.crawlers.common.logging import log_event
from scripts.crawlers.common.main_static import extract_structure
from scripts.crawlers.common.schema import (
    CrawlStats,
    apply_common_schema,
    build_attachment,
    now_kst,
)
from scripts.crawlers.main.collectors.static_pages import (
    StaticPageExtraction,
    crawl_static_pages,
    parse_static_page_html,
)
from scripts.crawlers.main.common import board as cms_board
from scripts.crawlers.main.common.http import fetch, is_html_response
from scripts.crawlers.main.common.storage import BASE_URL, PATHS, PROJECT_ROOT, log, save_json, write_json
from scripts.crawlers.main.paths import page_json
from scripts.crawlers.main.routes import ROUTES
from typing import Any
import json
import logging
import requests


SUBCATEGORY_BOARD_POST = "학점교류_게시판"


SUBCATEGORY_WEEKLY_MENU = "교내_식당_주간식단표"


def board_post_subcategory(page_id: int) -> str:
    """Store growing board archives in the board's configured category."""
    return ROUTES[page_id].options.get("post_subcategory", ROUTES[page_id].category_path[-1])


def parse_board_intro_html(html: str, page_id: int) -> StaticPageExtraction:
    """Parse the fixed introduction above a board with the shared static parser."""
    soup = BeautifulSoup(html, "lxml")
    body = soup.select_one("#subCont")
    if body is None or body.select_one("table.brdList") is None:
        return StaticPageExtraction(f"main-{page_id}", "", "needs_review", "board_intro_missing")
    for element in body.select("#frmPost, .bdCont, table.brdList, .paging, .brdBtn"):
        element.decompose()
    for child in list(body.children):
        if isinstance(child, NavigableString) and child.strip() in {"subMenu", "bdCont"}:
            child.extract()
    return parse_static_page_html(str(soup), page_id)


def crawl_board_page(
    session: requests.Session, state: dict[str, Any], page_id: int,
    full_resync: bool, max_pages: int | None = 1, *, max_posts: int | None = None,
) -> tuple[CrawlStats, dict[str, Any]]:
    """Collect a board and its posts; max_pages=None visits every list page."""
    if max_pages is not None and (type(max_pages) is not int or max_pages < 1):
        raise ValueError("max_pages must be a positive integer or None")
    if max_posts is not None and (type(max_posts) is not int or max_posts < 1):
        raise ValueError("max_posts must be a positive integer")
    url = f"{BASE_URL}/main/{page_id}"
    stats = CrawlStats()
    result: dict[str, Any] = {"page_id": page_id, "route": "board", "url": url}
    try:
        first = fetch(session, url)
        if not is_html_response(first, url):
            raise ValueError(f"board_list_unverified: HTTP {first.status_code}, {first.url}")
        soup = BeautifulSoup(first.text, "lxml")
        bbs_input = soup.select_one('input[name="bbsId"]')
        bbs_id = (bbs_input.get("value") or "").strip() if bbs_input else ""
        options = ROUTES[page_id].options
        expected_bbs_id = options.get("board_id")
        if bbs_id != expected_bbs_id:
            raise ValueError(f"board_id_changed: {bbs_id!r}")
        _, total_pages = cms_board.parse_page_indicator(first.text)
        pages = total_pages if max_pages is None else min(max_pages, total_pages)
        items: dict[str, cms_board.ListItem] = {}
        for page in range(1, pages + 1):
            response = first if page == 1 else fetch(session, f"{url}?bbsId={bbs_id}&pageIndex={page}")
            if page > 1 and not is_html_response(response, f"{url}?bbsId={bbs_id}&pageIndex={page}"):
                raise ValueError(f"board_page_unverified: page {page}")
            label = options.get("board_title", ROUTES[page_id].category_path[-1])
            found = cms_board.parse_list_page(response.text, bbs_id, label)
            if not found:
                raise ValueError(f"board_list_empty: page {page}")
            items.update((item.no, item) for item in found)
        listed_count = len(items)
        if max_posts is not None:
            items = dict(list(items.items())[:max_posts])
        stats.discovered = len(items)
        result.update(bbs_id=bbs_id, total_pages=total_pages, pages_crawled=pages,
                      listed_count=listed_count, post_limit=max_posts,
                      discovered=len(items), documents=[])
    except (requests.RequestException, ValueError) as exc:
        stats.failed += 1
        result.update(status="failed", reason=str(exc), retryable=isinstance(exc, requests.RequestException))
        log_event(log, logging.ERROR, "section_finished", section=f"main:{page_id}", status="failed", error=exc)
        return stats, result

    if options.get("collect_intro"):
        intro_details: list[dict[str, Any]] = []
        intro_stats = crawl_static_pages(
            session, state, full_resync, (page_id,), route_results=intro_details,
            prepared={page_id: parse_board_intro_html(first.text, page_id)},
        )
        stats.add(intro_stats)
        result["intro"] = intro_details[0]

    for post_no, item in items.items():
        post_url = f"{url}?action=view&no={post_no}"
        stats.requested += 1
        try:
            response = fetch(session, post_url)
            if not is_html_response(response, post_url):
                raise ValueError(f"detail_response_unverified: HTTP {response.status_code}, {response.url}")
            detail_soup = BeautifulSoup(response.text, "lxml")
            board_input = detail_soup.select_one('#frmPost input[name="bbsId"]')
            post_input = detail_soup.select_one('#frmPost input[name="chkNo"]')
            if (board_input is None or board_input.get("value") != bbs_id
                    or post_input is None or post_input.get("value") != post_no):
                raise ValueError("detail_identity_unverified")
            detail = cms_board.parse_detail_page(response.text, item)
            if not detail or (not detail["content"].strip() and not detail["attachments"]):
                raise ValueError("detail_body_missing")
            structure = None
            if options.get("structured_posts"):
                body = detail_soup.select_one("div.bdvTxt")
                if body is None:
                    raise ValueError("menu_body_missing")
                structure = extract_structure(body)
                detail["content"] = structure["content"]
            source_id = f"main:{page_id}:{post_no}"
            subcategory = board_post_subcategory(page_id)
            attachments = [build_attachment(
                index=index, name=entry["name"], url=entry["url"], project_root=PROJECT_ROOT,
            ) for index, entry in enumerate(detail["attachments"], start=1)]
            doc = apply_common_schema(
                {"url": post_url, "title": detail["title"], "category": ROUTES[page_id].category_path[0],
                 "subcategory": subcategory, "content": detail["content"],
                 "attachments": attachments,
                 **({"language": options["language"]} if options.get("language") else {}),
                 **({"structured_content": structure} if structure else {})},
                source_dataset="pknu_student_life", source_id=source_id,
                source_site=BASE_URL, document_type="notice", content_source="pknu_main_board_html",
                published_at=detail["date"], author=detail["author"],
                metadata={"page_id": page_id, "bbs_id": result["bbs_id"], "post_no": post_no},
            )
            slug = doc["slug"]
            saved_doc = PATHS.document_json(subcategory, slug, kind="posts")
            existing_doc = None
            if saved_doc.is_file():
                try:
                    existing_doc = json.loads(saved_doc.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    pass
            doc["attachments"] = cms_board.save_attachments(
                session, detail["attachments"], subcategory, slug, post_url,
                existing_doc=existing_doc, reuse_existing=not full_resync,
                file_dir=PATHS.attachment_dir(subcategory, slug),
            )
            if (existing_doc and existing_doc.get("content_hash") == doc["content_hash"]
                    and existing_doc.get("attachments") == doc["attachments"]
                    and not full_resync):
                stats.unchanged += 1
                outcome = "unchanged"
            else:
                save_json(doc, subcategory, slug, raw_html=response.text)
                outcome = "updated" if existing_doc else "new"
                setattr(stats, outcome, getattr(stats, outcome) + 1)
            state["items"][source_id] = {"slug": slug, "content_hash": doc["content_hash"],
                                         "url": post_url, "last_seen_at": now_kst(),
                                         "subcategory": subcategory}
            stats.count_attachments(doc["attachments"])
            result["documents"].append({"post_no": post_no, "status": outcome,
                                         "slug": slug,
                                         "output": saved_doc.relative_to(PROJECT_ROOT).as_posix(),
                                         "attachments": len(doc["attachments"]),
                                         **({"table_count": structure["table_count"]}
                                            if structure else {})})
            log_event(log, logging.INFO, "document_saved" if outcome != "unchanged" else "document_unchanged",
                      source_id=source_id, url=post_url, status=outcome)
        except (requests.RequestException, ValueError, OSError) as exc:
            stats.failed += 1
            result["documents"].append({"post_no": post_no, "status": "failed", "reason": str(exc)})
            log_event(log, logging.ERROR, "document_failed", source_id=f"main:{page_id}:{post_no}",
                      url=post_url, error=exc)
    result["status"] = "partial_success" if stats.failed else "success"
    summary_path = page_json(PROJECT_ROOT, page_id)
    result["output"] = summary_path.relative_to(PROJECT_ROOT).as_posix()
    write_json(summary_path, result)
    return stats, result
