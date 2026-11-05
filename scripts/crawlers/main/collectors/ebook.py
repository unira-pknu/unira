'Main website collectors ebook.'

from __future__ import annotations
from bs4 import BeautifulSoup
from scripts.crawlers.common.logging import log_event
from scripts.crawlers.common.reader import remove_redundant_legacy_fields
from scripts.crawlers.common.schema import (
    CrawlStats,
    apply_common_schema,
    content_sha256,
    document_slug,
    now_kst,
)
from scripts.crawlers.main.collectors.guides import GuideItem, process_guide_item
from scripts.crawlers.main.common.documents import make_slug
from scripts.crawlers.main.common.http import fetch
from scripts.crawlers.main.common.storage import (
    BASE_URL,
    EBOOK_BASE_URL,
    EBOOK_INDEX_URL,
    PATHS,
    SUBCATEGORY_ACADEMIC_INFO,
    log,
    save_json,
)
from typing import Any
from urllib.parse import urljoin
import json
import logging
import re
import requests


SUBCATEGORY_EBOOK = SUBCATEGORY_ACADEMIC_INFO


EBOOK_HTML_PARSER_VERSION = 2


def discover_ebook_pdf_url(session: requests.Session) -> str | None:
    candidates: list[str] = []

    paths = ["mobile/javascript/config.js"]
    for rel in paths:
        url = urljoin(EBOOK_BASE_URL, rel)
        resp = fetch(session, url)
        if resp.status_code != 200:
            continue
        text = resp.text
        for match in re.findall(r'["\']([^"\']*\.pdf)["\']', text, re.I):
            candidates.append(urljoin(EBOOK_BASE_URL, match.lstrip("/")))
        for match in re.findall(r"(https?://[^\s\"']+\.pdf)", text, re.I):
            candidates.append(match)

    try:
        idx_resp = fetch(session, EBOOK_INDEX_URL)
        if idx_resp.status_code == 200:
            for match in re.findall(r'["\']([^"\']*\.pdf)["\']', idx_resp.text, re.I):
                candidates.append(urljoin(EBOOK_BASE_URL, match.lstrip("/")))
    except Exception:
        pass

    for rel in [
        "files/col_life.pdf",
        "files/source.pdf",
        "download/col_life.pdf",
        "files/publication.pdf",
    ]:
        url = urljoin(EBOOK_BASE_URL, rel)
        try:
            head = session.head(url, timeout=15, allow_redirects=True, verify=False)
            if head.status_code == 200 and "pdf" in head.headers.get("Content-Type", "").lower():
                candidates.append(url)
        except requests.RequestException:
            pass

    seen: set[str] = set()
    for url in candidates:
        if url in seen:
            continue
        seen.add(url)
        try:
            head = session.head(url, timeout=15, allow_redirects=True, verify=False)
            if head.status_code == 200 and "pdf" in head.headers.get("Content-Type", "").lower():
                return url
        except requests.RequestException:
            continue
    return None


def parse_ebook_page_count(config: str) -> int:
    match = re.search(r"\bbookConfig\.totalPageCount\s*=\s*(\d+)\s*;", config)
    if not match:
        raise ValueError("E-하나로 뷰어 설정에서 페이지 수를 찾지 못했습니다")
    count = int(match.group(1))
    if not 1 <= count <= 500:
        raise ValueError(f"E-하나로 페이지 수가 비정상적입니다: {count}")
    return count


def clean_ebook_page_text(raw: str, page_number: int) -> str:
    """Remove flipbook layout noise while keeping words and column boundaries."""
    raw = raw.replace("\r\n", "\n").replace("\r", "\n").replace("\u00a0", " ")
    raw = re.sub(r"[\u0000-\u0008\u000b\u000c\u000e-\u001f\ufffd]", "", raw)
    raw = re.sub(r"[\u0400-\u0dff]", "", raw)  # broken embedded-font glyphs
    lines: list[str] = []
    for source_line in raw.split("\n"):
        line = source_line.strip()
        if not line:
            if lines and lines[-1]:
                lines.append("")
            continue
        if line in {"1BSU", "+"}:
            continue
        if re.fullmatch(rf"0?{page_number}\s+대학생활\s*[\"“]?E-하나로[\"”]?", line):
            continue
        if ("대학생활 안내" in line and
                re.fullmatch(rf"Part\s+\d+\s+.+\s+0?{page_number}", line, re.I)):
            continue
        line = re.sub(r"[·.]{4,}", " | ", line)
        line = re.sub(r"[ \t]{3,}", " | ", line)
        line = re.sub(r"[ \t]+", " ", line)
        line = re.sub(r"\( +", "(", line)
        line = re.sub(r" +\)", ")", line)
        line = re.sub(r"(?:\s*\|\s*){2,}", " | ", line).strip(" |")
        if line:
            lines.append(line)
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)


