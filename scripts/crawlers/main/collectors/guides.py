'Main website collectors guides.'

from __future__ import annotations
from bs4 import BeautifulSoup
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from scripts.crawlers.common.logging import log_attachment_events, log_event
from scripts.crawlers.common.reader import remove_redundant_legacy_fields
from scripts.crawlers.common.schema import (
    CrawlStats,
    apply_common_schema,
    attachment_text,
    content_sha256,
    document_slug,
    now_kst,
    sanitize_attachment_filename,
    url_source_id,
)
from scripts.crawlers.main.common.documents import (
    MIN_PDF_TEXT_CHARS,
    content_hash,
    download_pdf,
    extract_pdf_text,
    infer_date,
    make_slug,
    normalize_title,
)
from scripts.crawlers.main.common.http import fetch, resolve_media_pdf_url
from scripts.crawlers.main.common.storage import (
    BASE_URL,
    EBOOK_INDEX_URL,
    GUIDE_URL,
    PATHS,
    PROJECT_ROOT,
    log,
    save_json,
)
from typing import Any
from urllib.parse import urljoin, urlparse
import json
import logging
import re
import requests


SECTION_YEAR_RE = re.compile(r"(20\d{2})학년도")


@dataclass
class GuideItem:
    title: str
    pdf_url: str
    media_id: str | None
    subcategory: str
    year: int | None
    section_year: int | None = None
    download_name: str = ""
    source_url: str = GUIDE_URL


def infer_subcategory(title: str) -> str:
    if "예비부경" in title or "입학준비" in title:
        return "예비부경인"
    if "로드맵" in title or "교육과정" in title or "이수" in title:
        return "이수_로드맵"
    return "슬기로운_대학생활"


def extract_section_year(h4_text: str) -> int | None:
    m = SECTION_YEAR_RE.search(h4_text)
    return int(m.group(1)) if m else None


def find_section_year_for_element(element: Any) -> int | None:
    for h4 in element.find_all_previous("h4", class_="subNameH4", limit=15):
        year = extract_section_year(h4.get_text(" ", strip=True))
        if year is not None:
            return year
    return None


def infer_year(
    title: str,
    pdf_url: str = "",
    *,
    section_year: int | None = None,
    download: str = "",
) -> int | None:
    if section_year is not None:
        return section_year
    m = re.search(r"(20\d{2})", title)
    if m:
        return int(m.group(1))
    if download:
        m = re.search(r"(20\d{2})", download)
        if m:
            return int(m.group(1))
    m = re.search(r"/media/(20\d{2})/", pdf_url)
    return int(m.group(1)) if m else None


def guide_item_identity(element: Any, raw_title: str) -> tuple[str, str]:
    """Preserve the contest heading so numbered PDF titles stay meaningful."""
    title = normalize_title(raw_title)
    heading = element.find_previous("h4", class_="subNameH4")
    section = heading.get_text(" ", strip=True) if heading else ""
    if "콘테스트" in section and title.startswith("우수작"):
        year = extract_section_year(section)
        prefix = f"{year}학년도 " if year else ""
        return f"{prefix}대학생활계획서 콘테스트 {title}", "학생생활"
    return title, infer_subcategory(title)


def parse_guide_items_from_html(session: requests.Session, html: str) -> list[GuideItem]:
    soup = BeautifulSoup(html, "lxml")
    pending: list[GuideItem] = []
    seen_urls: set[str] = set()

    for div in soup.select("motion.div.uploadPdf, div.uploadPdf"):
        media_id = div.get("data-id")
        if not media_id:
            continue
        section_year = find_section_year_for_element(div)
        li = div.find_previous("li")
        raw_title = li.get_text(" ", strip=True) if li else f"media-{media_id}"
        title, subcategory = guide_item_identity(div, raw_title)
        pending.append(
            GuideItem(
                title=title,
                pdf_url="",
                media_id=media_id,
                subcategory=subcategory,
                year=None,
                section_year=section_year,
            )
        )

    for anchor in soup.select('a[href*=".pdf"]'):
        href = anchor.get("href", "").strip()
        if not href:
            continue
        section_year = find_section_year_for_element(anchor)
        download_name = anchor.get("download", "").strip()
        li = anchor.find_parent("li")
        context = (li.get_text(" ", strip=True) if li else "") or anchor.get_text(" ", strip=True)
        title, subcategory = guide_item_identity(anchor, context or download_name or "PDF")
        pdf_url = urljoin(GUIDE_URL, href)
        if pdf_url in seen_urls:
            continue
        seen_urls.add(pdf_url)
        pending.append(
            GuideItem(
                title=title,
                pdf_url=pdf_url,
                media_id=None,
                subcategory=subcategory,
                year=None,
                section_year=section_year,
                download_name=download_name,
            )
        )

    for item in pending:
        if item.media_id and not item.pdf_url:
            try:
                item.pdf_url = resolve_media_pdf_url(session, item.media_id)
            except Exception as exc:
                log.warning("getMdaId failed id=%s: %s", item.media_id, exc)
        if item.pdf_url:
            item.year = infer_year(
                item.title,
                item.pdf_url,
                section_year=item.section_year,
                download=item.download_name,
            )

    unique: dict[str, GuideItem] = {}
    for item in pending:
        if not item.pdf_url:
            continue
        if item.pdf_url in unique:
            existing = unique[item.pdf_url]
            if len(item.title) > len(existing.title):
                unique[item.pdf_url] = item
        else:
            unique[item.pdf_url] = item

    return list(unique.values())


