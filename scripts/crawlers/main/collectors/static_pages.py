'Main website collectors static_pages.'

from __future__ import annotations
from bs4 import BeautifulSoup
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from scripts.crawlers.common.body_images import collect_body_images, save_body_images
from scripts.crawlers.common.logging import log_event
from scripts.crawlers.common.main_static import (
    extract_card_structure,
    extract_structure,
    linked_file_ids,
    table_reference_links,
)
from scripts.crawlers.common.schema import CrawlStats, attachment_text
from scripts.preprocessing.extractors.common import extract_blocks
from scripts.preprocessing.extractors.image_ocr import VERSION as IMAGE_OCR_VERSION, extract_image
from scripts.crawlers.main.common import board as cms_board
from scripts.crawlers.main.common.documents import (
    MIN_PDF_TEXT_CHARS,
    content_hash,
    make_slug,
    normalize_title,
)
from scripts.crawlers.main.common.http import fetch, resolve_media_pdf_url
from scripts.crawlers.main.common.storage import (
    BASE_URL,
    PATHS,
    PROJECT_ROOT,
    SUBCATEGORY_ACADEMIC_INFO,
    SUBCATEGORY_STATIC_PAGE,
    log,
    save_json,
)
from scripts.crawlers.main.routes import FILE_PAGE_IDS, ROUTES, STATIC_PAGE_IDS, body_image_ocr_enabled
from scripts.crawlers.main.paths import asset_type
from typing import Any
from urllib.parse import urljoin, urlparse
import hashlib
import json
import logging
import re
import requests
import shutil
import zipfile


STATIC_PARSER_VERSION = 21


def static_page_subcategory(page_id: int) -> str:
    """Route verified pages to their configured site category."""
    from scripts.crawlers.main.routes import ROUTES
    route = ROUTES.get(page_id)
    if route is None:
        return SUBCATEGORY_STATIC_PAGE
    if route.options.get("document_subcategory"):
        return route.options["document_subcategory"]
    if route.category_path == ("대학생활", "학사안내"):
        return SUBCATEGORY_STATIC_PAGE
    if route.category_path == ("대학생활", "학사정보"):
        return SUBCATEGORY_ACADEMIC_INFO
    return route.category_path[-1]


@dataclass(frozen=True)
class StaticPageExtraction:
    title: str
    content: str
    status: str
    reason: str = ""
    warnings: tuple[str, ...] = ()
    structure: dict[str, Any] | None = None
    linked_file_page_ids: tuple[int, ...] = ()
    body_images: tuple[dict[str, Any], ...] = ()
    attachments: tuple[dict[str, str], ...] = ()
    embedded_pdfs: tuple[dict[str, str], ...] = ()
    reference_links: tuple[dict[str, Any], ...] = ()


_STATIC_REMOVE = (
    "script, style, noscript, .subMenu, .subTab, .subTitle, .edtDay, .paging, "
    ".brdSch, .brdAll, .brdBtn, .bdvNav, .c_bdvNav, .share, .sns, .lgnForm"
)


_STATIC_BOARD = ".brdList, .board_list, .board-list, .board-w, #tbl_contents, input[name=bbsId], input[name=bbs_id]"


