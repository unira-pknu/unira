"""
부경대학교 본교 공지사항 크롤러
https://www.pknu.ac.kr/main/163

저장 형식:
  - files/pknu_main/커뮤니티/공지사항/<category>/json/posts/<slug>.json
  - files/pknu_main/커뮤니티/공지사항/<category>/html/<slug>.html
  - files/pknu_main/커뮤니티/공지사항/<category>/files/<slug>/<format>/<attachment>
  - files/pknu_main/_archive/pknu_notice/deleted/<category>/<slug>.json

실행:
  - python scripts/crawlers/pknu_notice.py
  - python scripts/crawlers/pknu_notice.py --full-resync
  - python scripts/crawlers/pknu_notice.py --recent-only 5
  - python scripts/crawlers/pknu_notice.py --reset-state
"""

from __future__ import annotations
from bs4 import BeautifulSoup
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
import sys

# Run as a file (`python scripts/crawlers/pknu_notice.py`, as the daily crawl
# workflow does), sys.path[0] is scripts/crawlers, so the project root must be
# added before the `scripts.*` imports below. Keep this above them: when import
# sorting moved it below (PR #48), the daily crawl failed 10-24~10-26.
PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.crawlers.common.logging import (  # noqa: E402
    configure_crawler_logging,
    log_attachment_events,
    log_event,
    set_run_id,
)
from scripts.crawlers.common.reader import remove_redundant_legacy_fields
from scripts.crawlers.common.schema import (
    CrawlStats,
    RunResult,
    apply_common_schema,
    attachment_error,
    build_attachment,
    document_slug,
    log_run_result,
    normalize_attachment,
    now_kst,
    sanitize_attachment_filename,
    unique_attachment_filename,
)
from scripts.crawlers.common.storage import (
    archive_document,
    empty_state,
    find_document_json,
    get_dataset_paths,
    load_state_with_migration,
    save_state_atomic,
    write_document,
)
from scripts.crawlers.main.common.attachments import (
    extract_filename,
    local_path_exists,
    reusable_attachments_by_url,
    sanitize_filename,
    save_attachments as _save_attachments,
)
from scripts.crawlers.main.common.board import (
    ListItem,
    extract_body_text,
    parse_detail_page,
    parse_list_page,
    parse_page_indicator,
)
from scripts.crawlers.main.common.http import LegacySSLAdapter as _LegacySSLAdapter
from typing import Any
from urllib.parse import unquote, urljoin, urlparse
import argparse
import hashlib
import json
import logging
import re
import requests
import time
import urllib3


urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


log, log_context = configure_crawler_logging("pknu_notice", PROJECT_ROOT)

BASE_URL = "https://www.pknu.ac.kr"
LIST_URL = f"{BASE_URL}/main/163"
SOURCE_SITE = BASE_URL

PATHS = get_dataset_paths(PROJECT_ROOT, "pknu_notice")
OUTPUT_JSON = PATHS.json
OUTPUT_HTML = PATHS.html
OUTPUT_FILES = PATHS.files
OUTPUT_DELETED = PATHS.deleted
STATE_FILE = PATHS.state
LEGACY_STATE_FILES = (PROJECT_ROOT / "state_pknu_notice.json",)

REQUEST_DELAY = 1.2
LIST_DELAY = 0.8
REQUEST_TIMEOUT = 25
INCREMENTAL_MAX_PAGES = 3
ORPHAN_MISS_THRESHOLD = 3
# Most list pages one incremental run may read per category (see
# collect_list_items); the limit keeps a long outage from turning into a full
# re-crawl inside the daily workflow's 60-minute budget.
GAP_FILL_MAX_PAGES = 20

SUBCATEGORY = "부경대 공지사항"
DOC_TYPE = "notice"

CD_TO_LABEL: dict[str, str] = {
    "10001": "공지사항",
    "10002": "비교과안내",
    "10003": "학사안내",
    "10004": "등록·장학",
    "10007": "초빙·채용",
}

CD_ORDER = ["10001", "10002", "10003", "10004", "10007"]