def process_guide_item(
    session: requests.Session,
    item: GuideItem,
    state: dict[str, Any],
    full_resync: bool,
    raw_html: str | None = None,
) -> tuple[str, dict[str, Any] | None]:
    legacy_slug = make_slug(item.pdf_url, item.title)
    if item.source_url == EBOOK_INDEX_URL:
        source_id = "ebook:col_life"
    elif item.media_id:
        source_id = f"guide:media:{item.media_id}"
    else:
        source_id = f"guide:pdf:{url_source_id(item.pdf_url)}"
    slug = document_slug("pknu_student_life", source_id)
    subcategory = item.subcategory

    filename = sanitize_attachment_filename(Path(urlparse(item.pdf_url).path).name or f"{slug}.pdf")
    file_dir = PATHS.attachment_dir(subcategory, slug)
    pdf_path = PATHS.attachment_file(subcategory, slug, filename)

    old_hash = ""
    items_state: dict[str, Any] = state.setdefault("items", {})
    prev = items_state.get(slug, {}) or items_state.get(legacy_slug, {})
    if prev and not full_resync:
        old_hash = prev.get("content_hash", "")

    attachment = download_pdf(session, item.pdf_url, pdf_path)
    attachment["source_page_url"] = item.source_url
    attachment["source_site"] = BASE_URL
    content = ""
    extracted_characters = 0
    if attachment["downloaded"]:
        try:
            content = extract_pdf_text(pdf_path)
            extracted_characters = len(content)
        except Exception as exc:
            attachment["text"] = attachment_text(
                "failed", extractor="pymupdf", error=exc
            )
            log.warning("[PDF-TEXT-FAILED] %s: %s", item.title, exc)
    c_hash = content_sha256(content)

    current_json_path = PATHS.document_json(subcategory, slug, kind="attachments")
    current_schema_exists = False
    if current_json_path.exists():
        try:
            current_doc = json.loads(current_json_path.read_text(encoding="utf-8"))
            current_schema_exists = current_doc.get("schema_version") == "1.0"
        except (OSError, json.JSONDecodeError):
            pass

    if (
        not full_resync
        and attachment["downloaded"]
        and old_hash
        and old_hash == c_hash
        and current_schema_exists
    ):
        log.info("[SKIP] %s (unchanged)", item.title)
        log_event(log, logging.INFO, "document_unchanged", source_id=source_id, url=item.source_url, status="unchanged")
        return "unchanged", None

    if (
        attachment["downloaded"]
        and attachment["text"]["status"] != "failed"
        and len(content) < MIN_PDF_TEXT_CHARS
    ):
        log.warning(
            "[PDF-TEXT-SKIP] %s — extracted %d chars (< %d), metadata only",
            item.title,
            len(content),
            MIN_PDF_TEXT_CHARS,
        )
        attachment["text"] = attachment_text(
            "skipped",
            characters=extracted_characters,
            extractor="pymupdf",
            error=f"extracted_chars_below_{MIN_PDF_TEXT_CHARS}",
        )
        content = ""
    elif attachment["downloaded"] and attachment["text"]["status"] != "failed":
        attachment["text"] = attachment_text(
            "success",
            content=content,
            characters=len(content),
            extractor="pymupdf",
        )

    year = item.year or infer_year(
        item.title,
        item.pdf_url,
        section_year=item.section_year,
        download=item.download_name,
    )
    doc: dict[str, Any] = {
        "slug": slug,
        "title": item.title,
        "date": infer_date(year),
        "url": item.source_url,
        "pdf_url": item.pdf_url,
        "category": '대학생활',
        "subcategory": subcategory,
        "type": 'guide',
        "year": year,
        "content": content,
        "content_hash": content_hash(content) if content else content_hash(pdf_path.as_posix()),
        "attachments": [attachment],
        "source_site": BASE_URL,
        "crawled_at": datetime.now().isoformat(),
    }
    if item.media_id:
        doc["media_id"] = item.media_id
    if not content:
        doc["pdf_text_skipped"] = True
        if not attachment["downloaded"]:
            doc["pdf_text_skip_reason"] = "pdf_download_failed"
        elif attachment["text"]["status"] == "failed":
            doc["pdf_text_skip_reason"] = "pdf_text_extraction_failed"
        else:
            doc["pdf_text_skip_reason"] = f"extracted_chars_below_{MIN_PDF_TEXT_CHARS}"

    doc = apply_common_schema(
        doc,
        source_dataset="pknu_student_life",
        source_id=source_id,
        source_site=BASE_URL,
        document_type="guide",
        content_source="pdf_text",
        published_at=doc.get("date"),
        metadata={
            "year": year,
            "media_id": item.media_id,
            "pdf_url": item.pdf_url,
            "legacy_slug": legacy_slug,
            "pdf_text_skipped": doc.get("pdf_text_skipped", False),
            "pdf_text_skip_reason": doc.get("pdf_text_skip_reason"),
        },
        crawled_at=doc.get("crawled_at"),
    )
    remove_redundant_legacy_fields(doc)

    save_json(doc, subcategory, slug, raw_html)
    outcome = "new" if not prev else "updated"
    log_attachment_events(log, doc.get("attachments"), source_id=source_id)
    log_event(log, logging.INFO, "document_saved", source_id=source_id, url=item.source_url, status=outcome)
    items_state[slug] = {
        "slug": slug,
        "content_hash": doc["content_hash"],
        "pdf_url": item.pdf_url,
        "last_seen_at": now_kst(),
    }
    return outcome, doc