def extract_static_attachments(container, page_url: str, options: dict) -> tuple[tuple, tuple]:
    """Read same-site file links, including files offered by download selectors."""
    candidates: dict[str, dict] = {}
    selector_urls: set[str] = set()
    selector = 'a[href], option[download][value]'
    if options.get("collect_audio_files"):
        selector += ', audio[src], audio source[src]'
    extension_filter = options.get("attachment_extensions")
    for element in container.select(selector):
        attribute = "value" if element.name == "option" else "src" if element.name in {"audio", "source"} else "href"
        file_url = urljoin(page_url, element.get(attribute, ""))
        parsed = urlparse(file_url)
        extension = Path(parsed.path).suffix.lower()
        if extension_filter is not None and extension not in extension_filter:
            continue
        direct_upload = (parsed.path.startswith("/upload/") and
                         (extension in {".pdf", ".hwp", ".hwpx", ".pptx", ".docx", ".zip"}
                          or (options.get("collect_audio_files") and asset_type(parsed.path) == "audio")))
        media_download = (parsed.path == "/mdaDownload.do" and
                          re.fullmatch(r"no=\d+", parsed.query) is not None)
        board_download = (parsed.path == "/boardDownload.do" and
                          re.fullmatch(r"no=\d+", parsed.query) is not None)
        if (parsed.scheme != "https" or parsed.hostname != "www.pknu.ac.kr"
                or not (direct_upload or media_download or board_download)):
            continue
        label = element.get_text(" ", strip=True)
        if element.name in {"audio", "source"}:
            group = element.find_parent("li")
            heading = group.select_one("h2, h3, h4, h5, h6") if group else None
            label = heading.get_text(" ", strip=True) if heading else ""
        name = (element.get("download") if element.name == "option" else
                label or element.get("download")) or label or Path(parsed.path).name
        if element.name == "option" and extension:
            name = str(Path(name).with_suffix(extension))
        candidate = candidates.setdefault(file_url, {"name": name, "url": file_url})
        if element.name == "option":
            selector_urls.add(file_url)
            year = re.search(r"(?<!\d)(?:19|20)\d{2}(?!\d)", label)
            if year:
                candidate["year"] = int(year.group())
    selected = list(candidates.values())
    if options.get("download_pdf_attachments") is False:
        selected = [item for item in selected if Path(urlparse(item["url"]).path).suffix.lower() != ".pdf"]
    limit = options.get("attachment_limit")
    if limit is not None:
        selected = sorted(selected, key=lambda item: item.get("year", 0), reverse=True)[:limit]
    selected_urls = {item["url"] for item in selected}
    references = tuple({"text": item["name"], "url": item["url"], "role": "attachment",
                        **({"year": item["year"]} if "year" in item else {}),
                        "selected_for_download": item["url"] in selected_urls}
                       for item in candidates.values() if item["url"] in selector_urls
                       or options.get("record_attachment_links")
                       or (options.get("download_pdf_attachments") is False
                           and Path(urlparse(item["url"]).path).suffix.lower() == ".pdf"))
    return tuple(selected), references


def body_reference_links(container, page_url: str, attachment_urls: set[str]) -> tuple[dict, ...]:
    """Record non-file body links without following them; table links retain cell metadata."""
    references = []
    seen = set()
    for anchor in container.select("a[href]"):
        href = str(anchor.get("href") or "").strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        url = urljoin(page_url, href)
        parsed = urlparse(url)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username or parsed.password or url in attachment_urls
                or anchor.find_parent("table") is not None):
            continue
        text = re.sub(r"\s+", " ", anchor.get_text(" ", strip=True))
        key = (url, text)
        if key not in seen:
            references.append({"text": text, "url": url, "role": "reference", "followed": False})
            seen.add(key)
    return tuple(references)