def parse_ebook_page(html: str, page_number: int) -> dict[str, Any]:
    soup = BeautifulSoup(html, "lxml")
    text_node = soup.select_one(".text-container pre code")
    image = soup.select_one("#content-middle img")
    if text_node is None and image is None:
        raise ValueError(f"E-하나로 {page_number}쪽의 본문 영역을 찾지 못했습니다")
    raw = text_node.get_text() if text_node else ""
    content = clean_ebook_page_text(raw, page_number)
    hangul_count = len(re.findall(r"[가-힣]", content))
    suspicious_glyphs = len(re.findall(r"[\u0400-\u0dff]", raw))
    return {
        "page_number": page_number,
        "url": urljoin(EBOOK_BASE_URL, f"files/basic-html/page{page_number}.html"),
        "image_url": urljoin(EBOOK_BASE_URL + "files/basic-html/", image["src"]) if image and image.get("src") else None,
        "text_status": "readable" if hangul_count else ("unreadable" if raw.strip() else "image_only"),
        "characters": len(content) if hangul_count else 0,
        "source_characters": len(raw.strip()),
        "suspicious_glyphs": suspicious_glyphs,
        "content": content if hangul_count else "",
    }


def retire_legacy_ebook_metadata(state: dict[str, Any]) -> None:
    legacy_slug = make_slug(EBOOK_INDEX_URL, "국립 부경대학교 대학생활 E-하나로")
    legacy_path = PATHS.document_json("E-하나로", legacy_slug)
    if legacy_path.is_file():
        try:
            legacy = json.loads(legacy_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("[E-BOOK] 구버전 결과 확인 실패: %s", exc)
            return
        if legacy.get("url") != EBOOK_INDEX_URL or legacy.get("content"):
            return
        archived = PATHS.deleted_json("E-하나로", legacy_slug)
        if archived.exists():
            log.warning("[E-BOOK] 구버전 결과 보관 위치가 이미 존재합니다: %s", archived)
            return
        archived.parent.mkdir(parents=True, exist_ok=True)
        legacy_path.replace(archived)
    state.setdefault("items", {}).pop(legacy_slug, None)


def crawl_ebook_html(session: requests.Session, state: dict[str, Any], full_resync: bool) -> CrawlStats:
    stats = CrawlStats(discovered=1, requested=1)
    source_id = "ebook:col_life"
    slug = document_slug("pknu_student_life", source_id)
    config_url = urljoin(EBOOK_BASE_URL, "mobile/javascript/config.js")
    try:
        config = fetch(session, config_url)
        config.raise_for_status()
        page_count = parse_ebook_page_count(config.text)
    except (requests.RequestException, ValueError) as exc:
        log.error("[E-BOOK] 페이지 목록 확인 실패: %s", exc)
        stats.failed = 1
        return stats

    pages: list[dict[str, Any]] = []
    failed_pages: list[int] = []
    for number in range(1, page_count + 1):
        url = urljoin(EBOOK_BASE_URL, f"files/basic-html/page{number}.html")
        try:
            response = fetch(session, url)
            response.raise_for_status()
            pages.append(parse_ebook_page(response.content.decode("utf-8-sig", errors="replace"), number))
        except (requests.RequestException, ValueError) as exc:
            log.error("[E-BOOK] %d쪽 수집 실패: %s", number, exc)
            failed_pages.append(number)

    readable = [page for page in pages if page["text_status"] == "readable"]
    if not readable:
        log.error("[E-BOOK] 읽을 수 있는 본문이 없어 기존 결과를 유지합니다")
        stats.failed = 1
        return stats

    content = "\n\n".join(f"[Page {page['page_number']}]\n{page['content']}" for page in readable)
    old = state.setdefault("items", {}).get(slug, {})
    saved_path = PATHS.document_json(SUBCATEGORY_EBOOK, slug, kind="attachments")
    if failed_pages and saved_path.is_file():
        try:
            previous_content = json.loads(saved_path.read_text(encoding="utf-8")).get("content")
        except (OSError, json.JSONDecodeError):
            previous_content = None
        if previous_content:
            log.error("[E-BOOK] 일부 쪽 수집에 실패하여 기존 본문을 유지합니다")
            stats.failed = 1
            return stats
    digest = content_sha256(content)
    if (not full_resync and old.get("content_hash") == digest
            and old.get("parser_version") == EBOOK_HTML_PARSER_VERSION
            and saved_path.is_file()):
        stats.unchanged = 1
        if failed_pages:
            stats.failed = 1
        retire_legacy_ebook_metadata(state)
        return stats

    warnings = []
    unreadable_pages = [page["page_number"] for page in pages if page["text_status"] != "readable"]
    suspect_pages = [page["page_number"] for page in readable if page["suspicious_glyphs"]]
    if unreadable_pages:
        warnings.append(f"ebook_pages_without_readable_text:{','.join(map(str, unreadable_pages))}")
    if suspect_pages:
        warnings.append(f"ebook_pages_with_suspicious_glyphs:{','.join(map(str, suspect_pages))}")
    if failed_pages:
        warnings.append(f"ebook_pages_failed:{','.join(map(str, failed_pages))}")
        stats.failed = 1
    doc: dict[str, Any] = {
        "title": "국립 부경대학교 대학생활 E-하나로",
        "date": "",
        "url": EBOOK_INDEX_URL,
        "pdf_url": None,
        "category": '대학생활',
        "subcategory": SUBCATEGORY_EBOOK,
        "type": 'guide',
        "year": None,
        "content": content,
        "attachments": [],
        "pages": pages,
        "page_count": page_count,
        "parser_version": EBOOK_HTML_PARSER_VERSION,
        "source_site": BASE_URL,
        "crawled_at": now_kst(),
    }
    doc = apply_common_schema(
        doc,
        source_dataset="pknu_student_life",
        source_id=source_id,
        source_site=BASE_URL,
        document_type="guide",
        content_source="ebook_basic_html",
        metadata={"page_count": page_count, "readable_page_count": len(readable), "pdf_url": None},
        crawled_at=doc["crawled_at"],
    )
    doc["crawl"]["warnings"] = warnings
    if failed_pages:
        doc["crawl"]["status"] = "partial_success"
    remove_redundant_legacy_fields(doc)
    save_json(doc, SUBCATEGORY_EBOOK, slug)
    state["items"][slug] = {
        "slug": slug, "content_hash": doc["content_hash"],
        "parser_version": EBOOK_HTML_PARSER_VERSION,
        "url": EBOOK_INDEX_URL, "last_seen_at": now_kst(),
    }
    retire_legacy_ebook_metadata(state)
    setattr(stats, "updated" if old else "new", 1)
    log_event(log, logging.INFO, "document_saved", source_id=source_id,
              url=EBOOK_INDEX_URL, status="updated" if old else "new")
    return stats


def crawl_ebook(session: requests.Session, state: dict[str, Any], full_resync: bool) -> CrawlStats:
    stats = CrawlStats(discovered=1, requested=1)
    pdf_url = discover_ebook_pdf_url(session)
    title = "국립 부경대학교 대학생활 E-하나로"
    source_id = "ebook:col_life"
    slug = document_slug("pknu_student_life", source_id)
    subcategory = SUBCATEGORY_EBOOK

    if not pdf_url:
        log.info("[E-BOOK] 원본 PDF 미발견: 페이지별 HTML 텍스트 수집")
        return crawl_ebook_html(session, state, full_resync)

    log.info("[E-BOOK] PDF 발견: %s", pdf_url)
    item = GuideItem(
        title=title,
        pdf_url=pdf_url,
        media_id=None,
        subcategory=subcategory,
        year=None,
        source_url=EBOOK_INDEX_URL,
    )
    status, doc = process_guide_item(session, item, state, full_resync)
    setattr(stats, status, getattr(stats, status) + 1)
    if doc:
        stats.count_attachments(doc.get("attachments"))
    return stats
