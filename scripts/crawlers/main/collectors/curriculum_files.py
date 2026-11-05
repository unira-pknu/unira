"""Download and preprocess selected PDFs on registered curriculum and guide pages."""

from __future__ import annotations
from bs4 import BeautifulSoup
from pathlib import Path
from scripts.crawlers.common.schema import (
    apply_common_schema,
    attachment_text,
    build_attachment,
    document_slug,
    now_kst,
    sanitize_attachment_filename,
    url_source_id,
    validate_common_document,
)
from scripts.crawlers.common.storage import get_dataset_paths
from scripts.crawlers.main.paths import page_json
from scripts.crawlers.main.common.documents import normalize_title
from scripts.crawlers.main.common.http import resolve_media_pdf_url
from scripts.crawlers.main.routes import CURRICULUM_FILE_PAGE_IDS, ROUTES
from urllib.parse import urljoin, urlsplit
import hashlib
import json
import re
import requests
import shutil
import subprocess
import unicodedata


ROOT = Path(__file__).resolve().parents[4]
PATHS = get_dataset_paths(ROOT, "pknu_student_life")
PAGE_IDS = frozenset(CURRICULUM_FILE_PAGE_IDS)
SUBCATEGORY = "교육과정"
MAX_HTML_BYTES = 2 * 1024 * 1024
MAX_PDF_BYTES = 80 * 1024 * 1024
PDF_PREPROCESSOR_VERSION = 3
OCR_DPI = 180
OCR_LANGUAGE = "kor+eng"


def _validate_pdf_url(file_url: str) -> None:
    parsed = urlsplit(file_url)
    if (parsed.scheme != "https" or parsed.hostname != "www.pknu.ac.kr"
            or not parsed.path.startswith("/upload/")
            or Path(parsed.path).suffix.lower() != ".pdf"):
        raise ValueError(f"unexpected curriculum attachment URL: {file_url}")


def parse_curriculum_attachments(html: str, page_id: int) -> tuple[str, list[dict[str, str]]]:
    if page_id not in PAGE_IDS:
        raise ValueError(f"unverified curriculum page: {page_id}")
    soup = BeautifulSoup(html, "lxml")
    title = soup.select_one("#subCont h3.subTitle")
    body = soup.select_one("#subCont .content_wrap")
    if title is None or body is None:
        raise ValueError("curriculum content container changed")
    page_url = f"https://www.pknu.ac.kr/main/{page_id}"
    options = ROUTES[page_id].options
    title_filter = options.get("attachment_title_contains", "")
    attachments: list[dict[str, str]] = []
    seen: set[str] = set()
    for anchor in body.select('a[download][href], a[href$=".pdf" i]'):
        label = (anchor.get("download") or anchor.get_text(" ", strip=True)).strip()
        if title_filter and title_filter not in label:
            continue
        file_url = urljoin(page_url, (anchor.get("href") or "").strip())
        _validate_pdf_url(file_url)
        if file_url in seen:
            continue
        seen.add(file_url)
        if not label:
            raise ValueError("curriculum attachment has no label")
        filename = sanitize_attachment_filename(
            label if label.lower().endswith(".pdf") else f"{label}.pdf"
        )
        attachments.append({"title": label, "filename": filename, "url": file_url})
    if options.get("include_embedded_pdfs"):
        for viewer in body.select('.uploadPdf[data-id]'):
            label_node = viewer.find_previous_sibling(["li", "ul"])
            if label_node is not None and label_node.name == "ul":
                labels = label_node.find_all("li", recursive=False)
                label_node = labels[-1] if labels else None
            label = normalize_title(label_node.get_text(" ", strip=True)) if label_node else ""
            if title_filter and title_filter not in label:
                continue
            media_id = str(viewer.get("data-id") or "").strip()
            if not label or not media_id.isdigit():
                raise ValueError("embedded PDF label or media ID missing")
            if f"media:{media_id}" in seen:
                continue
            seen.add(f"media:{media_id}")
            attachments.append({"title": label,
                                "filename": sanitize_attachment_filename(f"{label}.pdf"),
                                "media_id": media_id})
    if not attachments or len(attachments) > 20:
        raise ValueError(f"unexpected curriculum attachment count: {len(attachments)}")
    return title.get_text(" ", strip=True), attachments


