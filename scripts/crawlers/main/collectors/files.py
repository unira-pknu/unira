'Main website collectors files.'

from __future__ import annotations
from scripts.crawlers.common.schema import (
    CrawlStats,
    apply_common_schema,
    build_attachment,
    document_slug,
    now_kst,
    sanitize_attachment_filename,
)
from scripts.crawlers.main.common.documents import MIN_PDF_TEXT_CHARS, extract_pdf_text
from scripts.crawlers.main.common.http import fetch
from scripts.crawlers.main.common.storage import BASE_URL, PATHS, PROJECT_ROOT, save_json
from scripts.crawlers.main.routes import ROUTES
from typing import Any
from urllib.parse import unquote
import hashlib
import re
import requests


MAX_FILE_PAGE_BYTES = 25 * 1024 * 1024


def crawl_file_page(
    session: requests.Session, state: dict[str, Any], page_id: int, full_resync: bool,
) -> tuple[CrawlStats, dict[str, Any]]:
    url = f"{BASE_URL}/main/{page_id}"
    result: dict[str, Any] = {"page_id": page_id, "route": "file", "url": url}
    stats = CrawlStats(discovered=1, requested=1)
    options = ROUTES[page_id].options
    subcategory = options.get("document_subcategory", ROUTES[page_id].category_path[-1])
    try:
        resp = fetch(session, url, stream=True)
        if resp.status_code != 200 or resp.url != url:
            raise ValueError(f"file_response_unverified: HTTP {resp.status_code}, {resp.url}")
        mime = resp.headers.get("Content-Type", "").split(";", 1)[0].lower()
        disposition = resp.headers.get("Content-Disposition", "")
        match = re.search(r'filename="?([^";]+)', disposition, re.I)
        filename = sanitize_attachment_filename(unquote(match.group(1))) if match else f"main-{page_id}.pdf"
        if not filename.lower().endswith(".pdf"):
            raise ValueError("file_name_not_pdf")
        chunks = resp.iter_content(chunk_size=256 * 1024)
        first = next(chunks, b"")
        if mime not in {"application/pdf", "application/octet-stream"} or not first.startswith(b"%PDF-"):
            raise ValueError("file_signature_or_mime_unverified")
        slug = document_slug("pknu_student_life", f"main:{page_id}:file")
        dest = PATHS.attachment_file(subcategory, slug, filename)
        dest.parent.mkdir(parents=True, exist_ok=True)
        temporary = dest.with_name(dest.name + ".part")
        try:
            size = len(first)
            if size > MAX_FILE_PAGE_BYTES:
                raise ValueError("file response exceeds size limit")
            digest = hashlib.sha256()
            with temporary.open("wb") as handle:
                handle.write(first)
                digest.update(first)
                for chunk in chunks:
                    if chunk:
                        size += len(chunk)
                        if size > MAX_FILE_PAGE_BYTES:
                            raise ValueError("file response exceeds size limit")
                        handle.write(chunk)
                        digest.update(chunk)
            current_hash = hashlib.sha256(dest.read_bytes()).hexdigest() if dest.is_file() else None
            if current_hash != digest.hexdigest():
                temporary.replace(dest)
        finally:
            temporary.unlink(missing_ok=True)
        attachment = build_attachment(index=1, name=filename, url=url, final_url=resp.url,
                                      saved_path=dest, downloaded=True, content_type=mime,
                                      project_root=PROJECT_ROOT)
        content = extract_pdf_text(dest)
        doc = apply_common_schema(
            {"url": url, "title": options.get("document_title", filename), "category": ROUTES[page_id].category_path[0],
             "subcategory": subcategory, "content": content,
             "attachments": [attachment], "pdf_url": url},
            source_dataset="pknu_student_life", source_id=f"main:{page_id}:file",
            source_site=BASE_URL, document_type="guide", content_source="pknu_main_pdf",
            metadata={"page_id": page_id, "file_name": filename},
        )
        if len(content) < MIN_PDF_TEXT_CHARS:
            doc["crawl"]["warnings"].append("PDF_TEXT_REQUIRES_REVIEW")
        previous = state.setdefault("items", {}).get(f"file:{page_id}", {})
        outcome = "unchanged" if previous.get("content_hash") == doc["content_hash"] and not full_resync else ("updated" if previous else "new")
        if outcome != "unchanged":
            save_json(doc, subcategory, slug)
        setattr(stats, outcome, getattr(stats, outcome) + 1)
        stats.count_attachments([attachment])
        state["items"][f"file:{page_id}"] = {"slug": slug, "content_hash": doc["content_hash"],
                                               "url": url, "last_seen_at": now_kst()}
        result.update(status=outcome, content_length=len(content), filename=filename,
                      saved_path=attachment["saved_path"], sha256=attachment["sha256"],
                      size_bytes=attachment["size_bytes"], warnings=doc["crawl"]["warnings"])
    except (requests.RequestException, ValueError, OSError, ImportError) as exc:
        stats.failed += 1
        result.update(status="failed", reason=str(exc), retryable=isinstance(exc, requests.RequestException))
    finally:
        if "resp" in locals():
            resp.close()
    return stats, result