def parse_static_page_html(html: str, page_id: int) -> StaticPageExtraction:
    """Extract static prose, tables, and CMS process diagrams together."""
    soup = BeautifulSoup(html, "lxml")
    route = ROUTES.get(page_id)
    options = route.options if route else {}
    title = options.get("document_title") or (normalize_title(soup.title.get_text(strip=True))
                                               if soup.title else f"main-{page_id}")
    container = soup.select_one("#subCont")
    if container is None:
        return StaticPageExtraction(title, "", "needs_review", "no_subcont")
    if container.select_one(_STATIC_BOARD):
        return StaticPageExtraction(title, "", "needs_review", "board_list_not_static")
    file_ids = linked_file_ids(container, f"{BASE_URL}/main/{page_id}", set(FILE_PAGE_IDS))
    tab_links = tuple({"text": anchor.get_text(" ", strip=True),
                       "url": urljoin(f"{BASE_URL}/main/{page_id}", anchor["href"]),
                       "role": "tab", "selected": "current" in anchor.parent.get("class", []),
                       "followed": False}
                      for anchor in container.select(".subTab a[href]"))
    body = BeautifulSoup(str(container), "lxml")
    for element in body.select(_STATIC_REMOVE):
        element.decompose()
    image_root = body.select_one("#subCont .content_wrap")
    if image_root is None:
        containers = body.select("#subCont")
        image_root = containers[-1] if containers else body
        image_root = (image_root.find("div", class_="col-lg-12", recursive=False)
                      or image_root)
    collect_images = options.get("collect_body_images", True) is not False
    if not collect_images:
        for image in image_root.select("img, picture, svg"):
            if image.parent is not None:
                image.decompose()
    omitted_image_count = 0
    if options.get("exclude_link_hosts"):
        for anchor in image_root.select('a[href]'):
            target = urljoin(f"{BASE_URL}/main/{page_id}", anchor.get("href", ""))
            if urlparse(target).hostname in options["exclude_link_hosts"]:
                if anchor.parent and "sblTxt" in anchor.parent.get("class", []):
                    anchor.parent.decompose()
                else:
                    anchor.decompose()
    if options.get("exclude_embedded_pdfs"):
        for viewer in image_root.select('.uploadPdf[data-id]'):
            label = viewer.find_previous_sibling('li')
            if label is not None:
                label.decompose()
            viewer.decompose()
    if options.get("exclude_image_hosts"):
        for img in image_root.select('img[src]'):
            if urlparse(img['src']).hostname in options["exclude_image_hosts"]:
                img.decompose()
                omitted_image_count += 1
    image_wrapper = BeautifulSoup('<div class="sub-content"></div>', "lxml")
    if image_root is not None:
        image_wrapper.select_one(".sub-content").append(BeautifulSoup(str(image_root), "lxml"))
    body_images = tuple(collect_body_images(image_wrapper)) if collect_images else ()
    section_selector = options.get("image_section_selector")
    if section_selector:
        sections = image_root.select(section_selector)
        for image in body_images:
            section = next((section for section in sections if any(
                element.get("src") == image["src"] for element in section.select("img[src]"))), None)
            if section is not None:
                heading = section.select_one(options.get("image_section_title_selector", "h2, h3, h4"))
                image["section_id"] = section.get("id")
                image["section_title"] = heading.get_text(" ", strip=True) if heading else image["alt"]
    embedded_pdfs = []
    for element in image_root.select('.uploadPdf[data-id]'):
        label = element.find_previous_sibling('li')
        name = label.get_text(' ', strip=True) if label else f"PDF {element['data-id']}"
        embedded_pdfs.append({"media_id": str(element['data-id']).strip(), "name": name})
    attachments, attachment_links = extract_static_attachments(image_root, f"{BASE_URL}/main/{page_id}", options)
    for selector in options.get("exclude_body_selectors", []):
        for element in image_root.select(selector):
            element.decompose()
    try:
        if options.get("card_selector"):
            structure = extract_card_structure(
                image_root, selector=options["card_selector"],
                title_selector=options.get("card_title_selector", "h5"),
                field_selectors=options.get("card_field_selectors", {}),
            )
        else:
            structure = extract_structure(image_root)
        content = structure["content"]
        reference_links = (*table_reference_links(image_root, f"{BASE_URL}/main/{page_id}"),
                           *tab_links, *attachment_links)
        if options.get("record_body_links"):
            reference_links += body_reference_links(
                image_root, f"{BASE_URL}/main/{page_id}",
                {item["url"] for item in attachments} | {item["url"] for item in attachment_links})
    except ValueError as exc:
        fallback = re.sub(r"\n{2,}", "\n", body.get_text("\n", strip=True)).strip()
        return StaticPageExtraction(title, fallback, "needs_review",
                                    f"structure_unverified: {exc}")
    expected_pdfs = options.get("embedded_pdf_count")
    if expected_pdfs is not None and (len(embedded_pdfs) != expected_pdfs or any(
            not item["media_id"].isdigit() for item in embedded_pdfs)):
        return StaticPageExtraction(title, content, "needs_review", "embedded_pdf_ids_changed",
                                    structure=structure)
    if len(attachments) < options.get("min_attachments", 0):
        return StaticPageExtraction(title, content, "needs_review", "accident_files_missing",
                                    structure=structure)
    other_dynamic = body.select_one(
        ".getOrgMngList, #calendar, #loadArea, #direction_daeyeon, #direction_yongdang"
    )
    if other_dynamic or (expected_pdfs is None and body.select_one('.uploadPdf[data-id]')):
        return StaticPageExtraction(title, content, "needs_review", "dynamic_content_requires_adapter",
                                    structure=structure, linked_file_page_ids=file_ids)
    image_text = bool(options.get("content_from_images") and body_images
                      and body_image_ocr_enabled(options))
    asset_backed = bool((options.get("allow_asset_only_body") and (attachments or body_images))
                        or (body_images and not body_image_ocr_enabled(options)))
    if body.select_one("img[src], img[data-src]") and not content and not image_text and not asset_backed:
        return StaticPageExtraction(title, content, "needs_review", "image_only_requires_ocr",
                                    structure=structure, linked_file_page_ids=file_ids)
    attachment = body.select_one('.uploadPdf[data-id], a[download], a[href$=".pdf" i], a[href$=".hwp" i], a[href$=".hwpx" i]')
    if attachment and len(content) < MIN_PDF_TEXT_CHARS and not asset_backed:
        return StaticPageExtraction(title, content, "needs_review", "attachment_only_requires_extractor",
                                    structure=structure, linked_file_page_ids=file_ids)
    without_links = BeautifulSoup(str(body), "lxml")
    for anchor in without_links.select("a"):
        anchor.decompose()
    prose_length = len(without_links.get_text(" ", strip=True))
    if body.select_one("a[href]") and prose_length < MIN_PDF_TEXT_CHARS and not image_text and not asset_backed:
        return StaticPageExtraction(title, content, "needs_review", "link_hub_not_static",
                                    structure=structure, linked_file_page_ids=file_ids)
    if len(content) < MIN_PDF_TEXT_CHARS and not image_text and not asset_backed:
        return StaticPageExtraction(title, content, "needs_review", "content_too_short",
                                    structure=structure, linked_file_page_ids=file_ids)
    warnings = (f"unavailable_source_images:{omitted_image_count}",) if omitted_image_count else ()
    return StaticPageExtraction(title, content, "ready", warnings=warnings, structure=structure,
                                linked_file_page_ids=file_ids, body_images=body_images,
                                attachments=tuple(attachments),
                                embedded_pdfs=tuple(embedded_pdfs),
                                reference_links=reference_links)


