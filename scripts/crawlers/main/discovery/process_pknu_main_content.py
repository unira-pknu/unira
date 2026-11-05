"""Inspect selected /main/<id> pages by content type without changing crawl state.

Uses saved page HTML. Only explicitly discovered image, iframe, AJAX, and
attachment URLs are requested. Output remains reviewable and is not RAG input.

python -m scripts.crawlers.main.discovery.process_pknu_main_content --page-ids 16 102 472 459 110
python -m scripts.crawlers.main.discovery.process_pknu_main_content --page-ids 459 --language kor --ocr-psm 11 --restore-ocr-spacing --output files/pknu_main/_discovery/ocr_459_recheck.json
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup, Tag

from scripts.crawlers.common.body_images import collect_body_images, save_body_images
from scripts.crawlers.main.common.http import build_session
from scripts.crawlers.main.routes import body_image_ocr_enabled
from scripts.crawlers.main.collectors.tuition import collect_main_102_tuition
from scripts.crawlers.main.collectors.organization import collect_main_533_org
from scripts.preprocessing.extractors.image_ocr import VERSION, extract_image, require_ocr_languages
from scripts.preprocessing.body_image_layout import enrich_entry


ROOT = Path(__file__).resolve().parents[4]
DISCOVERY = ROOT / "files/pknu_main/_discovery"
ALLOWED_HOSTS = {"www.pknu.ac.kr", "irumi.pknu.ac.kr"}
FILE_EXTENSIONS = (".pdf", ".hwp", ".hwpx", ".doc", ".docx", ".xls", ".xlsx", ".zip")
MAX_PROBE_BYTES = 64 * 1024
MAX_HTML_BYTES = 2 * 1024 * 1024


def text(node: Tag | None) -> str:
    return re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip() if node else ""


def safe_url(href: str, page_url: str) -> str | None:
    href = href.strip()
    if not href or href.startswith("#"):
        return None
    try:
        url = urljoin(page_url, href)
        parsed = urlparse(url)
        return url if parsed.scheme in {"http", "https"} and parsed.hostname in ALLOWED_HOSTS else None
    except ValueError:
        return None


def body_root(soup: BeautifulSoup) -> Tag | None:
    sub = soup.select_one("#subCont")
    return (sub.select_one(".content_wrap, #loadArea, .container-fluid") if sub else None)


def _direct_rows(table: Tag) -> list[Tag]:
    return [row for row in table.find_all("tr") if row.find_parent("table") is table]


def extract_tables(root: Tag | None) -> list[dict]:
    if root is None:
        return []
    results = []
    for index, table in enumerate(root.select("table"), 1):
        # CMS layout tables can wrap the actual data table. Keep leaf tables.
        if table.find("table") is not None:
            continue
        occupied: set[tuple[int, int]] = set()
        rows = []
        warnings = []
        for row_index, tr in enumerate(_direct_rows(table)):
            cells = []
            column = 0
            for cell in tr.find_all(["th", "td"], recursive=False):
                while (row_index, column) in occupied:
                    column += 1
                try:
                    rowspan = int(cell.get("rowspan") or 1)
                    colspan = int(cell.get("colspan") or 1)
                except ValueError:
                    rowspan = colspan = 0
                if not 1 <= rowspan <= 100 or not 1 <= colspan <= 100:
                    warnings.append("INVALID_TABLE_SPAN")
                    continue
                if any((r, c) in occupied for r in range(row_index, row_index + rowspan)
                       for c in range(column, column + colspan)):
                    warnings.append("OVERLAPPING_TABLE_SPAN")
                    continue
                for r in range(row_index, row_index + rowspan):
                    for c in range(column, column + colspan):
                        occupied.add((r, c))
                cells.append({"row": row_index, "column": column, "rowspan": rowspan,
                              "colspan": colspan, "text": text(cell), "header": cell.name == "th"})
                column += colspan
            rows.append({"row": row_index, "cells": cells})
        height = len(rows)
        width = max((c + 1 for _, c in occupied), default=0)
        if any(r >= height for r, _ in occupied):
            warnings.append("SPAN_EXCEEDS_TABLE_ROWS")
        if not occupied or any((r, c) not in occupied for r in range(height) for c in range(width)):
            warnings.append("IRREGULAR_TABLE_GRID")
        caption = text(table.find("caption", recursive=False))
        heading = table.find_previous(["h3", "h4", "h5"])
        title = text(heading) if heading and heading in root.descendants else None
        results.append({"table_index": index, "title": title, "caption": caption or None,
                        "rows": rows, "row_count": height, "column_count": width,
                        "status": "needs_review" if warnings else "structured",
                        "warnings": sorted(set(warnings))})
    return results


def _read_limited(response, limit: int) -> bytes:
    data = bytearray()
    for chunk in response.iter_content(8192):
        if len(data) + len(chunk) > limit:
            raise ValueError("response exceeds inspection limit")
        data.extend(chunk)
    return bytes(data)


def discover_dynamic(soup: BeautifulSoup, page_url: str) -> list[dict]:
    sub = soup.select_one("#subCont")
    if sub is None:
        return []
    sources = []
    for frame in sub.select("iframe[src]"):
        sources.append({"kind": "iframe", "url": safe_url(frame["src"], page_url),
                        "evidence": frame.get("title") or frame.get("src")})
    # Only a literal URL loaded into an element that exists in the page is
    # accepted. Arbitrary JavaScript is never evaluated or guessed.
    # Some CMS templates place the loader in <head>, outside #subCont.
    # Require its target element to exist in the body before accepting it.
    for script in soup.select("script"):
        for match in re.finditer(r'\$\(["\']#([\w-]+)["\']\)\.load\(["\']([^"\']+)["\']', script.string or ""):
            if sub.select_one(f'#{match.group(1)}'):
                sources.append({"kind": "ajax_html", "url": safe_url(match.group(2), page_url),
                                "evidence": f"#{match.group(1)}.load"})
    for node in sub.select(".getOrgMngList[data-id]"):
        sources.append({"kind": "script_widget", "url": None,
                        "evidence": f"getOrgMngList data-id={node.get('data-id')}"})
    unique = {}
    for source in sources:
        unique[(source["kind"], source["url"], source["evidence"])] = source
    return list(unique.values())


def inspect_dynamic(soup: BeautifulSoup, page_url: str, session) -> list[dict]:
    results = []
    for source in discover_dynamic(soup, page_url):
        result = {**source, "status": "needs_review", "warnings": []}
        if not source["url"]:
            result["warnings"].append("DYNAMIC_SOURCE_UNVERIFIED")
        else:
            try:
                with session.get(source["url"], timeout=20, stream=True,
                                 headers={"Referer": page_url}) as response:
                    result.update(http_status=response.status_code, final_url=response.url,
                                  content_type=response.headers.get("Content-Type", ""))
                    if response.status_code != 200 or not safe_url(response.url, page_url):
                        result["warnings"].append("DYNAMIC_SOURCE_UNAVAILABLE")
                    elif "html" not in result["content_type"].lower():
                        result["warnings"].append("DYNAMIC_RESPONSE_NOT_HTML")
                    else:
                        payload = _read_limited(response, MAX_HTML_BYTES)
                        html = BeautifulSoup(payload, "lxml")
                        content = text(html)
                        result["text_length"] = len(content)
                        result["text"] = content[:20000]
                        result["tables"] = extract_tables(html)
                        result["table_count"] = len(result["tables"])
                        result["sample"] = content[:160]
                        if len(content) > 20000:
                            result["warnings"].append("DYNAMIC_TEXT_TRUNCATED")
                        if any(table["status"] == "needs_review" for table in result["tables"]):
                            result["warnings"].append("DYNAMIC_TABLE_UNVERIFIED")
                        if result["text_length"] >= 40 and not result["warnings"] and not re.search(
                                r"로그인|접근.*제한|권한.*없|login required|데이타가 없습니다|데이터가 없습니다",
                                result["sample"], re.I):
                            result["status"] = "source_verified"
                        else:
                            result["warnings"].append("DYNAMIC_DATA_UNVERIFIED")
            except Exception as exc:
                result.update(error=f"{type(exc).__name__}: {exc}")
                result["warnings"].append("DYNAMIC_SOURCE_UNAVAILABLE")
        results.append(result)
    return results


def discover_attachments(root: Tag | None, page_url: str) -> list[dict]:
    if root is None:
        return []
    candidates = []
    for node in root.select(".uploadPdf[data-id]"):
        media_id = node.get("data-id", "")
        if media_id.isdigit():
            candidates.append({"kind": "media_id", "media_id": media_id, "url": None})
    for anchor in root.select("a[href]"):
        url = safe_url(anchor.get("href", ""), page_url)
        if url and (urlparse(url).path.lower().endswith(FILE_EXTENSIONS)
                    or "boarddownload.do" in urlparse(url).path.lower()):
            candidates.append({"kind": "link", "url": url, "label": text(anchor)})
    return candidates


def _file_signature(first: bytes) -> str | None:
    if first.startswith(b"%PDF-"):
        return "pdf"
    if first.startswith(b"PK\x03\x04"):
        return "zip_container"
    if first.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        return "ole_container"
    return None


def inspect_attachments(root: Tag | None, page_url: str, session) -> list[dict]:
    results = []
    for candidate in discover_attachments(root, page_url):
        result = {**candidate, "status": "needs_review", "warnings": []}
        try:
            if candidate["kind"] == "media_id":
                with session.post("https://www.pknu.ac.kr/common/getMdaId.do",
                                  data={"no": candidate["media_id"]}, timeout=20,
                                  headers={"Referer": page_url}) as response:
                    result["resolver_status"] = response.status_code
                    response.raise_for_status()
                    path = str(response.json().get("response") or "")
                    url = safe_url("/upload/" + path.lstrip("/"), page_url) if path else None
                    if not url or not urlparse(url).path.startswith("/upload/"):
                        raise ValueError("invalid media resolver path")
                    result["url"] = url
            with session.get(result["url"], timeout=20, stream=True,
                             headers={"Referer": page_url}) as response:
                result.update(http_status=response.status_code, final_url=response.url,
                              content_type=response.headers.get("Content-Type", ""),
                              content_disposition=response.headers.get("Content-Disposition", ""))
                first = next(response.iter_content(MAX_PROBE_BYTES), b"")
                result["signature"] = _file_signature(first)
                result["first_bytes_sha256"] = hashlib.sha256(first).hexdigest() if first else None
                mime = result["content_type"].lower()
                if response.status_code != 200:
                    result["warnings"].append("ATTACHMENT_RESPONSE_UNAVAILABLE")
                elif not safe_url(response.url, page_url):
                    result["warnings"].append("ATTACHMENT_REDIRECT_UNVERIFIED")
                elif first.lstrip().lower().startswith((b"<!doctype html", b"<html", b"<script")) or "text/html" in mime:
                    result["warnings"].append("ATTACHMENT_IS_HTML")
                elif result["signature"]:
                    result["status"] = "file_verified"
                else:
                    result["warnings"].append("ATTACHMENT_FORMAT_UNVERIFIED")
        except Exception as exc:
            result.update(error=f"{type(exc).__name__}: {exc}")
            result["warnings"].append("ATTACHMENT_REQUEST_FAILED")
        results.append(result)
    return results


def compact_main_image_ocr(entry: dict) -> None:
    """Keep source OCR once while making the review-facing result concise."""
    for image in entry.get("images", []):
        if image.get("ocr_storage") == "compact_v1":
            continue
        result = image.get("result")
        grouping = image.get("ocr_regions")
        if not isinstance(result, dict) or not isinstance(grouping, dict):
            continue
        regions = result.get("regions")
        blocks = image.get("blocks")
        if not isinstance(regions, list) or not isinstance(blocks, list):
            continue
        # The raw blocks remain available for layout classification and
        # debugging. A second paragraph copy would expose the same OCR noise.
        result["paragraphs"] = []
        image.pop("ocr_text", None)
        grouping["region_count"] = len(regions)
        grouping.pop("regions", None)
        unassigned = grouping.pop("unassigned_blocks", [])
        counts = Counter(item.get("reason", "UNKNOWN") for item in unassigned)
        grouping["unassigned_count"] = len(unassigned)
        grouping["unassigned_reason_counts"] = dict(counts)
        grouping["unassigned_exception_indices"] = {
            reason: [item["block_index"] for item in unassigned
                     if item.get("reason") == reason]
            for reason in counts if reason != "NO_VERIFIED_REGION"
        }
        for region in regions:
            if not isinstance(region, dict):
                continue
            for field in ("source_block_text", "crop_ocr_raw_text",
                          "first_line_ocr_raw_text"):
                region.pop(field, None)
        image["ocr_storage"] = "compact_v1"
        # Put the useful output first; retain raw blocks at the end for audit.
        ordered = {key: image[key] for key in
                   ("order", "saved_path", "sha256", "status", "ocr_storage", "layout_type",
                    "layout_confidence", "extractor", "warnings", "result",
                    "ocr_regions") if key in image}
        ordered.update((key, value) for key, value in image.items()
                       if key not in ordered and key != "blocks")
        ordered["blocks"] = blocks
        image.clear()
        image.update(ordered)


def inspect_images(root: Tag | None, page_id: int, page_url: str, session,
                   *, tesseract: str, language: str, tessdata: str | None,
                   ocr_psm: int, restore_ocr_spacing: bool) -> dict:
    if root is None:
        return {"status": "needs_review", "images": [], "warnings": ["BODY_NOT_FOUND"]}
    # The existing collector requires a body selector, so wrap only the
    # verified content region. Navigation and page chrome never enter OCR.
    wrapped = BeautifulSoup('<div class="sub-content"></div>', "lxml")
    wrapped.select_one(".sub-content").append(BeautifulSoup(str(root), "lxml"))
    candidates = collect_body_images(wrapped)
    output = ROOT / "files/pknu_main/_discovery"
    target = ROOT / f"files/pknu_main/_derived/pknu_main/preprocessed/body_images/main_{page_id}.json"
    old = json.loads(target.read_text(encoding="utf-8")) if target.exists() else {}
    prior = old.get("images", [])
    storage_fields = {"order", "alt", "source_kind", "source_url", "source_page_url",
                      "saved_path", "sha256", "size_bytes", "status", "error", "final_url",
                      "width", "height", "content_type"}
    reusable = (len(prior) == len(candidates) and bool(candidates)
                and all(not c["src"].lower().startswith("data:") for c in candidates))
    if reusable:
        for candidate, old_record in zip(candidates, prior):
            try:
                image_path = (ROOT / old_record["saved_path"]).resolve()
                image_path.relative_to(output.resolve())
                reusable = (old_record.get("source_url") == safe_url(candidate["src"], page_url)
                            and hashlib.sha256(image_path.read_bytes()).hexdigest() == old_record["sha256"])
            except (KeyError, OSError, TypeError, ValueError):
                reusable = False
            if not reusable:
                break
    records = ([{**{key: value for key, value in record.items() if key in storage_fields},
                 "status": "saved", "error": None}
                for record in prior] if reusable else
               save_body_images(candidates, session=session, page_url=page_url,
                                output_dir=output / "images", project_root=ROOT))
    if not body_image_ocr_enabled():
        entry = {"url": page_url, "status": "collected" if records and all(
                 record.get("status") == "saved" for record in records) else "failed",
                 "images": [{**record, "ocr_status": "not_requested", "blocks": []} for record in records],
                 "image_text_extraction": "not_requested", "processed_at": datetime.now(timezone.utc).isoformat()}
        if candidates and not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(entry, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"status": entry["status"], "image_count": len(candidates),
                "saved_count": sum(record.get("status") == "saved" for record in records),
                "ocr_block_count": 0, "ocr_region_count": 0, "spacing_restored_blocks": 0,
                "ocr_path": None, "image_text_extraction": "not_requested", "images": entry["images"]}
    entry = {"source_path": f"files/pknu_main/_discovery/raw/{page_id:04}.txt",
             "url": page_url, "images": [], "processed_at": datetime.now(timezone.utc).isoformat(),
             "ocr_version": VERSION, "language": language, "ocr_psm": ocr_psm,
             "restore_ocr_spacing": restore_ocr_spacing,
             "status": "needs_review",
             "tessdata_dir": (Path(tessdata).resolve().relative_to(ROOT).as_posix()
                              if tessdata and Path(tessdata).resolve().is_relative_to(ROOT) else tessdata),
             "ocr_model_sha256": ({model: hashlib.sha256((Path(tessdata) / f"{model}.traineddata").read_bytes()).hexdigest()
                                   for model in language.split('+')} if tessdata else None)}
    for record in records:
        if record.get("status") == "saved":
            digest = record["sha256"]
            image_path = ROOT / record["saved_path"]
            try:
                if hashlib.sha256(image_path.read_bytes()).hexdigest() != digest:
                    raise ValueError("image hash mismatch")
                record.update(extract_image(image_path, executable=tesseract,
                                            language=language, tessdata=tessdata,
                                            psm=ocr_psm,
                                            restore_spacing=restore_ocr_spacing,
                                            group_regions=True))
            except Exception as exc:
                record.update(status="failed", ocr_error=f"{type(exc).__name__}: {exc}")
        entry["images"].append(record)
    if not candidates or any(r.get("status") in {"failed", "empty"} for r in entry["images"]):
        entry["status"] = "partial_failure" if any(r.get("blocks") for r in entry["images"]) else "failed"
    enrich_entry(entry)
    compact_main_image_ocr(entry)
    if candidates:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(entry, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"status": entry["status"], "image_count": len(candidates),
            "saved_count": sum(r.get("saved_path") is not None for r in entry["images"]),
            "ocr_block_count": sum(len(r.get("blocks", [])) for r in entry["images"]),
            "ocr_region_count": sum(len(r.get("result", {}).get("regions", []))
                                    for r in entry["images"]),
            "spacing_restored_blocks": sum(r.get("spacing_restored_blocks", 0)
                                           for r in entry["images"]),
            "ocr_path": target.relative_to(ROOT).as_posix() if candidates else None,
            "images": [{"sha256": r.get("sha256"), "saved_path": r.get("saved_path"),
                        "status": r.get("status"), "warnings": r.get("warnings", []),
                        "error": r.get("error") or r.get("ocr_error")}
                       for r in entry["images"]]}


def process_page(page_id: int, page: dict, session, *, tesseract: str,
                 language: str, tessdata: str | None, ocr_psm: int,
                 restore_ocr_spacing: bool) -> dict:
    raw_path = DISCOVERY / "raw" / f"{page_id:04}.txt"
    result = {"page_id": page_id, "url": page["url"], "title": page["title"],
              "source_path": raw_path.relative_to(ROOT).as_posix(), "types": {},
              "status": "needs_review"}
    if not raw_path.is_file():
        result["warnings"] = ["SAVED_RESPONSE_MISSING"]
        return result
    soup = BeautifulSoup(raw_path.read_text(encoding="utf-8"), "lxml")
    root = body_root(soup)
    if root is None:
        result["warnings"] = ["BODY_NOT_FOUND"]
        return result
    if page_id == 102:
        frame = root.select_one('iframe[title="계열별 등록금 조회"][src]')
        iframe_url = safe_url(frame.get("src", ""), page["url"]) if frame else None
        result["types"]["tuition"] = collect_main_102_tuition(
            session, page["url"], iframe_url or "")
        result["status"] = result["types"]["tuition"]["status"]
        return result
    if page_id == 533:
        result["types"]["organization"] = collect_main_533_org(
            session, page["url"], str(soup))
        result["status"] = ("processed" if result["types"]["organization"]["status"] == "collected"
                            else "needs_review")
        return result
    tables = extract_tables(root)
    if tables:
        result["types"]["table"] = {"status": "needs_review" if any(
            t["status"] == "needs_review" for t in tables) else "structured",
            "tables": tables}
    dynamic = inspect_dynamic(soup, page["url"], session)
    if dynamic:
        result["types"]["dynamic"] = {"status": "needs_review" if any(
            d["status"] == "needs_review" for d in dynamic) else "source_verified",
            "sources": dynamic}
    attachments = inspect_attachments(root, page["url"], session)
    if attachments:
        result["types"]["attachment"] = {"status": "needs_review" if any(
            a["status"] == "needs_review" for a in attachments) else "file_verified",
            "candidates": attachments}
    if page["page_type"] == "image_only":
        result["types"]["image"] = inspect_images(root, page_id, page["url"], session,
                                                      tesseract=tesseract, language=language,
                                                      tessdata=tessdata, ocr_psm=ocr_psm,
                                                      restore_ocr_spacing=restore_ocr_spacing)
    if result["types"] and all(v["status"] in {"structured", "source_verified", "file_verified"}
                               for v in result["types"].values()):
        result["status"] = "processed"
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--page-ids", nargs="+", type=int, required=True)
    parser.add_argument("--output", type=Path, default=DISCOVERY / "content_type_results.json")
    parser.add_argument("--tesseract", default=shutil.which("tesseract") or
                        "C:/Program Files/Tesseract-OCR/tesseract.exe")
    parser.add_argument("--language", default="kor+eng")
    parser.add_argument("--ocr-psm", type=int, choices=(6, 11), default=6,
                        help="6: one text block; 11: sparse infographic text")
    parser.add_argument("--restore-ocr-spacing", action="store_true",
                        help="Use a second plain-text pass to restore Tesseract's Korean word spacing")
    local_tessdata = ROOT / ".tools/tessdata"
    parser.add_argument("--tessdata", default=str(local_tessdata) if local_tessdata.is_dir() else None)
    args = parser.parse_args()
    catalog = json.loads((DISCOVERY / "page_catalog.json").read_text(encoding="utf-8"))
    pages = {p["page_id"]: p for p in catalog["pages"]}
    unknown = set(args.page_ids) - pages.keys()
    if unknown:
        parser.error(f"Page IDs not in catalog: {sorted(unknown)}")
    if body_image_ocr_enabled() and any(pages[page_id]["page_type"] == "image_only" for page_id in args.page_ids):
        try:
            require_ocr_languages(args.tesseract, args.language, args.tessdata)
        except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
            parser.error(str(exc))
    results = []
    with build_session() as session:
        for page_id in dict.fromkeys(args.page_ids):
            results.append(process_page(page_id, pages[page_id], session,
                                        tesseract=args.tesseract, language=args.language,
                                        tessdata=args.tessdata, ocr_psm=args.ocr_psm,
                                        restore_ocr_spacing=args.restore_ocr_spacing))
    counts = Counter(kind for row in results for kind in row["types"])
    review = []
    for row in results:
        if row["status"] == "processed":
            continue
        pending = {}
        for kind, value in row["types"].items():
            if value["status"] not in {"needs_review", "failed", "partial_failure"}:
                continue
            items = value.get("sources") or value.get("candidates") or value.get("images") or []
            pending[kind] = sorted(set(value.get("warnings", [])) | {
                w for item in items for w in item.get("warnings", [])})
        review.append({"page_id": row["page_id"], "types": pending,
                       "warnings": row.get("warnings", [])})
    details = {
        "tables": sum(len(row["types"].get("table", {}).get("tables", [])) for row in results),
        "tables_with_spans": sum(any(cell["rowspan"] > 1 or cell["colspan"] > 1
                               for line in table["rows"] for cell in line["cells"])
                               for row in results for table in row["types"].get("table", {}).get("tables", [])),
        "dynamic_sources_verified": sum(source["status"] == "source_verified" for row in results
                                        for source in row["types"].get("dynamic", {}).get("sources", [])),
        "images_saved": sum(row["types"].get("image", {}).get("saved_count", 0) for row in results),
        "image_regions": sum(row["types"].get("image", {}).get("ocr_region_count", 0)
                             for row in results),
        "attachments_verified": sum(file["status"] == "file_verified" for row in results
                                    for file in row["types"].get("attachment", {}).get("candidates", [])),
        "tuition_rows": sum(row["types"].get("tuition", {}).get("counts", {}).get("tuition", 0)
                            for row in results),
        "tuition_departments": sum(row["types"].get("tuition", {}).get("counts", {}).get("departments", 0)
                                   for row in results),
        "tuition_installments": sum(row["types"].get("tuition", {}).get("counts", {}).get("installments", 0)
                                    for row in results),
        "verified_department_tuition": sum(row["types"].get("tuition", {}).get("counts", {}).get(
            "verified_department_tuition", 0) for row in results),
        "organization_staff": sum(row["types"].get("organization", {}).get("staff_count", 0)
                                  for row in results),
    }
    report = {"generated_at": datetime.now(timezone.utc).isoformat(),
              "scope": "selected saved /main pages only; no crawler state write",
              "counts": dict(counts), "detail_counts": details,
              "needs_review": review, "pages": results}
    target = (ROOT / args.output).resolve() if not args.output.is_absolute() else args.output.resolve()
    target.relative_to(ROOT / "files")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output": target.relative_to(ROOT).as_posix(), "counts": report["counts"],
                      "detail_counts": details,
                      "needs_review": review}, ensure_ascii=False))


if __name__ == "__main__":
    main()