def _existing_document(path: Path) -> dict | None:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _download_pdf(
    session: requests.Session, page_url: str, candidate: dict[str, str],
    slug: str, existing_doc: dict | None, *, full_resync: bool,
    subcategory: str = SUBCATEGORY,
) -> dict:
    destination = PATHS.attachment_file(subcategory, slug, candidate["filename"])
    old_attachment = (existing_doc.get("attachments") or [{}])[0] if existing_doc else {}
    reusable = (
        not full_resync
        and old_attachment.get("url") == candidate["url"]
        and old_attachment.get("saved_path") == destination.relative_to(ROOT).as_posix()
        and destination.is_file()
    )
    if reusable:
        with destination.open("rb") as handle:
            signature = handle.read(5)
        reusable = (signature == b"%PDF-" and _sha256(destination) == old_attachment.get("sha256"))
    if not reusable:
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(".pdf.part")
        try:
            with session.get(candidate["url"], timeout=(15, 120), stream=True,
                             allow_redirects=False, headers={"Referer": page_url}) as response:
                mime = response.headers.get("Content-Type", "").split(";", 1)[0].lower()
                if (response.status_code != 200 or response.url != candidate["url"]
                        or mime not in {"application/pdf", "application/octet-stream"}):
                    raise ValueError(f"curriculum PDF response unverified: {response.status_code} {response.url} {mime}")
                expected_size = int(response.headers.get("Content-Length") or 0)
                if expected_size > MAX_PDF_BYTES:
                    raise ValueError("curriculum PDF exceeds size limit")
                size = 0
                prefix = bytearray()
                with temporary.open("wb") as handle:
                    for chunk in response.iter_content(256 * 1024):
                        if not chunk:
                            continue
                        size += len(chunk)
                        if size > MAX_PDF_BYTES:
                            raise ValueError("curriculum PDF exceeds size limit")
                        if len(prefix) < 5:
                            prefix.extend(chunk[:5 - len(prefix)])
                        handle.write(chunk)
                if prefix != b"%PDF-" or not size or (expected_size and size != expected_size):
                    raise ValueError("curriculum PDF signature or size mismatch")
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)
    return build_attachment(
        index=1, name=candidate["filename"], url=candidate["url"],
        final_url=candidate["url"], saved_path=destination, downloaded=True,
        content_type="application/pdf", project_root=ROOT,
    )


def _clean_page_text(raw: str, page_number: int, *, ocr: bool) -> str:
    raw = unicodedata.normalize("NFC", raw).replace("\u00a0", " ")
    raw = re.sub(r"[\u200b-\u200d\ufeff\x00-\x08\x0b\x0c\x0e-\x1f]", "", raw)
    lines: list[str] = []
    for original in raw.splitlines():
        line = re.sub(r"[ \t]+", " ", original).strip()
        if ocr:
            line = line.strip(" |_=~")
            if line and not re.search(r"[가-힣A-Za-z0-9]", line):
                continue
        if line and line != (lines[-1] if lines else None):
            lines.append(line)
    while lines and lines[0] == str(page_number):
        lines.pop(0)
    while lines and lines[-1] == str(page_number):
        lines.pop()
    return "\n".join(lines).strip()


def _is_visually_blank(page) -> bool:
    import fitz

    pixels = page.get_pixmap(matrix=fitz.Matrix(0.2, 0.2), alpha=False).samples
    return bool(pixels) and min(pixels) >= 250 and len(set(pixels)) == 1


def _ocr_page(
    page, executable: str, tessdata: Path, *, dpi: int = OCR_DPI,
    psm: int | None = None,
) -> tuple[str, str]:
    import fitz

    zoom = dpi / 72
    wide = page.rect.width / page.rect.height > 1.7
    clips = ([fitz.Rect(
        index * page.rect.width / 4, 0,
        (index + 1) * page.rect.width / 4, page.rect.height,
    ) for index in range(4)] if wide else [page.rect])
    texts: list[str] = []
    for index, clip in enumerate(clips, start=1):
        png = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), clip=clip,
                              alpha=False).tobytes("png")
        command = [executable, "stdin", "stdout", "-l", OCR_LANGUAGE,
                   "--tessdata-dir", str(tessdata), "--psm", str(psm or (6 if wide else 3))]
        process = subprocess.run(command, input=png, capture_output=True,
                                 timeout=120, check=False)
        if process.returncode != 0:
            message = process.stderr.decode("utf-8", "replace").strip()
            raise RuntimeError(f"Tesseract failed on panel {index}: {message}")
        text = process.stdout.decode("utf-8-sig", "replace")
        if text.strip():
            texts.append(f"[패널 {index}]\n{text}" if wide else text)
    return "\n\n".join(texts), "four_vertical_panels" if wide else "full_page"


