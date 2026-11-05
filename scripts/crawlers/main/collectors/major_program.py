"""Collect image-only major-program guides from /main/233-235.

The original image is the source of truth. OCR is stored as reviewable text,
not as verified policy text.
"""

from __future__ import annotations
from bs4 import BeautifulSoup
from datetime import datetime, timezone
from pathlib import Path
from scripts.crawlers.common.body_images import collect_body_images, save_body_images
from scripts.preprocessing.extractors.image_ocr import VERSION, extract_image
from scripts.crawlers.main.paths import page_root
from scripts.crawlers.main.routes import PAGE_SETTINGS, body_image_ocr_enabled
import requests
import shutil


ROOT = Path(__file__).resolve().parents[4]
PAGE_IDS = frozenset({233, 234, 235})
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
DEFAULT_TESSERACT = shutil.which("tesseract") or "C:/Program Files/Tesseract-OCR/tesseract.exe"
LOCAL_TESSDATA = ROOT / ".tools/tessdata"


def parse_major_program_page(html: str, page_id: int) -> tuple[str, list[dict]]:
    if page_id not in PAGE_IDS:
        raise ValueError(f"unverified major-program page: {page_id}")
    soup = BeautifulSoup(html, "lxml")
    title = soup.select_one("#subCont h3.subTitle")
    body = soup.select_one("#subCont .content_wrap")
    if title is None or body is None:
        raise ValueError("major-program content container changed")
    if body.get_text(" ", strip=True):
        raise ValueError("major-program page now has HTML body text")
    wrapped = BeautifulSoup('<div class="sub-content"></div>', "lxml")
    wrapped.select_one(".sub-content").append(BeautifulSoup(str(body), "lxml"))
    candidates = collect_body_images(wrapped)
    if len(candidates) != 1:
        raise ValueError(f"expected one major-program body image, found {len(candidates)}")
    return title.get_text(" ", strip=True), candidates


def collect_major_program(session: requests.Session, page_id: int, *,
                          tesseract: str = DEFAULT_TESSERACT,
                          language: str = "kor+eng",
                          tessdata: str | None = None) -> dict:
    if page_id not in PAGE_IDS:
        raise ValueError(f"unverified major-program page: {page_id}")
    url = f"https://www.pknu.ac.kr/main/{page_id}"
    with session.get(url, timeout=30, stream=True, allow_redirects=False) as response:
        if response.status_code != 200 or response.url != url:
            raise ValueError(f"unexpected major-program response: {response.status_code} {response.url}")
        mime = response.headers.get("Content-Type", "").lower()
        if "text/html" not in mime and "application/xhtml+xml" not in mime:
            raise ValueError(f"unexpected major-program content type: {mime}")
        payload = bytearray()
        for chunk in response.iter_content(16384):
            if len(payload) + len(chunk) > MAX_RESPONSE_BYTES:
                raise ValueError("major-program page exceeds size limit")
            payload.extend(chunk)
        html = payload.decode(response.encoding or "utf-8")
    title, candidates = parse_major_program_page(html, page_id)
    records = save_body_images(candidates, session=session, page_url=url,
                               output_dir=page_root(ROOT, page_id) / "files" / f"main_{page_id}" / "images",
                               project_root=ROOT)
    if len(records) != 1 or records[0]["status"] != "saved":
        raise ValueError(f"major-program image download failed: {records[0].get('error') if records else 'no record'}")
    record = records[0]
    image = {key: record.get(key) for key in (
        "order", "alt", "source_url", "saved_path", "sha256", "size_bytes",
        "width", "height", "content_type")}
    image["ocr_blocks"] = []
    if not body_image_ocr_enabled(PAGE_SETTINGS.get(page_id, {}).get("options")):
        image.update(ocr_status="not_requested", ocr_text="")
        return {"page_id": page_id, "title": title, "url": url,
                "retrieved_at": datetime.now(timezone.utc).isoformat(),
                "status": "collected", "warnings": [], "image_count": 1,
                "ocr_engine": None, "ocr_version": None, "ocr_block_count": 0,
                "images": [image], "content": "", "content_source": "original_images",
                "image_text_extraction": "not_requested"}
    warnings = ["OCR_REVIEW_REQUIRED"]
    try:
        local_models = str(LOCAL_TESSDATA) if tessdata is None and LOCAL_TESSDATA.is_dir() else tessdata
        result = extract_image(ROOT / record["saved_path"], executable=tesseract,
                               language=language, tessdata=local_models,
                               psm=6, restore_spacing=True)
        image["ocr_blocks"] = result["blocks"]
        image["ocr_status"] = result["status"]
        image["spacing_restored_blocks"] = result.get("spacing_restored_blocks", 0)
        warnings.extend(w for w in result["warnings"] if w not in {
            "OCR_REVIEW_REQUIRED", "TABLE_LAYOUT_UNVERIFIED"})
        if not result["blocks"]:
            warnings.append("OCR_EMPTY")
    except Exception as exc:
        image["ocr_status"] = "failed"
        image["ocr_error"] = f"{type(exc).__name__}: {exc}"
        warnings.append("OCR_FAILED")
    return {"page_id": page_id, "title": title, "url": url,
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
            "status": "needs_review", "warnings": sorted(set(warnings)),
            "ocr_engine": "tesseract", "ocr_version": VERSION,
            "ocr_language": language, "ocr_psm": 6, "image_count": 1,
            "ocr_block_count": len(image["ocr_blocks"]), "images": [image],
            "content": "\n".join(block["text"] for block in image["ocr_blocks"])}
