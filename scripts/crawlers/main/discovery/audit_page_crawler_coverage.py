"""Replay saved /main/<id> responses through existing parsers without crawler state writes.

This is a list/static-page compatibility audit, not a live crawl of detail pages,
attachments, dynamic APIs, or redirect destinations.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from contextlib import redirect_stderr
from dataclasses import asdict
from datetime import datetime, timezone
import io
import json
import logging
from pathlib import Path
import re
from unittest.mock import patch
from unittest.mock import MagicMock
from urllib.parse import parse_qs, urlsplit

from scripts.crawlers.departments.adapters.numeric_cms import NumericCMSAdapter
from scripts.crawlers.main.collectors.adapter import MainListParserMismatch, PknuMainAdapter
from scripts.crawlers.pknu_notice import parse_list_page
from scripts.crawlers.main.collectors import static_pages as static_crawler, guides
from scripts.crawlers.main import routes
from scripts.crawlers.main.discovery.inventory_pages import DEFAULT_OUTPUT, inventory_soup
from scripts.crawlers.main.routes import (ACADEMIC_CALENDAR_PAGE_IDS, MAJOR_PROGRAM_PAGE_IDS,
                                 TUITION_PAGE_IDS, ORG_PAGE_IDS)


ISSUE_TEXT = {
    "EMPTY_HTTP_200": "HTTP 200이지만 응답이 비어 있어 존재와 본문을 확인할 수 없음",
    "ACCESS_DENIED_OR_BAD_ROUTE": "접근 제한 안내. 실제 권한 제한인지 잘못된 경로인지는 미확인",
    "SOURCE_REQUEST_FAILED": "원본 응답 수집 실패 또는 HTTP 오류",
    "UNTESTED_REDIRECT_DESTINATION": "이동 목적지의 내용과 접근 가능성을 검사하지 않음",
    "INVALID_REDIRECT_DESTINATION": "이동 목적지가 /main/null이거나 유효하지 않음",
    "BINARY_RESPONSE": "HTML 대신 파일 응답. 파일 내용과 크롤러 경로 미검증",
    "BOARD_LIST_PARSER_MISMATCH": "상세 링크 후보가 있지만 본교 어댑터의 목록 파싱 결과가 0건",
    "BOARD_LIST_COUNT_MISMATCH": "파싱된 목록 수와 확인된 상세 링크 수가 다름",
    "BOARD_NO_PUBLIC_ITEMS": "게시판 구조는 있지만 공개 상세 링크가 확인되지 않음",
    "NO_CONTENT_CONTAINER": "기존 정적 페이지 크롤러가 #subCont를 찾지 못함",
    "STATIC_BODY_MISSING": "기존 정적 페이지 크롤러 출력에 확인된 본문이 없음",
    "STATIC_TOO_SHORT": "기존 정적 페이지 크롤러의 80자 기준을 통과하지 못함",
    "NUMERIC_ADAPTER_NAV_ONLY": "학과 숫자 CMS 비교 파서가 메뉴만 읽었고 본교 어댑터도 정적 본문을 검증하지 못함",
    "TABLE_STRUCTURE_UNVERIFIED": "기존 정적 크롤러가 표의 행·열·병합 구조를 보존하지 않음",
    "IMAGE_OCR_NEEDED": "이미지 중심 본문이며 기존 정적 크롤러로 텍스트 추출 불가",
    "ATTACHMENT_NOT_TESTED": "첨부파일 후보의 실제 응답과 텍스트 추출은 검사하지 않음",
    "DYNAMIC_CONTENT_NOT_TESTED": "동적·삽입 콘텐츠의 실제 데이터 로딩은 검사하지 않음",
    "LINK_TARGETS_NOT_TESTED": "본문의 연결 대상은 별도 수집하지 않음",
    "HOMEPAGE_SPECIAL_CASE": "메인 화면은 정적 본문 크롤러의 대상이 아님",
    "DUPLICATE_CONTENT_CANDIDATE": "다른 숫자 경로와 본문이 같아 대표 경로 검토 필요",
    "PARSER_EXCEPTION": "기존 파서가 예외를 발생시킴",
    "CONFIGURED_TARGET_WRONG_TYPE": "기존 정적 페이지 대상이지만 실제 응답은 게시판 등 다른 유형",
    "CONFIGURED_TARGET_UNAVAILABLE": "기존 크롤러 설정 대상이지만 현재 경로에서 HTML 본문을 얻을 수 없음",
    "NAV_PADDED_SHORT_BODY": "실제 본문은 80자 미만인데 내비게이션이 더해져 길이 기준을 통과할 위험",
    "GUIDE_FILES_NOT_TESTED": "대학생활 가이드의 PDF 후보만 발견했고 미디어 해석·다운로드는 검사하지 않음",
    "LEGACY_STATIC_FALSE_SUCCESS": "기존 정적 크롤러가 게시판 목록·이미지·내비게이션을 본문으로 잘못 저장할 수 있음",
}

HARD_ERROR = {"INVALID_REDIRECT_DESTINATION", "BOARD_LIST_PARSER_MISMATCH",
              "BOARD_LIST_COUNT_MISMATCH", "STATIC_BODY_MISSING", "PARSER_EXCEPTION",
              "CONFIGURED_TARGET_WRONG_TYPE"}
SOURCE_PROBLEM = {"EMPTY_HTTP_200", "ACCESS_DENIED_OR_BAD_ROUTE", "SOURCE_REQUEST_FAILED"}


class CachedResponse:
    def __init__(self, text: str):
        self.text = text

    def raise_for_status(self) -> None:
        return None


def normalized(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def contains_body_anchor(output: str, summary: str) -> bool | None:
    excerpt = normalized(summary)
    if not excerpt:
        return None
    # Compare a contiguous body excerpt. A long navigation menu cannot pass.
    anchor = excerpt[: min(28, len(excerpt))]
    return anchor in normalized(output)


def observed_detail_count(row: dict) -> int:
    urls = set()
    for item in row.get("content_links", []):
        query = parse_qs(urlsplit(item["url"]).query)
        if query.get("no"):
            urls.add(item["url"])
    return len(urls)


def test_page(catalog: dict, inventory: dict, folder: Path, adapter: NumericCMSAdapter,
              main_adapter: PknuMainAdapter,
              compact_cache: dict[int, str]) -> dict:
    page_id = catalog["page_id"]
    configured = (["pknu_notice.list"] if page_id == 163 else [])
    if page_id in routes.STATIC_PAGE_IDS:
        configured.append("main.static")
    if page_id in routes.BOARD_PAGE_IDS:
        configured.append("main.board")
    if page_id in routes.LINK_HUB_PAGE_IDS:
        configured.append("main.link_hub")
    if page_id in routes.REDIRECT_PAGE_TARGETS:
        configured.append("main.redirect")
    if page_id in routes.FILE_PAGE_IDS:
        configured.append("main.file")
    if page_id in TUITION_PAGE_IDS:
        configured.append("main.tuition")
    if page_id in ORG_PAGE_IDS:
        configured.append("main.organization")
    if page_id in ACADEMIC_CALENDAR_PAGE_IDS:
        configured.append("main.academic_calendar")
    if page_id in MAJOR_PROGRAM_PAGE_IDS:
        configured.append("main.major_program")
    if page_id == 434:
        configured.append("main.guide")
    result = {
        "page_id": page_id, "url": catalog["url"], "title": catalog["title"],
        "page_type": catalog["page_type"], "source_status": catalog["status"],
        "outcome": "needs_review", "configured_routes": configured,
        "configured_result": "not_configured" if not configured else "not_tested",
        "issue_codes": [], "parser_results": {},
    }
    codes = result["issue_codes"]
    status, kind = catalog["status"], catalog["page_type"]
    if status == "empty_response":
        codes.append("EMPTY_HTTP_200")
    elif status == "access_blocked":
        codes.append("ACCESS_DENIED_OR_BAD_ROUTE")
    elif status not in {"exists", "link_only"}:
        codes.append("SOURCE_REQUEST_FAILED")
    elif kind == "redirect":
        destination = catalog["crawler_hints"].get("destination_url") or ""
        codes.append("INVALID_REDIRECT_DESTINATION" if not destination or "/main/null" in destination
                     else "UNTESTED_REDIRECT_DESTINATION")
        if "main.redirect" in configured:
            result["configured_result"] = "destination_unverified_offline"
    elif kind == "file_endpoint":
        codes.append("BINARY_RESPONSE")
        if "main.file" in configured:
            result["configured_result"] = "file_unverified_offline"
    else:
        raw_path = inventory.get("raw_path")
        path = folder / raw_path if raw_path else None
        if not path or not path.is_file():
            codes.append("SOURCE_REQUEST_FAILED")
        else:
            raw = path.read_text(encoding="utf-8")
            soup = inventory_soup(raw)
            compact = str(soup)
            compact_cache[page_id] = compact
            try:
                section = adapter.analyze_section(name=catalog["title"], page_url=catalog["url"], html=compact)
                result["parser_results"]["numeric_section"] = {
                    "kind": section["kind"], "status": section["status"],
                    "bbs_id": section.get("bbs_id"), "confidence": section["confidence"],
                }
                main_section = main_adapter.analyze_section(
                    name=catalog["title"], page_url=catalog["url"], html=compact)
                result["parser_results"]["main_section"] = {
                    "kind": main_section["kind"], "status": main_section["status"],
                    "detail_link_candidates": main_section["detail_link_candidates"],
                }
                if kind == "board":
                    candidates = max(observed_detail_count(inventory),
                                     main_adapter.count_list_candidates(soup, catalog["url"]))
                    try:
                        main_items = main_adapter.parse_list(soup, catalog["url"])
                    except MainListParserMismatch as exc:
                        main_items = []
                        result["parser_results"]["main_list_error"] = str(exc)
                    numeric = adapter.parse_list(soup, catalog["url"])
                    notice = parse_list_page(compact, "10001", "공지사항")
                    result["parser_results"].update(
                        observed_detail_links=candidates, main_list_items=len(main_items),
                        numeric_list_items=len(numeric),
                        notice_list_items=len(notice))
                    if page_id in {193, 349, 355, 356}:
                        result["parser_results"]["main_detail_urls"] = [item["post_url"] for item in main_items]
                    if candidates and not main_items:
                        codes.append("BOARD_LIST_PARSER_MISMATCH")
                    elif candidates and candidates != len(main_items):
                        codes.append("BOARD_LIST_COUNT_MISMATCH")
                    elif not candidates:
                        codes.append("BOARD_NO_PUBLIC_ITEMS")
                    if main_items and candidates == len(main_items):
                        result["outcome"] = "list_parsed"
                    if page_id == 163:
                        result["configured_result"] = "list_parsed_detail_untested" if main_items else "failed"
                    elif "main.board" in configured:
                        result["configured_result"] = "list_parsed_detail_untested" if main_items else "failed"
                else:
                    # Use the existing student-life fetch_static_page code with a saved
                    # HTML response and no network call or production state mutation.
                    with patch.object(static_crawler, "fetch", return_value=CachedResponse(compact)):
                        parsed = static_crawler.fetch_static_page(None, page_id)
                    student_text = parsed.content
                    student_anchor = contains_body_anchor(student_text, inventory.get("content_summary", ""))
                    result["parser_results"]["student_life_static"] = {
                        "text_length": len(student_text), "body_excerpt_found": student_anchor,
                        "status": parsed.status, "reason": parsed.reason,
                        "warnings": list(parsed.warnings),
                        "passes_80_char_rule": parsed.status == "ready",
                    }
                    main_static = main_adapter.parse_static(soup, fallback_title=catalog["title"])
                    main_anchor = contains_body_anchor(main_static["content"], inventory.get("content_summary", ""))
                    result["parser_results"]["main_static"] = {
                        "text_length": len(main_static["content"]), "body_excerpt_found": main_anchor,
                        "status": main_static["status"], "reason": main_static["reason"],
                    }
                    if kind == "homepage":
                        codes.append("HOMEPAGE_SPECIAL_CASE")
                    elif kind == "image_only":
                        codes.append("IMAGE_OCR_NEEDED")
                    elif kind == "attachment_only":
                        codes.append("ATTACHMENT_NOT_TESTED")
                    elif kind == "embedded_content":
                        codes.append("DYNAMIC_CONTENT_NOT_TESTED")
                    elif kind == "link_hub":
                        codes.append("LINK_TARGETS_NOT_TESTED")
                    elif parsed.reason == "no_subcont":
                        codes.append("NO_CONTENT_CONTAINER")
                    elif student_anchor is False:
                        codes.append("STATIC_BODY_MISSING")
                    elif parsed.reason == "content_too_short":
                        codes.append("STATIC_TOO_SHORT")
                    elif parsed.status == "ready":
                        result["outcome"] = "static_parsed"
                    if kind == "table_page":
                        codes.append("TABLE_STRUCTURE_UNVERIFIED")
                    if catalog["crawler_hints"].get("attachment_candidates") and "ATTACHMENT_NOT_TESTED" not in codes:
                        codes.append("ATTACHMENT_NOT_TESTED")
                    if "DYNAMIC_OR_EMBEDDED_CONTENT_NOT_FETCHED" in inventory.get("warnings", []) and "DYNAMIC_CONTENT_NOT_TESTED" not in codes:
                        codes.append("DYNAMIC_CONTENT_NOT_TESTED")
                    # The numeric department parser uses .container/#container
                    # rather than #subCont. Check its actual extracted text, not
                    # just the section's optimistic candidate status.
                    if inventory.get("text_length", 0) > 0 and soup.select_one("#subCont"):
                        with redirect_stderr(io.StringIO()):
                            numeric_static = adapter.parse_static(soup, fallback_title=catalog["title"])
                        numeric_anchor = contains_body_anchor(numeric_static["content"], inventory["content_summary"])
                        result["parser_results"]["numeric_static"] = {
                            "text_length": len(numeric_static["content"]),
                            "body_excerpt_found": numeric_anchor,
                        }
                        if numeric_anchor is False and not (
                            main_anchor is True and main_static["status"] == "ready"
                        ):
                            codes.append("NUMERIC_ADAPTER_NAV_ONLY")
                    if "main.static" in configured:
                        if parsed.status != "ready" or student_anchor is False:
                            result["configured_result"] = "failed"
                        else:
                            result["configured_result"] = "text_parsed_structure_unverified" if kind == "table_page" else "text_parsed"
                    if "main.link_hub" in configured:
                        result["configured_result"] = "links_identified_targets_untested" if catalog["crawler_hints"].get("content_link_examples") else "failed"
                    if page_id == 434:
                        with patch.object(guides, "resolve_media_pdf_url",
                                          side_effect=lambda _session, media_id: f"https://www.pknu.ac.kr/unverified/{media_id}.pdf"):
                            guide_items = guides.parse_guide_items_from_html(None, compact)
                        result["parser_results"]["guide_media_candidates"] = len(guide_items)
                        result["configured_result"] = "pdf_candidates_only" if guide_items else "failed"
                        codes.append("GUIDE_FILES_NOT_TESTED")
            except Exception as exc:
                result["parser_results"]["exception"] = f"{type(exc).__name__}: {str(exc)[:200]}"
                codes.append("PARSER_EXCEPTION")
    if catalog["crawler_hints"].get("duplicate_content_of_page_id") is not None:
        codes.append("DUPLICATE_CONTENT_CANDIDATE")
    if "main.static" in configured:
        if status == "exists" and kind in {"board", "link_hub", "attachment_only", "image_only", "embedded_content"}:
            result["configured_result"] = "wrong_page_type"
            codes.append("CONFIGURED_TARGET_WRONG_TYPE")
        elif result["configured_result"] == "not_tested":
            result["configured_result"] = "unavailable"
            codes.append("CONFIGURED_TARGET_UNAVAILABLE")
    if any(code in SOURCE_PROBLEM for code in codes):
        result["outcome"] = "source_unavailable"
    elif any(code in HARD_ERROR for code in codes):
        result["outcome"] = "parser_error"
    elif codes:
        result["outcome"] = "needs_review"
    return result


def replay_static_crawler(compact_cache: dict[int, str], pages: list[dict]) -> dict:
    """Run its actual all-page loop with in-memory responses, state, and writes."""
    saved: dict[int, dict] = {}

    def replay_fetch(_session, url, **_kwargs):
        match = re.search(r"/main/(\d+)(?:[?#]|$)", url)
        page_id = int(match.group(1)) if match else -1
        return CachedResponse(compact_cache.get(page_id, ""))

    def capture_save(doc, *_args, **_kwargs):
        match = re.search(r"/main/(\d+)(?:[?#]|$)", doc.get("url", ""))
        if match:
            saved[int(match.group(1))] = doc
        return Path("in-memory")

    with patch.object(static_crawler, "STATIC_PAGE_IDS", tuple(i for i in compact_cache if i in routes.STATIC_PAGE_IDS)), \
         patch.object(static_crawler, "fetch", side_effect=replay_fetch), \
         patch.object(static_crawler, "save_json", side_effect=capture_save), \
         patch.object(static_crawler, "log_event", return_value=None), \
         patch.object(static_crawler, "log", MagicMock()):
        stats = static_crawler.crawl_static_pages(None, {"items": {}}, full_resync=True)
    false_success = []
    for page in pages:
        page_id = page["page_id"]
        was_saved = page_id in saved
        page["static_replay"] = "saved" if was_saved else "skipped_or_failed"
        if not was_saved:
            continue
        static_info = page["parser_results"].get("student_life_static", {})
        if (page["page_type"] in {"board", "image_only", "attachment_only", "embedded_content"}
                or page["source_status"] != "exists"
                or static_info.get("body_excerpt_found") is False
                or "NAV_PADDED_SHORT_BODY" in page["issue_codes"]):
            false_success.append(page_id)
            page["issue_codes"].append("STATIC_FALSE_SUCCESS")
    return {"stats": asdict(stats), "saved_pages": len(saved),
            "saved_page_ids": sorted(saved), "false_success_page_ids": false_success,
            "saved_by_catalog_type": dict(Counter(page["page_type"] for page in pages if page["page_id"] in saved))}


def run(folder: Path, output: Path) -> dict:
    previous = json.loads(output.read_text(encoding="utf-8")) if output.is_file() else {}
    old_replay = previous.get("legacy_static_full_replay", {})
    baseline = previous.get("comparison", {}).get("baseline") or {
        "saved_page_ids": old_replay.get("saved_page_ids", []),
        "false_success_page_ids": old_replay.get("false_success_page_ids", []),
        "nav_padded_page_ids": previous.get("issue_groups", {}).get("NAV_PADDED_SHORT_BODY", {}).get("page_ids", []),
    }
    catalog = json.loads((folder / "page_catalog.json").read_text(encoding="utf-8"))
    inventory = {row["page_id"]: row for line in (folder / "page_inventory.jsonl").read_text(encoding="utf-8").splitlines()
                 if (row := json.loads(line))}
    adapter = NumericCMSAdapter()
    main_adapter = PknuMainAdapter()
    pages = []
    compact_cache: dict[int, str] = {}
    logging.disable(logging.WARNING)  # Existing attachment parser logs skipped nav links loudly.
    for page in catalog["pages"]:
        pages.append(test_page(page, inventory[page["page_id"]], folder,
                               adapter, main_adapter, compact_cache))
        if len(pages) % 50 == 0:
            print(json.dumps({"tested": len(pages), "total": len(catalog["pages"])}, ensure_ascii=False), flush=True)
    current_replay = replay_static_crawler(compact_cache, pages)
    current_saved = set(current_replay["saved_page_ids"])
    comparison = {
        "baseline": baseline,
        "prevented_false_success_page_ids": sorted(set(baseline["false_success_page_ids"]) - current_saved),
        "remaining_false_success_page_ids": sorted(set(baseline["false_success_page_ids"]) & current_saved),
        "prevented_nav_padded_page_ids": sorted(set(baseline["nav_padded_page_ids"]) - current_saved),
        "remaining_nav_padded_page_ids": sorted(set(baseline["nav_padded_page_ids"]) & current_saved),
    }
    groups: dict[str, list[int]] = defaultdict(list)
    for page in pages:
        for code in page["issue_codes"]:
            groups[code].append(page["page_id"])
    baseline_mismatch = previous.get("main_adapter_recovery", {}).get("baseline_board_mismatch_page_ids")
    if baseline_mismatch is None:
        baseline_mismatch = previous.get("issue_groups", {}).get("BOARD_LIST_PARSER_MISMATCH", {}).get("page_ids", [])
    remaining_mismatch = groups.get("BOARD_LIST_PARSER_MISMATCH", [])
    main_static_recovered = [page["page_id"] for page in pages
                             if page["parser_results"].get("numeric_static", {}).get("body_excerpt_found") is False
                             and page["parser_results"].get("main_static", {}).get("body_excerpt_found") is True
                             and page["parser_results"]["main_static"]["status"] == "ready"]
    target_ids = {193, 349, 355, 356}
    target_urls = [url for page in pages if page["page_id"] in target_ids
                   for url in page["parser_results"].get("main_detail_urls", [])]
    report = {
        "generated_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "mode": "offline_replay_of_saved_responses",
        "scope": "All 533 numeric main routes, one saved list/static response each. No detail-page, attachment, dynamic API, or redirect-destination fetch.",
        "caveat": "Parser compatibility and in-memory static-crawler replay are not a successful end-to-end network crawl. No production documents or crawler state were written.",
        "source_checked_at_range": catalog["checked_at_range"],
        "parsers": ["PknuMainAdapter.analyze_section/parse_list/parse_static",
                    "NumericCMSAdapter (comparison only)", "pknu_notice.parse_list_page (comparison only)",
                    "static_crawler.fetch_static_page"],
        "counts": {"pages": len(pages), "outcome": dict(Counter(page["outcome"] for page in pages)),
                   "configured_routes": sum(bool(page["configured_routes"]) for page in pages),
                   "configured_result": dict(Counter(page["configured_result"] for page in pages if page["configured_routes"])),
                   "issue_code": {code: len(ids) for code, ids in sorted(groups.items())}},
        "current_static_full_replay": current_replay,
        "comparison": comparison,
        "main_adapter_recovery": {
            "baseline_board_mismatch_page_ids": baseline_mismatch,
            "recovered_board_page_ids": sorted(set(baseline_mismatch) - set(remaining_mismatch)),
            "remaining_board_mismatch_page_ids": remaining_mismatch,
            "target_board_list_entries": len(target_urls),
            "target_unique_detail_urls": len(set(target_urls)),
            "numeric_nav_only_recovered_page_ids": main_static_recovered,
        },
        "configured_targets": [{key: page[key] for key in
                                ("page_id", "url", "title", "page_type", "configured_routes", "configured_result", "issue_codes")}
                               for page in pages if page["configured_routes"]],
        "priority_issues": {
            code: [{"page_id": page["page_id"], "url": page["url"], "title": page["title"]}
                   for page in pages if code in page["issue_codes"]]
            for code in ("BOARD_LIST_PARSER_MISMATCH", "CONFIGURED_TARGET_WRONG_TYPE",
                         "CONFIGURED_TARGET_UNAVAILABLE", "INVALID_REDIRECT_DESTINATION",
                         "EMPTY_HTTP_200", "ACCESS_DENIED_OR_BAD_ROUTE")
        },
        "issue_groups": {code: {"description": ISSUE_TEXT[code], "page_ids": ids}
                         for code, ids in sorted(groups.items())},
        "pages": pages,
    }
    part = output.with_suffix(output.suffix + ".part")
    part.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    part.replace(output)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    folder = args.input_dir.resolve()
    output = (args.output or folder / "crawler_coverage_report.json").resolve()
    summary = run(folder, output)
    print(json.dumps({"output": str(output), "counts": summary["counts"]}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