def _retry_empty_ocr_pages(path: Path, pages: list[dict]) -> list[dict]:
    """Upgrade earlier OCR output without repeating successful pages."""
    import fitz

    executable = shutil.which("tesseract") or "C:/Program Files/Tesseract-OCR/tesseract.exe"
    tessdata = ROOT / ".tools/tessdata"
    with fitz.open(path) as pdf:
        for record in pages:
            if record.get("method") != "ocr" or record.get("content"):
                continue
            page = pdf[record["page"] - 1]
            if _is_visually_blank(page):
                record.update(method="blank", status="empty", warnings=[])
                continue
            try:
                raw, layout = _ocr_page(page, executable, tessdata, dpi=250, psm=6)
                content = _clean_page_text(raw, record["page"], ocr=True)
                record.update(content=content, characters=len(content), layout=layout,
                              status="needs_review" if content else "empty",
                              warnings=["OCR_REVIEW_REQUIRED" if content else "OCR_TEXT_EMPTY"],
                              raw_ocr_text=(record.get("raw_ocr_text") or "") + "\n\n" + raw)
            except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
                record["warnings"] = [f"OCR_FAILED: {type(exc).__name__}: {exc}"]
    return pages


def _pdf_pages(path: Path) -> list[dict]:
    import fitz

    pages: list[dict] = []
    executable = shutil.which("tesseract") or "C:/Program Files/Tesseract-OCR/tesseract.exe"
    tessdata = ROOT / ".tools/tessdata"
    with fitz.open(path) as pdf:
        for number, page in enumerate(pdf, start=1):
            raw = page.get_text("text") or ""
            content = _clean_page_text(raw, number, ocr=False)
            record = {"page": number, "content": content, "characters": len(content),
                      "method": "embedded_text" if content else "blank",
                      "status": "success" if content else "empty", "warnings": []}
            if len(content) < 80 and page.get_images():
                if _is_visually_blank(page):
                    pages.append(record)
                    continue
                if not Path(executable).is_file() and not shutil.which(executable):
                    record["warnings"].append("OCR_EXECUTABLE_MISSING")
                elif not all((tessdata / f"{name}.traineddata").is_file()
                             for name in OCR_LANGUAGE.split("+")):
                    record["warnings"].append("OCR_LANGUAGE_DATA_MISSING")
                else:
                    try:
                        raw_ocr, layout = _ocr_page(page, executable, tessdata)
                        ocr_text = _clean_page_text(raw_ocr, number, ocr=True)
                        if not ocr_text:
                            retry_raw, layout = _ocr_page(
                                page, executable, tessdata, dpi=250, psm=6,
                            )
                            raw_ocr += "\n\n" + retry_raw
                            ocr_text = _clean_page_text(retry_raw, number, ocr=True)
                        record.update(content=ocr_text or content,
                                      characters=len(ocr_text or content),
                                      method="ocr", layout=layout, raw_ocr_text=raw_ocr,
                                      status="needs_review" if ocr_text else "empty")
                        record["warnings"].append(
                            "OCR_REVIEW_REQUIRED" if ocr_text else "OCR_TEXT_EMPTY"
                        )
                    except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
                        record["warnings"].append(f"OCR_FAILED: {type(exc).__name__}: {exc}")
            pages.append(record)
    return pages


