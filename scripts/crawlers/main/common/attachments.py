'Shared attachment downloads, reuse and filename handling.'
from __future__ import annotations
from pathlib import Path
from scripts.crawlers.common.schema import (
    attachment_error,
    build_attachment,
    normalize_attachment,
    sanitize_attachment_filename,
    unique_attachment_filename,
)
from scripts.crawlers.main.common.storage import BASE_URL, PROJECT_ROOT, log
from typing import Any
from urllib.parse import unquote, urlparse
import re
import requests
import time


SOURCE_SITE = BASE_URL
REQUEST_TIMEOUT = 25


def sanitize_filename(filename: str) -> str:
    return sanitize_attachment_filename(filename)


def extract_filename(resp: requests.Response, url: str, fallback_name: str) -> str:
    content_disposition = resp.headers.get("Content-Disposition", "")
    if content_disposition:
        m = re.search(r"filename\*=UTF-8''([^;]+)", content_disposition, re.I)
        if m:
            return sanitize_filename(unquote(m.group(1).strip()))
        m = re.search(r'filename="?([^";]+)"?', content_disposition, re.I)
        if m:
            return sanitize_filename(unquote(m.group(1).strip()))

    parsed = urlparse(resp.url or url)
    basename = Path(parsed.path).name
    if basename:
        return sanitize_filename(unquote(basename))

    if fallback_name:
        return sanitize_filename(fallback_name)

    return "attachment"


def local_path_exists(path_value: str) -> bool:
    if not path_value:
        return False
    path = Path(path_value)
    if path.exists():
        return True
    return (PROJECT_ROOT / path_value).exists()