def save_attachments(session, attachments, category_folder, slug, source_page_url,
                     existing_doc=None, reuse_existing=True, file_dir=None):
    return _save_attachments(session, attachments, category_folder, slug, source_page_url,
                             existing_doc, reuse_existing,
                             file_dir=file_dir or ensure_file_dir(category_folder, slug))


def build_session() -> requests.Session:
    session = requests.Session()
    session.trust_env = False
    session.verify = False
    adapter = _LegacySSLAdapter()
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8",
            "Referer": LIST_URL,
        }
    )
    return session


def load_state() -> dict[str, Any]:
    state, origin = load_state_with_migration(
        PATHS, legacy_paths=LEGACY_STATE_FILES, legacy_kind="posts"
    )
    log_event(log, logging.INFO, "state_loaded", path=STATE_FILE, entries=len(state["items"]), origin=origin)
    return state


def save_state(state: dict[str, Any]) -> None:
    save_state_atomic(STATE_FILE, state, "pknu_notice")
    log_event(log, logging.INFO, "state_saved", path=STATE_FILE, entries=len(state["items"]))


def make_slug(url: str, extra: str = "") -> str:
    return hashlib.md5(f"{url}|{extra}".encode()).hexdigest()[:12]


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def category_dir_name(label: str) -> str:
    return label.replace("·", "_").replace(" ", "_")


def primary_category(labels: list[str]) -> str:
    order = {label: i for i, label in enumerate(CD_TO_LABEL.values())}
    return sorted(labels, key=lambda x: order.get(x, 999))[0]


def fetch(
    session: requests.Session,
    url: str,
    params: dict[str, str] | None = None,
    delay: float = REQUEST_DELAY,
) -> requests.Response | None:
    log_event(log, logging.INFO, "request_started", url=url, method="GET", params=params)
    time.sleep(delay)
    try:
        resp = session.get(url, params=params, timeout=REQUEST_TIMEOUT, verify=False)
        resp.encoding = "utf-8"
        if resp.status_code == 200:
            return resp
        log_event(log, logging.ERROR, "request_failed", url=url, status_code=resp.status_code, retryable=False)
    except requests.RequestException as exc:
        log_event(log, logging.ERROR, "request_failed", url=url, error=exc, retryable=True)
    return None


def merge_list_item(index: dict[str, ListItem], item: ListItem) -> None:
    if item.no not in index:
        index[item.no] = item
        return
    existing = index[item.no]
    existing.pknu_cd.update(item.pknu_cd)
    existing.categories.update(item.categories)
    if item.is_notice:
        existing.is_notice = True
    if item.list_date and not existing.list_date:
        existing.list_date = item.list_date
    if item.notice_no is not None and existing.notice_no is None:
        existing.notice_no = item.notice_no


