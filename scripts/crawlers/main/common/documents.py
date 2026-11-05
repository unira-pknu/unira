'Main website common documents.'

from __future__ import annotations
from pathlib import Path
from scripts.crawlers.common.schema import attachment_error, build_attachment
from scripts.crawlers.main.common.http import fetch
from scripts.crawlers.main.common.storage import PROJECT_ROOT
from typing import Any
import hashlib
import re
import requests


MIN_PDF_TEXT_CHARS = 80


def make_slug(url: str, title: str = "") -> str:
    return hashlib.md5(f"{url}|{title}".encode()).hexdigest()[:12]


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def normalize_title(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"\s*보기\s*$", "", text)
    text = re.sub(r"\(PDF\)\s*$", "", text, flags=re.I).strip()
    return text


def infer_date(year: int | None) -> str:
    if year:
        return f"{year}-01-01"
    return ""


def extract_pdf_text(pdf_path: Path) -> str:
    import fitz

    doc = fitz.open(pdf_path)
    parts: list[str] = []
    try:
        for page in doc:
            text = page.get_text("text") or ""
            text = re.sub(r"\s+", " ", text).strip()
            if text:
                parts.append(text)
    finally:
        doc.close()
    return "\n\n".join(parts).strip()


def download_pdf(session: requests.Session, pdf_url: str, dest: Path) -> dict[str, Any]:
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        resp = fetch(session, pdf_url, stream=True)
    except requests.RequestException as exc:
        return build_attachment(
            index=1,
            name=dest.name,
            url=pdf_url,
            project_root=PROJECT_ROOT,
            error=attachment_error("REQUEST_FAILED", exc, True),
        )

    final_url = resp.url
    content_type = resp.headers.get("Content-Type", "")
    if resp.status_code != 200:
        status_code = resp.status_code
        resp.close()
        return build_attachment(
            index=1,
            name=dest.name,
            url=pdf_url,
            final_url=final_url,
            project_root=PROJECT_ROOT,
            content_type=content_type,
            error=attachment_error(
                f"HTTP_{status_code}",
                f"attachment returned HTTP {status_code}",
                status_code == 429 or status_code >= 500,
            ),
        )

    try:
        with dest.open("wb") as f:
            for chunk in resp.iter_content(chunk_size=1024 * 256):
                if chunk:
                    f.write(chunk)
    except (OSError, requests.RequestException) as exc:
        error_code = "STREAM_FAILED" if isinstance(exc, requests.RequestException) else "SAVE_FAILED"
        return build_attachment(
            index=1,
            name=dest.name,
            url=pdf_url,
            final_url=final_url,
            project_root=PROJECT_ROOT,
            content_type=content_type,
            error=attachment_error(error_code, exc, True),
        )
    finally:
        resp.close()

    return build_attachment(
        index=1,
        name=dest.name,
        url=pdf_url,
        final_url=final_url,
        saved_path=dest,
        downloaded=True,
        project_root=PROJECT_ROOT,
        content_type=content_type,
    )