def reusable_attachments_by_url(existing_doc: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    reusable: dict[str, dict[str, Any]] = {}
    if not existing_doc:
        return reusable

    attachments = existing_doc.get("attachments", [])
    if not isinstance(attachments, list):
        return reusable

    for index, attachment in enumerate(attachments, start=1):
        if not isinstance(attachment, dict):
            continue
        url = str(attachment.get("url") or "").strip()
        saved_path = str(attachment.get("saved_path") or "").strip()
        if url and saved_path and local_path_exists(saved_path):
            reusable[url] = normalize_attachment(attachment, index=index, project_root=PROJECT_ROOT)
    return reusable


def save_attachments(
    session: requests.Session,
    attachments: list[dict[str, Any]],
    category_folder: str,
    slug: str,
    source_page_url: str,
    existing_doc: dict[str, Any] | None = None,
    reuse_existing: bool = True,
    file_dir: Path | None = None,
) -> list[dict[str, Any]]:
    if not attachments:
        return []

    if file_dir is None:
        raise ValueError('file_dir is required')
    file_dir.mkdir(parents=True, exist_ok=True)
    reusable_by_url = reusable_attachments_by_url(existing_doc) if reuse_existing else {}
    results: list[dict[str, Any]] = []
    used_names: set[str] = set()

    for idx, attachment in enumerate(attachments, start=1):
        file_url = str(attachment.get("url") or "").strip()
        if not file_url:
            results.append(
                build_attachment(
                    index=idx,
                    name=attachment.get("name") or f"attachment-{idx}",
                    url="",
                    project_root=PROJECT_ROOT,
                    error=attachment_error("MISSING_URL", "attachment URL is empty", False),
                    legacy={**attachment, "source_page_url": source_page_url, "source_site": SOURCE_SITE},
                )
            )
            continue

        reusable = reusable_by_url.get(file_url)
        if reusable:
            normalized = normalize_attachment(
                {**attachment, **reusable}, index=idx, project_root=PROJECT_ROOT
            )
            results.append(normalized)
            saved_name = Path(str(reusable.get("saved_path", ""))).name
            if saved_name:
                used_names.add(saved_name)
            continue

        try:
            time.sleep(0.2)
            resp = session.get(
                file_url,
                timeout=REQUEST_TIMEOUT,
                stream=True,
                verify=False,
                headers={"Referer": source_page_url or BASE_URL},
            )
        except requests.RequestException as exc:
            log.warning("attachment download failed %s: %s", file_url, exc)
            results.append(
                build_attachment(
                    index=idx,
                    name=attachment.get("name") or f"attachment-{idx}",
                    url=file_url,
                    project_root=PROJECT_ROOT,
                    error=attachment_error("REQUEST_FAILED", exc, True),
                    legacy={**attachment, "source_page_url": source_page_url, "source_site": SOURCE_SITE},
                )
            )
            continue

        content_type = (resp.headers.get("Content-Type", "") or "").lower()
        final_url = resp.url
        if resp.status_code != 200:
            log.warning("attachment HTTP %d %s", resp.status_code, file_url)
            resp.close()
            results.append(
                build_attachment(
                    index=idx,
                    name=attachment.get("name") or f"attachment-{idx}",
                    url=file_url,
                    final_url=final_url,
                    project_root=PROJECT_ROOT,
                    content_type=content_type,
                    error=attachment_error(
                        f"HTTP_{resp.status_code}",
                        f"attachment returned HTTP {resp.status_code}",
                        resp.status_code == 429 or resp.status_code >= 500,
                    ),
                    legacy={**attachment, "source_page_url": source_page_url, "source_site": SOURCE_SITE},
                )
            )
            continue

        filename = extract_filename(resp, file_url, str(attachment.get("name") or ""))
        if "text/html" in content_type or "application/xhtml+xml" in content_type:
            log.info("skip HTML attachment response: %s", file_url)
            resp.close()
            results.append(
                build_attachment(
                    index=idx,
                    name=filename,
                    url=file_url,
                    final_url=final_url,
                    project_root=PROJECT_ROOT,
                    content_type=content_type,
                    error=attachment_error("UNSUPPORTED_CONTENT_TYPE", content_type, False),
                    legacy={**attachment, "source_page_url": source_page_url, "source_site": SOURCE_SITE},
                )
            )
            continue

        if filename.lower().endswith((".html", ".htm", ".shtml")):
            log.info("skip HTML attachment filename: %s", filename)
            resp.close()
            results.append(
                build_attachment(
                    index=idx,
                    name=filename,
                    url=file_url,
                    final_url=final_url,
                    project_root=PROJECT_ROOT,
                    content_type=content_type,
                    error=attachment_error("UNSUPPORTED_FILE_TYPE", Path(filename).suffix, False),
                    legacy={**attachment, "source_page_url": source_page_url, "source_site": SOURCE_SITE},
                )
            )
            continue

        if "." not in filename:
            fallback_ext = Path(urlparse(resp.url).path).suffix
            if fallback_ext:
                filename = f"{filename}{fallback_ext}"

        filename = unique_attachment_filename(filename, used_names)

        from scripts.crawlers.main.paths import attachment_path
        output_file = attachment_path(file_dir, filename, PROJECT_ROOT)
        output_file.parent.mkdir(parents=True, exist_ok=True)
        try:
            with output_file.open("wb") as f:
                for chunk in resp.iter_content(chunk_size=1024 * 256):
                    if chunk:
                        f.write(chunk)
            saved_path = output_file
            downloaded = True
            save_error = None
        except (OSError, requests.RequestException) as exc:
            log.warning("attachment save failed %s: %s", output_file, exc)
            saved_path = None
            downloaded = False
            error_code = "STREAM_FAILED" if isinstance(exc, requests.RequestException) else "SAVE_FAILED"
            save_error = attachment_error(error_code, exc, True)
        finally:
            resp.close()

        results.append(
            build_attachment(
                index=idx,
                name=attachment.get("name") or filename,
                url=file_url,
                final_url=final_url,
                saved_path=saved_path,
                downloaded=downloaded,
                project_root=PROJECT_ROOT,
                content_type=content_type,
                error=save_error,
                legacy={**attachment, "source_page_url": source_page_url, "source_site": SOURCE_SITE},
            )
        )

    return results