def collect_list_items(
    session: requests.Session,
    cds: list[str],
    max_pages: int | None,
    full_resync: bool,
    newest_known_no: dict[str, int] | None = None,
    *, all_pages: bool = False,
) -> tuple[dict[str, ListItem], dict[str, int]]:
    """List items of the first `max_pages` pages per category, plus each
    category's oldest regular (non-pinned) post number seen.

    A page-limited run keeps paging past `max_pages` (up to GAP_FILL_MAX_PAGES)
    while a page's oldest post is still newer than the newest post the state
    knows for that category: with a fixed 3 pages, a busy day or a few failed
    daily runs (2026-10-24~26) push new posts past page 3 and they were never
    collected. Post numbers, not state membership, decide what is new: the
    state only keeps recent posts, so "not in state" would page to the cap.
    """
    index: dict[str, ListItem] = {}
    windows: dict[str, int] = {}
    list_params_base = {
        "bbsId": "2",
        "searchCondition": "",
        "searchKeyword": "",
    }

    for cd in cds:
        label = CD_TO_LABEL[cd]
        log_event(log, logging.INFO, "section_started", section=label, category_code=cd)
        log.info("[%s %s] 목록 수집 시작", cd, label)

        first = fetch(
            session,
            LIST_URL,
            params={**list_params_base, "cd": cd, "pageIndex": "1"},
            delay=LIST_DELAY,
        )
        if first is None:
            if all_pages:
                raise RuntimeError(f"notice_list_incomplete: cd={cd}, page=1")
            continue

        _, total_pages = parse_page_indicator(first.text)
        if max_pages is not None:
            last_page = min(max_pages, total_pages)
        elif full_resync or all_pages:
            last_page = total_pages
        else:
            last_page = min(INCREMENTAL_MAX_PAGES, total_pages)

        def take(page_items: list[ListItem]) -> None:
            for item in page_items:
                merge_list_item(index, item)
            # The bottom row is the page's oldest regular post (pinned posts sit
            # on top). ListItem.is_notice can't tell them apart here: on this
            # board every row parses as is_notice=True (checked 2026-10-26).
            if page_items and page_items[-1].no.isdigit():
                oldest = int(page_items[-1].no)
                windows[label] = min(windows.get(label, oldest), oldest)

        log.info("[%s %s] 페이지 1/%d (전체 %d페이지)", cd, label, 1, total_pages)
        page_items = parse_list_page(first.text, cd, label)
        take(page_items)
        log_event(log, logging.INFO, "list_fetched", section=label, page=1, total_pages=total_pages)

        # Gap fill only when the state knows this category; a first run (empty
        # state) reads just `max_pages`.
        newest = (newest_known_no or {}).get(label)
        gap_fill = max_pages is not None and not full_resync and not all_pages and newest is not None
        page_cap = min(total_pages, max(last_page, GAP_FILL_MAX_PAGES) if gap_fill else last_page)
        page = 1
        while page < page_cap:
            bottom = page_items[-1].no if page_items else ""
            if page >= last_page and not (bottom.isdigit() and newest is not None and int(bottom) > newest):
                break
            page += 1
            log.info("[%s %s] 페이지 %d/%d 처리 중...", cd, label, page, total_pages)
            resp = fetch(
                session,
                LIST_URL,
                params={**list_params_base, "cd": cd, "pageIndex": str(page)},
                delay=LIST_DELAY,
            )
            if resp is None:
                if all_pages:
                    raise RuntimeError(f"notice_list_incomplete: cd={cd}, page={page}/{last_page}")
                break
            page_items = parse_list_page(resp.text, cd, label)
            if not page_items:
                if all_pages:
                    raise RuntimeError(f"notice_list_empty: cd={cd}, page={page}/{last_page}")
                break
            take(page_items)
            log_event(log, logging.INFO, "list_fetched", section=label, page=page, total_pages=total_pages)
        if page > last_page:
            log.info("[%s %s] 처음 보는 글이 이어져 %d페이지까지 추가 수집", cd, label, page)
        log_event(log, logging.INFO, "section_finished", section=label, discovered=len(index))
    return index, windows


def json_path_for(category_folder: str, slug: str) -> Path:
    return PATHS.document_json(category_folder, slug)


def html_path_for(category_folder: str, slug: str) -> Path:
    return PATHS.document_html(category_folder, slug)


def ensure_file_dir(category_folder: str, slug: str) -> Path:
    file_dir = PATHS.attachment_dir(category_folder, slug)
    file_dir.mkdir(parents=True, exist_ok=True)
    return file_dir


def find_existing_doc(slug: str) -> tuple[Path | None, dict | None]:
    path = find_document_json(PATHS, slug)
    if path is None:
        return None, None
    try:
        return path, json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None, None


def save_document(doc: dict[str, Any], raw_html: str) -> None:
    folder = category_dir_name(doc["category"])
    write_document(PATHS, doc, folder, doc["slug"], raw_html)


def move_to_deleted(json_path: Path, doc: dict[str, Any]) -> None:
    folder = category_dir_name(doc.get("category", "unknown"))
    slug = doc["slug"]
    moved = archive_document(PATHS, folder, slug)
    log.info("orphan 이동 → deleted/%s/%s.json", folder, slug)
    log_event(log, logging.INFO, "document_deleted", source_id=doc.get("source_id"), url=doc.get("url"), slug=slug, moved=moved)