def existing_guide_pdf_urls() -> set[str]:
    """Recognize saved legacy guide results without creating duplicate documents."""
    urls: set[str] = set()
    for path in PATHS.iter_document_json():
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if doc.get("url") != GUIDE_URL or not doc.get("pdf_url"):
            continue
        for attachment in doc.get("attachments") or []:
            saved = attachment.get("saved_path")
            if not saved:
                continue
            file_path = Path(saved)
            if not file_path.is_absolute():
                file_path = PROJECT_ROOT / file_path
            if file_path.is_file() and file_path.stat().st_size > 0:
                urls.add(doc["pdf_url"])
                break
    return urls


def crawl_guide(
    session: requests.Session,
    state: dict[str, Any],
    full_resync: bool,
    limit: int | None,
) -> CrawlStats:
    resp = fetch(session, GUIDE_URL)
    resp.raise_for_status()
    items = parse_guide_items_from_html(session, resp.text)
    log.info("[GUIDE] PDF 대상 %d건", len(items))
    existing_urls = existing_guide_pdf_urls()

    discovered = len(items)
    if limit is not None:
        items = items[:limit]

    stats = CrawlStats(discovered=discovered, skipped=discovered - len(items))
    if stats.skipped:
        log_event(log, logging.INFO, "document_skipped", reason="limit", count=stats.skipped)
    for i, item in enumerate(items, start=1):
        log_event(log, logging.DEBUG, "document_discovered", source_id=item.media_id, url=item.source_url, title=item.title)
        log.info("[GUIDE] %d/%d %s", i, len(items), item.title)
        if not full_resync and item.pdf_url in existing_urls:
            stats.requested += 1
            stats.unchanged += 1
            log.info("[GUIDE] existing result: %s", item.title)
            continue
        try:
            stats.requested += 1
            status, doc = process_guide_item(session, item, state, full_resync, resp.text)
            setattr(stats, status, getattr(stats, status) + 1)
            if doc:
                stats.count_attachments(doc.get("attachments"))
        except Exception as exc:
            stats.failed += 1
            log.error("[GUIDE] 실패 %s: %s", item.title, exc)
            log_event(log, logging.ERROR, "document_failed", source_id=item.media_id, url=item.source_url, error=exc)
    return stats