def collect_curriculum_files(
    session: requests.Session, page_id: int, *, full_resync: bool = False,
) -> dict:
    if page_id not in PAGE_IDS:
        raise ValueError(f"unverified curriculum page: {page_id}")
    page_url = f"https://www.pknu.ac.kr/main/{page_id}"
    with session.get(page_url, timeout=30, stream=True, allow_redirects=False) as response:
        mime = response.headers.get("Content-Type", "").lower()
        if (response.status_code != 200 or response.url != page_url
                or "text/html" not in mime):
            raise ValueError(f"curriculum page response unverified: {response.status_code} {response.url}")
        payload = bytearray()
        for chunk in response.iter_content(16384):
            if len(payload) + len(chunk) > MAX_HTML_BYTES:
                raise ValueError("curriculum page exceeds size limit")
            payload.extend(chunk)
        html = payload.decode(response.encoding or "utf-8")
    page_title, candidates = parse_curriculum_attachments(html, page_id)
    route = ROUTES[page_id]
    subcategory = route.options.get("document_subcategory", route.category_path[-1])
    for candidate in candidates:
        if candidate.get("media_id"):
            candidate["url"] = resolve_media_pdf_url(session, candidate["media_id"])
            _validate_pdf_url(candidate["url"])
    candidates = list({candidate["url"]: candidate for candidate in candidates}.values())

    pending: list[tuple[Path, dict]] = []
    summaries: list[dict] = []
    for index, candidate in enumerate(candidates, start=1):
        source_id = f"main:{page_id}:attachment:{url_source_id(candidate['url'])}"
        slug = document_slug("pknu_student_life", source_id)
        doc_path = PATHS.document_json(subcategory, slug, kind="attachments")
        existing_doc = _existing_document(doc_path)
        attachment = _download_pdf(
            session, page_url, candidate, slug, existing_doc, full_resync=full_resync,
            subcategory=subcategory,
        )
        old_attachment = (existing_doc.get("attachments") or [{}])[0] if existing_doc else {}
        if (not full_resync and existing_doc
                and existing_doc.get("preprocessor_version") == PDF_PREPROCESSOR_VERSION
                and old_attachment.get("sha256") == attachment["sha256"]
                and isinstance(existing_doc.get("pages"), list)):
            pages = existing_doc["pages"]
            warnings = list((existing_doc.get("crawl") or {}).get("warnings") or [])
        elif (not full_resync and existing_doc
              and existing_doc.get("preprocessor_version") == 2
              and old_attachment.get("sha256") == attachment["sha256"]
              and isinstance(existing_doc.get("pages"), list)):
            pages = _retry_empty_ocr_pages(
                ROOT / attachment["saved_path"], existing_doc["pages"],
            )
            warnings = sorted({warning for page in pages for warning in page["warnings"]})
            if not any(page["content"] for page in pages):
                warnings.append("PDF_TEXT_EMPTY")
        else:
            try:
                pages = _pdf_pages(ROOT / attachment["saved_path"])
                warnings = sorted({warning for page in pages for warning in page["warnings"]})
                if not any(page["content"] for page in pages):
                    warnings.append("PDF_TEXT_EMPTY")
            except Exception as exc:
                pages = []
                warnings = [f"PDF_TEXT_EXTRACTION_FAILED: {type(exc).__name__}: {exc}"]
        content = "\n\n".join(
            f"[{page['page']}쪽]\n{page['content']}" for page in pages if page["content"]
        )
        attachment["text"] = attachment_text(
            "success" if content else "failed" if warnings and warnings[0].startswith("PDF_TEXT_EXTRACTION_FAILED") else "skipped",
            content=content or None,
            extractor=("pymupdf+tesseract" if any(page.get("method") == "ocr" for page in pages)
                       else "pymupdf") if pages else None,
            error=warnings[0] if not content and warnings else None,
        )
        doc = apply_common_schema(
            {"url": page_url, "pdf_url": candidate["url"], "title": candidate["title"],
             "category": route.category_path[0], "subcategory": subcategory,
             "tags": [subcategory, page_title], "content": content or candidate["title"],
             "attachments": [attachment], "pages": pages, "page_count": len(pages),
             "preprocessor_version": PDF_PREPROCESSOR_VERSION,
             "ocr_page_count": sum(page.get("method") == "ocr" for page in pages)},
            source_dataset="pknu_student_life", source_id=source_id,
            source_site="https://www.pknu.ac.kr", document_type="guide",
            content_source="pknu_main_curriculum_pdf",
            metadata={"page_id": page_id, "page_title": page_title,
                      "file_url": candidate["url"], "attachment_index": index},
        )
        if warnings:
            doc["crawl"]["status"] = "needs_review"
            doc["crawl"]["warnings"] = warnings
        errors = validate_common_document(doc, ROOT)
        if errors:
            raise ValueError("invalid curriculum document: " + "; ".join(errors))
        pending.append((doc_path, doc))
        summaries.append({
            "title": candidate["title"], "file_url": candidate["url"],
            "document_path": doc_path.relative_to(ROOT).as_posix(),
            "saved_path": attachment["saved_path"], "size_bytes": attachment["size_bytes"],
            "sha256": attachment["sha256"], "page_count": len(pages),
            "text_page_count": sum(bool(page["content"]) for page in pages),
            "ocr_page_count": doc["ocr_page_count"], "text_characters": len(content),
            "text_status": attachment["text"]["status"],
            "text_preview": content[:500], "page_text_field": "pages[].content",
            "status": doc["crawl"]["status"],
        })
    for path, doc in pending:
        previous = _existing_document(path)
        if (previous and previous.get("content_hash") == doc["content_hash"]
                and previous.get("preprocessor_version") == PDF_PREPROCESSOR_VERSION
                and (previous.get("attachments") or [{}])[0].get("sha256")
                == doc["attachments"][0]["sha256"]
                and previous.get("title") == doc["title"]):
            continue
        _write_json(path, doc)

    manifest = {
        "page_id": page_id, "source_url": page_url, "title": page_title,
        "category": route.category_path[0], "subcategory": subcategory,
        "retrieved_at": now_kst(), "status": "needs_review" if any(
            item["status"] != "success" for item in summaries) else "success",
        "attachment_count": len(summaries), "attachments": summaries,
    }
    target = page_json(ROOT, page_id)
    _write_json(target, manifest)
    manifest["output"] = target.relative_to(ROOT).as_posix()
    return manifest