def build_document(
    list_item: ListItem,
    detail: dict[str, Any],
) -> dict[str, Any]:
    categories = sorted(
        list_item.categories,
        key=lambda x: list(CD_TO_LABEL.values()).index(x) if x in CD_TO_LABEL.values() else 99,
    )
    pknu_cd = sorted(list_item.pknu_cd, key=lambda x: CD_ORDER.index(x) if x in CD_ORDER else 99)
    post_url = f"{LIST_URL}?action=view&no={list_item.no}"
    title = detail["title"]
    content = detail["content"]

    source_id = list_item.no
    doc = {
        "slug": document_slug("pknu_notice", source_id),
        "no": int(list_item.no),
        "notice_no": list_item.notice_no,
        "title": title,
        "date": detail["date"],
        "url": post_url,
        "is_notice": list_item.is_notice,
        "categories": categories,
        "pknu_cd": pknu_cd,
        "category": primary_category(categories),
        "subcategory": SUBCATEGORY,
        "type": DOC_TYPE,
        "author": detail["author"],
        "content": content,
        "content_hash": content_hash(content),
        "attachments": detail["attachments"],
        "source_site": SOURCE_SITE,
        "crawled_at": datetime.now().isoformat(),
    }
    result = apply_common_schema(
        doc,
        source_dataset="pknu_notice",
        source_id=source_id,
        source_site=SOURCE_SITE,
        document_type="notice",
        content_source="pknu_main_notice_html",
        published_at=detail["date"],
        author=detail["author"],
        metadata={
            "no": int(list_item.no),
            "notice_no": list_item.notice_no,
            "is_notice": list_item.is_notice,
            "categories": categories,
            "pknu_cd": pknu_cd,
        },
        crawled_at=doc["crawled_at"],
    )
    remove_redundant_legacy_fields(result)
    return result


def merge_doc_categories(existing: dict[str, Any], new_doc: dict[str, Any]) -> dict[str, Any]:
    merged_cats = sorted(
        set(existing.get("categories", [])) | set(new_doc.get("categories", [])),
        key=lambda x: list(CD_TO_LABEL.values()).index(x) if x in CD_TO_LABEL.values() else 99,
    )
    merged_cd = sorted(
        set(existing.get("pknu_cd", [])) | set(new_doc.get("pknu_cd", [])),
        key=lambda x: CD_ORDER.index(x) if x in CD_ORDER else 99,
    )
    new_doc["categories"] = merged_cats
    new_doc["pknu_cd"] = merged_cd
    new_doc["category"] = primary_category(merged_cats)
    metadata = new_doc.setdefault("metadata", {})
    metadata["categories"] = merged_cats
    metadata["pknu_cd"] = merged_cd
    return new_doc