def fetch_static_page(session: requests.Session, page_id: int) -> StaticPageExtraction:
    """Fetch a main-site info page and return its validated static extraction."""
    url = f"{BASE_URL}/main/{page_id}"
    resp = fetch(session, url)
    resp.raise_for_status()
    mime = getattr(resp, "headers", {}).get("Content-Type", "").lower()
    if mime and "text/html" not in mime and "application/xhtml+xml" not in mime:
        return StaticPageExtraction(f"main-{page_id}", "", "needs_review", "non_html_response")
    final_url = getattr(resp, "url", url)
    if final_url != url:
        return StaticPageExtraction(f"main-{page_id}", "", "needs_review", "redirect_destination_unverified")
    from scripts.crawlers.main.paths import page_root
    path = page_root(PROJECT_ROOT, page_id) / "html" / f"main_{page_id}.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(resp.text, encoding="utf-8")
    return parse_static_page_html(resp.text, page_id)


def download_static_body_images(
    session: requests.Session, page_url: str, subcategory: str, slug: str,
    candidates: tuple[dict[str, Any], ...],
    cached_images: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Preserve the original images before saving a static page document."""
    if not candidates:
        return []
    # Reuse complete, unchanged originals; verify every source URL and file hash.
    cached = {item.get("source_url"): item for item in (cached_images or [])}
    reusable = []
    for candidate in candidates:
        previous = cached.get(urljoin(page_url, candidate["src"]))
        try:
            path = (PROJECT_ROOT / previous["saved_path"]).resolve() if previous else None
            valid = (path is not None and path.is_relative_to(PROJECT_ROOT.resolve())
                     and path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == previous["sha256"])
        except (KeyError, OSError, TypeError):
            valid = False
        if not valid:
            break
        reusable.append({**previous, **{key: candidate[key] for key in
                         ("order", "alt", "section_id", "section_title") if key in candidate},
                         "status": "saved", "error": None})
    if len(reusable) == len(candidates):
        return reusable
    records = save_body_images(
        list(candidates), session=session, page_url=page_url,
        output_dir=PATHS.attachment_dir(subcategory, slug) / "images",
        project_root=PROJECT_ROOT,
    )
    failed = [record for record in records if record["status"] != "saved"]
    if failed:
        raise ValueError(f"body_image_download_failed: {failed[0].get('error')}")
    for record, candidate in zip(records, candidates):
        for key in ("section_id", "section_title"):
            if key in candidate:
                record[key] = candidate[key]
    return records


def image_text_sections(records: list[dict[str, Any]]) -> tuple[str, list[dict]]:
    """Keep OCR text in source-image order, with a separate named section per image."""
    sections = []
    for record in records:
        reviewed = record.get("visual_extraction", {})
        use_review = (reviewed.get("status") == "extracted" and reviewed.get("text")
                      and reviewed.get("source_sha256") == record.get("sha256"))
        text = reviewed["text"] if use_review else record.get("ocr_text", "")
        text = re.sub(r"[\u200b\u200c\u200d\ufeff]", "", text)
        lines = [re.sub(r"[^\S\n]+", " ", line).strip() for line in text.splitlines()]
        text = "\n".join(line for line in lines if line)
        sections.append({"image_order": record["order"], "id": record.get("section_id"),
                         "title": record.get("section_title") or record.get("alt") or f"이미지 {record['order']}",
                         "source_url": record.get("source_url"), "ocr_status": record["ocr_status"],
                         "text_source": "visual_extraction" if use_review else "image_ocr",
                         "content": text})
    return "\n\n".join(f"{section['title']}\n{section['content']}" for section in sections), sections


def recognize_static_body_images(records: list[dict[str, Any]]) -> None:
    """Keep tentative OCR beside its source image, outside verified page text."""
    executable = shutil.which("tesseract") or "C:/Program Files/Tesseract-OCR/tesseract.exe"
    tessdata = PROJECT_ROOT / ".tools/tessdata"
    for record in records:
        try:
            result = extract_image(
                PROJECT_ROOT / record["saved_path"], executable=executable,
                language="kor+eng", tessdata=str(tessdata) if tessdata.is_dir() else None,
                psm=11, restore_spacing=True,
            )
            record["ocr_status"] = result["status"]
            record["ocr_blocks"] = result["blocks"]
            record["ocr_text"] = "\n".join(block["text"] for block in result["blocks"])
            record["ocr_warnings"] = result["warnings"]
        except Exception as exc:
            record["ocr_status"] = "failed"
            record["ocr_blocks"] = []
            record["ocr_text"] = ""
            record["ocr_warnings"] = ["OCR_FAILED"]
            record["ocr_error"] = f"{type(exc).__name__}: {exc}"


def extract_static_attachment_text(records: list[dict[str, Any]]) -> None:
    """Keep each downloaded guide's text beside its file metadata."""
    for record in records:
        if not record.get("downloaded"):
            continue
        if record.get("text", {}).get("status") == "success":
            continue
        try:
            path = PROJECT_ROOT / record["saved_path"]
            if asset_type(path.name) == "audio":
                record["text"] = attachment_text("skipped", extractor="audio_original")
                continue
            if asset_type(path.name) == "images" and not body_image_ocr_enabled():
                record["text"] = attachment_text("skipped", extractor="image_original")
                continue
            if path.suffix.lower() == ".zip":
                with zipfile.ZipFile(path) as archive:
                    members = archive.infolist()
                    if len(members) > 200 or sum(item.file_size for item in members) > 200 * 1024 * 1024:
                        raise ValueError("archive member or uncompressed size limit exceeded")
                    record["archive_members"] = [
                        {"name": item.filename, "size_bytes": item.file_size}
                        for item in members if not item.is_dir()
                    ]
                record["text"] = attachment_text("skipped", extractor="zip_archive_manifest")
                continue
            blocks = extract_blocks(path)
            content = "\n".join(str(block.get("text") or "").strip()
                                for block in blocks if block.get("text")).strip()
            record["text"] = attachment_text(
                "success" if content else "skipped", content=content,
                extractor="scripts.preprocessing.extractors.common.extract_blocks",
            )
            if path.suffix.lower() == ".pdf":
                import fitz
                with fitz.open(path) as pdf:
                    page_count = len(pdf)
                page_text: dict[int, list[str]] = {}
                for block in blocks:
                    if isinstance(block.get("page"), int) and block.get("text"):
                        page_text.setdefault(block["page"], []).append(block["text"].strip())
                record["text"]["page_count"] = page_count
                record["text"]["pages"] = [
                    {"page_number": number, "content": "\n".join(page_text.get(number, []))}
                    for number in range(1, page_count + 1)
                ]
        except Exception as exc:
            record["text"] = attachment_text(
                "failed", extractor="scripts.preprocessing.extractors.common.extract_blocks",
                error=f"{type(exc).__name__}: {exc}",
            )


def resolve_static_embedded_pdfs(
    session: requests.Session, records: tuple[dict[str, str], ...],
) -> list[dict[str, str]]:
    """Resolve verified CMS PDF viewer IDs without visiting linked pages."""
    resolved = []
    for record in records:
        url = resolve_media_pdf_url(session, record["media_id"])
        parsed = urlparse(url)
        if (parsed.scheme != "https" or parsed.hostname != "www.pknu.ac.kr"
                or not parsed.path.startswith("/upload/")
                or Path(parsed.path).suffix.lower() != ".pdf"):
            raise ValueError(f"embedded_pdf_url_unverified: {url}")
        resolved.append({"name": record["name"], "url": url,
                         "media_id": record["media_id"]})
    return resolved


def crawl_static_pages(
    session: requests.Session, state: dict[str, Any], full_resync: bool,
    page_ids: tuple[int, ...] | None = None,
    route_results: list[dict[str, Any]] | None = None,
    prepared: dict[int, StaticPageExtraction] | None = None,
) -> CrawlStats:
    selected = STATIC_PAGE_IDS if page_ids is None else page_ids
    stats = CrawlStats(discovered=len(selected))
    items_state: dict[str, Any] = state.setdefault("items", {})

    for page_id in selected:
        url = f"{BASE_URL}/main/{page_id}"
        log_event(log, logging.INFO, "document_discovered", source_id=f"page:{page_id}", url=url)
        stats.requested += 1
        try:
            fetched = prepared[page_id] if prepared and page_id in prepared else fetch_static_page(session, page_id)
        except Exception as exc:
            stats.failed += 1
            if route_results is not None:
                route_results.append({"page_id": page_id, "route": "static", "url": url,
                                      "status": "failed", "reason": f"{type(exc).__name__}: {exc}"})
            log.error("[PAGE] %s 실패: %s", url, exc)
            log_event(log, logging.ERROR, "document_failed", source_id=f"page:{page_id}", url=url, error=exc)
            continue

        if fetched.status != "ready":
            stats.failed += 1
            if route_results is not None:
                route_results.append({"page_id": page_id, "route": "static", "url": url,
                                      "status": "needs_review", "reason": fetched.reason})
            log.warning("[PAGE] %s: %s, 스킵", url, fetched.reason)
            log_event(log, logging.ERROR, "document_failed", source_id=f"page:{page_id}", url=url, reason=fetched.reason)
            continue

        title, content = fetched.title, fetched.content
        image_ocr_enabled = body_image_ocr_enabled(ROUTES[page_id].options)
        image_text = bool(ROUTES[page_id].options.get("content_from_images", False) and image_ocr_enabled)
        asset_backed = bool((ROUTES[page_id].options.get("allow_asset_only_body")
                             and (fetched.attachments or fetched.body_images))
                            or (fetched.body_images and not image_ocr_enabled))
        if len(content) < MIN_PDF_TEXT_CHARS and not (image_text and fetched.body_images) and not asset_backed:
            stats.failed += 1
            if route_results is not None:
                route_results.append({"page_id": page_id, "route": "static", "url": url,
                                      "status": "needs_review", "reason": "content_too_short"})
            log.warning("[PAGE] %s: 본문 %d자 (너무 짧음), 스킵", url, len(content))
            log_event(log, logging.ERROR, "document_failed", source_id=f"page:{page_id}", url=url, reason="content_too_short")
            continue

        try:
            media_attachments = resolve_static_embedded_pdfs(session, fetched.embedded_pdfs)
        except (requests.RequestException, ValueError) as exc:
            stats.failed += 1
            if route_results is not None:
                route_results.append({"page_id": page_id, "route": "static", "url": url,
                                      "status": "needs_review", "reason": str(exc)})
            log_event(log, logging.ERROR, "document_failed", source_id=f"page:{page_id}",
                      url=url, error=exc)
            continue

        subcategory = static_page_subcategory(page_id)
        slug = make_slug(url, title)
        c_hash = content_hash(content)
        source_content_hash = c_hash
        prev = items_state.get(slug, {})
        output_path = PATHS.document_json(subcategory, slug)
        existing_doc = None
        if output_path.is_file():
            try:
                existing_doc = json.loads(output_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                pass
        try:
            images = download_static_body_images(session, url, subcategory, slug, fetched.body_images,
                                                 cached_images=None if full_resync else (existing_doc or {}).get("images"))
        except ValueError as exc:
            stats.failed += 1
            if route_results is not None:
                route_results.append({"page_id": page_id, "route": "static", "url": url,
                                      "status": "needs_review", "reason": str(exc)})
            log.warning("[PAGE] %s: %s, 스킵", url, exc)
            continue
        reference_links = list(fetched.reference_links)
        if ROUTES[page_id].options.get("download_pdf_attachments") is False:
            reference_links.extend({"text": item["name"], "url": item["url"],
                                    "role": "attachment", "media_id": item["media_id"],
                                    "selected_for_download": False, "followed": False}
                                   for item in media_attachments)
            media_downloads = []
        else:
            media_downloads = media_attachments
        attachment_sources = [*fetched.attachments, *media_downloads] if ROUTES[page_id].download_attachments else []
        attachments = cms_board.save_attachments(
            session, attachment_sources, subcategory, slug, url,
            existing_doc=existing_doc, reuse_existing=not full_resync,
            file_dir=PATHS.attachment_dir(subcategory, slug),
        )
        extract_static_attachment_text(attachments)
        stats.count_attachments(attachments)
        image_hash = hashlib.sha256(json.dumps(
            [(image["source_url"], image["sha256"]) for image in images],
            ensure_ascii=False,
        ).encode("utf-8")).hexdigest()
        attachment_hash = hashlib.sha256(json.dumps(
            [(item["url"], item["sha256"], item["text"]["status"])
             for item in attachments], ensure_ascii=False,
        ).encode("utf-8")).hexdigest()
        reference_link_hash = hashlib.sha256(json.dumps(
            reference_links, ensure_ascii=False, sort_keys=True,
        ).encode("utf-8")).hexdigest()
        if (prev and not full_resync and prev.get("source_content_hash", prev.get("content_hash")) == c_hash
                and prev.get("image_hash") == image_hash
                and prev.get("attachment_hash") == attachment_hash
                and prev.get("reference_link_hash") == reference_link_hash
                and prev.get("parser_version") == STATIC_PARSER_VERSION and output_path.is_file()):
            log.info("[PAGE] %s (unchanged)", title)
            stats.unchanged += 1
            if route_results is not None:
                route_results.append({"page_id": page_id, "route": "static", "url": url,
                                      "status": "unchanged",
                                      "warnings": (existing_doc or {}).get("extraction_warnings", []),
                                      "output": output_path.relative_to(PROJECT_ROOT).as_posix(),
                                      "table_count": fetched.structure["table_count"],
                                      "flow_count": fetched.structure["flow_count"],
                                      "image_count": len(images),
                                      "attachment_count": len(attachments),
                                      "reference_link_count": len(reference_links),
                                      "linked_file_page_ids": list(fetched.linked_file_page_ids)})
            continue

        if image_ocr_enabled:
            recognize_static_body_images(images)
        else:
            for image in images:
                image.update(ocr_status="not_requested", ocr_text="", ocr_blocks=[], ocr_warnings=[])
        reviewed_images = {image.get("sha256"): image.get("visual_extraction")
                           for image in (existing_doc or {}).get("images", [])
                           if image.get("visual_extraction")}
        for image in images:
            review = reviewed_images.get(image.get("sha256"))
            if review and review.get("source_sha256") == image.get("sha256"):
                image["visual_extraction"] = review
        extraction_warnings = list(fetched.warnings)
        if images and image_ocr_enabled:
            extraction_warnings.append("IMAGE_OCR_REVIEW_REQUIRED")
            if any(image["ocr_status"] in {"failed", "empty"} for image in images):
                extraction_warnings.append("IMAGE_OCR_INCOMPLETE")
        if any(not item["downloaded"] for item in attachments):
            extraction_warnings.append("ATTACHMENT_DOWNLOAD_INCOMPLETE")
        if any(item["text"]["status"] == "failed" for item in attachments):
            extraction_warnings.append("ATTACHMENT_TEXT_INCOMPLETE")
        if any(item.get("downloaded") and
               Path(item.get("saved_path") or "").suffix.lower() == ".pdf" and
               item["text"]["characters"] < MIN_PDF_TEXT_CHARS
               for item in attachments):
            extraction_warnings.append("PDF_VISUAL_REVIEW_REQUIRED")
        ocr_sections = []
        if image_text:
            content, ocr_sections = image_text_sections(images)
            c_hash = content_hash(content)
        doc: dict[str, Any] = {
            "slug": slug,
            "title": title,
            "date": datetime.now().strftime("%Y-%m-%d"),
            "url": url,
            "pdf_url": (media_attachments[0]["url"] if media_attachments else
                        f"{BASE_URL}/main/{fetched.linked_file_page_ids[0]}"
                        if fetched.linked_file_page_ids else None),
            "category": ROUTES[page_id].category_path[0],
            "subcategory": subcategory,
            "type": "page",
            "year": None,
            "content": content,
            "content_hash": c_hash,
            "source_content_hash": source_content_hash,
            "attachments": attachments,
            "linked_files": [{"page_id": file_id, "url": f"{BASE_URL}/main/{file_id}"}
                             for file_id in fetched.linked_file_page_ids],
            "reference_links": reference_links,
            "reference_link_hash": reference_link_hash,
            "embedded_pdf_media_ids": [item["media_id"] for item in fetched.embedded_pdfs],
            "structured_content": fetched.structure,
            "images": images,
            "image_count": len(images),
            "image_hash": image_hash,
            "attachment_hash": attachment_hash,
            "image_ocr_version": IMAGE_OCR_VERSION if images and image_ocr_enabled else None,
            "parser_version": STATIC_PARSER_VERSION,
            "extraction_warnings": extraction_warnings,
            "source_site": BASE_URL,
            "crawled_at": datetime.now().isoformat(),
        }
        if "card_count" in fetched.structure:
            doc["card_count"] = fetched.structure["card_count"]
        if ROUTES[page_id].options.get("collect_body_images") is False:
            doc.update(content_source="html_text_cards" if "card_count" in fetched.structure else "html",
                       image_collection="not_requested", image_text_extraction="not_requested",
                       ocr_review_required=False)
        if images and not image_ocr_enabled:
            doc.update(content_source="html_with_original_images", image_text_extraction="not_requested",
                       ocr_review_required=False)
        if image_text:
            doc.update(content_source="image_ocr", image_text_sections=ocr_sections,
                       ocr_review_required=True, html_content=fetched.content)
            if ocr_sections and all(section["text_source"] == "visual_extraction" for section in ocr_sections):
                doc.update(content_source="image_ocr_with_visual_review", ocr_review_required=False)
            doc["structured_content"] = {**fetched.structure, "content": content,
                "sections": [{"title": section["title"], "blocks": [
                    {"type": "image_ocr", "text": section["content"], "image_order": section["image_order"]}]
                } for section in ocr_sections]}
        from scripts.crawlers.main.paths import page_root
        source_html = page_root(PROJECT_ROOT, page_id) / "html" / f"main_{page_id}.html"
        if source_html.is_file():
            doc["source_html"] = source_html.relative_to(PROJECT_ROOT).as_posix()
        saved_path = save_json(doc, subcategory, slug)
        items_state[slug] = {
            "slug": slug,
            "content_hash": c_hash,
            "source_content_hash": source_content_hash,
            "url": url,
            "last_seen_at": datetime.now().isoformat(),
            "parser_version": STATIC_PARSER_VERSION,
            "image_hash": image_hash,
            "attachment_hash": attachment_hash,
            "reference_link_hash": reference_link_hash,
            "subcategory": subcategory,
        }
        outcome = "new" if not prev else "updated"
        setattr(stats, outcome, getattr(stats, outcome) + 1)
        if route_results is not None:
            route_results.append({"page_id": page_id, "route": "static", "url": url,
                                  "status": outcome,
                                  "warnings": extraction_warnings,
                                  "output": saved_path.relative_to(PROJECT_ROOT).as_posix(),
                                  "table_count": fetched.structure["table_count"],
                                  "flow_count": fetched.structure["flow_count"],
                                  "image_count": len(images),
                                  "attachment_count": len(attachments),
                                  "reference_link_count": len(reference_links),
                                  "linked_file_page_ids": list(fetched.linked_file_page_ids)})
        log_event(log, logging.INFO, "document_saved", source_id=f"page:{page_id}", url=url, status=outcome)
        log.info("[PAGE] %s: %s", outcome, title)

    return stats