def crawl_details(
    session: requests.Session,
    index: dict[str, ListItem],
    state: dict[str, Any],
    full_resync: bool,
) -> tuple[CrawlStats, set[str]]:
    stats = CrawlStats()
    seen_slugs: set[str] = set()
    posts_state: dict[str, Any] = state.setdefault("items", {})

    total = len(index)
    stats.discovered = total
    for i, (post_no, list_item) in enumerate(index.items(), start=1):
        post_url = f"{LIST_URL}?action=view&no={post_no}"
        log_event(log, logging.DEBUG, "document_discovered", source_id=post_no, url=post_url)
        if i % 20 == 0 or i == 1:
            log.info("상세 크롤링 %d/%d (no=%s)", i, total, post_no)

        stats.requested += 1
        resp = fetch(session, post_url, delay=REQUEST_DELAY)
        if resp is None:
            stats.failed += 1
            log_event(log, logging.ERROR, "document_failed", source_id=post_no, url=post_url, stage="request")
            continue

        detail = parse_detail_page(resp.text, list_item)
        if detail is None:
            log.warning("상세 파싱 실패 no=%s", post_no)
            stats.failed += 1
            log_event(log, logging.ERROR, "document_failed", source_id=post_no, url=post_url, stage="parse")
            continue

        doc = build_document(list_item, detail)
        slug = doc["slug"]
        seen_slugs.add(slug)

        existing_path, existing_doc = find_existing_doc(slug)
        if existing_doc:
            doc = merge_doc_categories(existing_doc, doc)
            old_hash = existing_doc.get("content_hash", "")
            save_folder = category_dir_name(doc["category"])
            doc["attachments"] = save_attachments(
                session=session,
                attachments=doc.get("attachments", []),
                category_folder=save_folder,
                slug=slug,
                source_page_url=post_url,
                existing_doc=existing_doc,
                reuse_existing=not full_resync,
            )
            stats.count_attachments(doc.get("attachments"))
            if not full_resync and old_hash == doc["content_hash"]:
                # 메타(카테고리)만 갱신된 경우 저장
                if (
                    set(existing_doc.get("categories", [])) == set(doc["categories"])
                    and set(existing_doc.get("pknu_cd", [])) == set(doc["pknu_cd"])
                    and existing_doc.get("attachments", []) == doc.get("attachments", [])
                ):
                    stats.unchanged += 1
                    log_attachment_events(log, doc.get("attachments"), source_id=post_no)
                    log_event(log, logging.INFO, "document_unchanged", source_id=post_no, url=post_url, status="unchanged")
                    posts_state[post_no] = {
                        "slug": slug,
                        "category": existing_doc.get("category", doc["category"]),
                        "miss_count": 0,
                        "last_seen_at": now_kst(),
                    }
                    continue
                doc["content_hash"] = old_hash if old_hash else doc["content_hash"]

        save_folder = category_dir_name(doc["category"])
        if not existing_doc:
            doc["attachments"] = save_attachments(
                session=session,
                attachments=doc.get("attachments", []),
                category_folder=save_folder,
                slug=slug,
                source_page_url=post_url,
            )
            stats.count_attachments(doc.get("attachments"))
        was_existing = bool(existing_path and existing_path.exists())
        if existing_path and existing_path != PATHS.document_json(save_folder, slug):
            # 첫 카테고리 폴더가 바뀐 경우: 기존 파일 제거 후 새 경로에 저장
            try:
                existing_path.unlink(missing_ok=True)
                old_html = PATHS.document_html(category_dir_name(existing_doc["category"]), slug)
                if old_html.exists():
                    old_html.unlink()
            except OSError as exc:
                log.warning("기존 파일 삭제 실패 %s: %s", existing_path, exc)

        save_document(doc, resp.text)
        outcome = "updated" if was_existing else "new"
        log_attachment_events(log, doc.get("attachments"), source_id=post_no)
        log_event(log, logging.INFO, "document_saved", source_id=post_no, url=post_url, status=outcome)
        if was_existing:
            stats.updated += 1
        else:
            stats.new += 1

        posts_state[post_no] = {
            "slug": slug,
            "category": doc["category"],
            "miss_count": 0,
            "last_seen_at": now_kst(),
        }

    return stats, seen_slugs


def process_orphans(
    state: dict[str, Any],
    seen_slugs: set[str],
    full_resync: bool,
    windows: dict[str, int] | None = None,
) -> int:
    """Archive posts that vanished from the board.

    A post only counts as missing when it should have been on the pages this
    run read: its number is at least its category's oldest regular post seen
    (`windows`, from collect_list_items). Without that check every post that
    simply scrolled past page 3 was counted, and after ORPHAN_MISS_THRESHOLD
    runs the archive of older notices was moved to deleted/ (on 2026-10-26,
    172 of 400 state entries were one run away from it).
    """
    if full_resync:
        return 0

    posts_state: dict[str, Any] = state.setdefault("items", {})
    moved = 0
    for post_no, meta in list(posts_state.items()):
        slug = meta.get("slug", "")
        if not slug:
            continue
        if slug in seen_slugs:
            meta["miss_count"] = 0
            continue
        if windows is not None:
            window = windows.get(str(meta.get("category") or ""))
            if window is None or not str(post_no).isdigit() or int(post_no) < window:
                continue  # outside the pages read this run, not evidence of deletion

        meta["miss_count"] = int(meta.get("miss_count", 0)) + 1
        if meta["miss_count"] < ORPHAN_MISS_THRESHOLD:
            continue

        json_path, doc = find_existing_doc(slug)
        if json_path is None or doc is None:
            posts_state.pop(post_no, None)
            continue

        move_to_deleted(json_path, doc)
        posts_state.pop(post_no, None)
        moved += 1

    return moved


def run_crawl(
    full_resync: bool = False,
    recent_only: int | None = None,
    only_cd: str | None = None,
    *, all_pages: bool = False,
) -> CrawlStats:
    if all_pages and recent_only is not None:
        raise ValueError("all_pages cannot be combined with recent_only")
    cds = CD_ORDER.copy()
    if only_cd:
        if only_cd not in CD_TO_LABEL:
            raise ValueError(f"Unknown cd: {only_cd}")
        cds = [only_cd]

    max_pages = None
    if recent_only is not None:
        max_pages = recent_only
    elif not full_resync and not all_pages:
        max_pages = INCREMENTAL_MAX_PAGES

    session = build_session()
    state = load_state()

    log.info("=" * 60)
    log.info(
        "부경대 공지사항 크롤 시작 (full_resync=%s, max_pages=%s)",
        full_resync,
        max_pages if max_pages else "ALL",
    )

    newest_known_no: dict[str, int] = {}
    for post_no, meta in state.get("items", {}).items():
        label = str(meta.get("category") or "")
        if str(post_no).isdigit() and label:
            newest_known_no[label] = max(newest_known_no.get(label, 0), int(post_no))
    index, windows = collect_list_items(
        session, cds, max_pages, full_resync, newest_known_no=newest_known_no, all_pages=all_pages
    )
    log.info("목록 수집 완료: 고유 글 %d건", len(index))

    stats, seen_slugs = crawl_details(session, index, state, full_resync)
    # Archive collection must not retire existing documents outside its scope.
    stats.deleted = 0 if all_pages else process_orphans(state, seen_slugs, full_resync, windows)

    save_state(state)

    log.info("=" * 60)
    log.info(
        "완료: 신규=%d, 업데이트=%d, 스킵=%d, 삭제=%d, 오류=%d",
        stats.new,
        stats.updated,
        stats.skipped,
        stats.deleted,
        stats.failed,
    )
    log.info("=" * 60)
    return stats


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="부경대 본교 공지사항 크롤러")
    parser.add_argument(
        "--full-resync",
        action="store_true",
        help="전체 페이지 재수집",
    )
    parser.add_argument(
        "--recent-only",
        type=int,
        metavar="N",
        default=None,
        help="카테고리별 최근 N페이지만 목록 수집",
    )
    parser.add_argument(
        "--reset-state",
        action="store_true",
        help="files/pknu_main/_state/pknu_notice.json을 빈 공통 상태로 초기화",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="1회 실행 (스케줄러 없음, 기본 동작과 동일)",
    )
    parser.add_argument(
        "--only-cd",
        type=str,
        default=None,
        help="단일 카테고리만 수집 (예: 10001)",
    )
    return parser.parse_args()


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    args = parse_args()
    mode = "full" if args.full_resync else ("recent" if args.recent_only is not None else "incremental")
    result = RunResult(dataset="pknu_notice", mode=mode)
    set_run_id(log_context, result.run_id)
    log_event(log, logging.INFO, "run_started", mode=mode)
    try:
        if args.reset_state:
            save_state_atomic(STATE_FILE, empty_state("pknu_notice"), "pknu_notice")
            log_event(log, logging.INFO, "state_saved", path=STATE_FILE, action="reset")
        result.stats = run_crawl(
            full_resync=args.full_resync,
            recent_only=args.recent_only,
            only_cd=args.only_cd,
        )
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


if __name__ == "__main__":
    raise SystemExit(main())
