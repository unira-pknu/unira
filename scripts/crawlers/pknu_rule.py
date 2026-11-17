"""Crawl Pukyong National University rule pages.

This crawler targets the newer rule site at:
    https://www.pknu.ac.kr/rule/main.do

It writes documents under the rule data root:
    files/rule/output/json/pknu_rule_law/*.json
    files/rule/output/json/pknu_rule_bylaw/*.json
    files/rule/output/html/pknu_rule_law/*.html
    files/rule/output/html/pknu_rule_bylaw/*.html
    files/rule/output/files/... unless --no-download-files is used
"""

from __future__ import annotations

import argparse
import html
import hashlib
import json
import logging
import re
import ssl
import sys
import time
import zipfile
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, unquote, urlencode, urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util import Retry

BASE_URL = "https://www.pknu.ac.kr"
RULE_URL = f"{BASE_URL}/rule"
LAW_URL = "https://www.law.go.kr"

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.crawlers.common.schema import (  # noqa: E402
    CrawlStats,
    RunResult,
    apply_common_schema,
    attachment_error,
    attachment_text,
    build_attachment,
    classify_document,
    document_slug,
    log_run_result,
    normalize_attachment,
    now_kst,
    sanitize_attachment_filename,
    unique_attachment_filename,
)
from scripts.crawlers.common.logging import (  # noqa: E402
    configure_crawler_logging, log_attachment_events, log_event, set_run_id,
)
from scripts.crawlers.common.storage import (  # noqa: E402
    get_dataset_paths, load_state_with_migration, save_state_delta, write_document,
)
from scripts.crawlers.common.reader import remove_redundant_legacy_fields  # noqa: E402

PATHS = get_dataset_paths(PROJECT_ROOT, "rule")
OUTPUT_JSON = PATHS.json
OUTPUT_HTML = PATHS.html
OUTPUT_FILES = PATHS.files
OUTPUT_TREE = PATHS.output / "tree"
STATE_FILE = PATHS.state
REQUEST_TIMEOUT = 30
REQUEST_DELAY = 0.25
PKNU_INTERMEDIATES = Path(__file__).with_name("certificates") / "pknu-intermediates.pem"
RETRY_STATUS_CODES = frozenset({429, 500, 502, 503, 504})

DOWNLOAD_PATH_MARKERS = (
    "download",
    "ruleBoardDownload.do",
    "flDownload.do",
    "fileDown",
    "fileDownload",
    "atchFile",
)
FILE_EXTENSIONS = {
    ".pdf",
    ".hwp",
    ".hwpx",
    ".doc",
    ".docx",
    ".xls",
    ".xlsx",
    ".ppt",
    ".pptx",
    ".zip",
    ".rar",
    ".7z",
    ".txt",
    ".csv",
}
STATIC_EXTENSIONS = {
    ".css",
    ".js",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".svg",
    ".ico",
    ".woff",
    ".woff2",
    ".ttf",
    ".eot",
}


class RuleRetry(Retry):
    """Record bounded retries in the crawler's operational logs."""

    def increment(self, method=None, url=None, response=None, error=None, *args, **kwargs):
        retry = super().increment(method, url, response, error, *args, **kwargs)
        log_event(
            log, logging.WARNING, "retry_scheduled", method=method, url=url,
            status_code=getattr(response, "status", None), error=str(error) if error else None,
            attempt=len(retry.history), retries_remaining=retry.total,
        )
        return retry


class PknuSslAdapter(HTTPAdapter):
    """Support PKNU's older cipher suite and incomplete certificate chain."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self._context = ssl.create_default_context()
        self._context.load_verify_locations(cafile=str(PKNU_INTERMEDIATES))
        self._context.set_ciphers("DEFAULT@SECLEVEL=1")
        super().__init__(*args, **kwargs)

    def init_poolmanager(self, *args: Any, **kwargs: Any) -> None:
        kwargs["ssl_context"] = self._context
        return super().init_poolmanager(*args, **kwargs)

    def proxy_manager_for(self, proxy: str, **kwargs: Any):
        kwargs["ssl_context"] = self._context
        return super().proxy_manager_for(proxy, **kwargs)


GENERIC_ATTACHMENT_NAMES = {
    "attachment",
    "download",
    "file",
    "hwp파일 다운로드",
    "pdf파일 다운로드",
    "파일 다운로드",
}

log, log_context = configure_crawler_logging("rule", PROJECT_ROOT)


@dataclass(frozen=True)
class LawNode:
    lid: str
    title: str
    kind_type: str
    issue: str
    effective: str
    law_path: str
    parent_lid: str
    parent_title: str


@dataclass(frozen=True)
class BylawItem:
    bbs_id: str
    board_seq: str
    title: str
    list_url: str
    page_index: int


def configure_logging() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def build_session() -> requests.Session:
    session = requests.Session()
    retry = RuleRetry(
        total=3, connect=3, read=3, status=3, other=0,
        allowed_methods=frozenset({"GET", "HEAD"}), status_forcelist=RETRY_STATUS_CODES,
        backoff_factor=1, backoff_max=30, retry_after_max=60,
        respect_retry_after_header=True, raise_on_status=False,
    )
    session.mount("https://", HTTPAdapter(max_retries=retry))
    session.mount("http://", HTTPAdapter(max_retries=retry))
    # Keep PKNU's compatibility settings on its exact host, including redirects
    # and attachments; law.go.kr and other hosts retain normal TLS settings.
    session.mount(f"{BASE_URL}/", PknuSslAdapter(max_retries=retry))
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8",
            "Referer": f"{RULE_URL}/main.do",
        }
    )
    return session


def fetch(session: requests.Session, url: str, **kwargs: Any) -> requests.Response:
    if kwargs.get("verify") is False or session.verify is False:
        raise ValueError("rule requests require HTTPS certificate verification")
    # Preserve Requests' session/REQUESTS_CA_BUNDLE settings for custom CAs.
    kwargs.setdefault("timeout", REQUEST_TIMEOUT)
    log_event(log, logging.INFO, "request_started", url=url, method="GET")
    try:
        resp = session.get(url, **kwargs)
        resp.raise_for_status()
    except requests.RequestException as exc:
        status = exc.response.status_code if exc.response is not None else None
        retryable = (status in RETRY_STATUS_CODES if status is not None else
                     isinstance(exc, (requests.Timeout, requests.ConnectionError))
                     and not isinstance(exc, requests.exceptions.SSLError))
        log_event(log, logging.ERROR, "request_failed", url=url, error=exc,
                  status_code=status, retryable=retryable)
        raise
    content_type = resp.headers.get("Content-Type", "").lower()
    host = urlsplit(resp.url or url).netloc.lower()
    if "pknu.ac.kr" in host and ("text/" in content_type or "json" in content_type or not content_type):
        try:
            resp.content.decode("utf-8")
            resp.encoding = "utf-8"
        except UnicodeDecodeError:
            resp.encoding = resp.apparent_encoding or "cp949"
    elif "charset=utf-8" in content_type:
        resp.encoding = "utf-8"
    elif resp.encoding is None or resp.encoding.lower() == "iso-8859-1":
        resp.encoding = resp.apparent_encoding or "utf-8"
    time.sleep(REQUEST_DELAY)
    return resp


def normalize_text(text: str) -> str:
    text = html.unescape(text or "").replace("\xa0", " ")
    return " ".join(text.split()).strip()


def html_to_text(fragment: str) -> str:
    soup = BeautifulSoup(fragment or "", "html.parser")
    for tag in soup(["script", "style", "noscript", "svg", "canvas", "form"]):
        tag.decompose()
    return normalize_text(soup.get_text("\n", strip=True))


def slug_for(*parts: str) -> str:
    raw = "|".join(parts)
    return hashlib.md5(raw.encode("utf-8")).hexdigest()[:12]


def sanitize_filename(filename: str) -> str:
    return sanitize_attachment_filename(filename)


def repair_mojibake(text: str) -> str:
    if not text:
        return text
    try:
        repaired = text.encode("latin1").decode("utf-8")
    except UnicodeError:
        return text
    hangul_before = len(re.findall(r"[\uac00-\ud7a3]", text))
    hangul_after = len(re.findall(r"[\uac00-\ud7a3]", repaired))
    return repaired if hangul_after > hangul_before else text


def is_generic_attachment_name(name: str) -> bool:
    normalized = normalize_text(Path(name).stem or name).lower()
    return normalized in GENERIC_ATTACHMENT_NAMES


def is_mojibake_filename(name: str) -> bool:
    return bool(re.search(r"[ÃÂ]|[ðÐ]|[ëìêí][\x80-\xff\u0080-\u00ff]?", name)) and not re.search(
        r"[\uac00-\ud7a3]", name
    )


def dedupe_join_texts(*texts: str) -> str:
    merged: list[str] = []
    seen: set[str] = set()
    for text in texts:
        normalized = normalize_text(text)
        if not normalized or normalized in seen:
            continue
        merged.append(normalized)
        seen.add(normalized)
    return "\n\n".join(merged)


def save_document(doc: dict[str, Any], raw_html: str, category: str) -> None:
    write_document(PATHS, doc, category, doc["slug"], raw_html or "")


def filename_from_response(resp: requests.Response, fallback: str) -> str:
    disposition = resp.headers.get("Content-Disposition", "")
    for pattern in (r"filename\*=UTF-8''([^;]+)", r'filename="([^"]+)"', r"filename=([^;]+)"):
        match = re.search(pattern, disposition, flags=re.I)
        if match:
            return sanitize_filename(repair_mojibake(unquote(match.group(1).strip())))
    return sanitize_filename(fallback)


def filename_from_url(url: str, fallback: str = "attachment") -> str:
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    for key in ("filename", "fileName", "fileNm", "flNm", "orgFileNm", "downFileName"):
        value = query.get(key)
        if value:
            return sanitize_filename(unquote(value))
    path_name = unquote(Path(parts.path).name)
    return sanitize_filename(path_name or fallback)


def select_download_filename(resp: requests.Response, url: str, fallback: str) -> str:
    response_name = filename_from_response(resp, fallback)
    url_name = filename_from_url(url, fallback)
    fallback_name = sanitize_filename(fallback)

    if not is_generic_attachment_name(url_name) and (
        is_generic_attachment_name(fallback_name)
        or is_generic_attachment_name(response_name)
        or is_mojibake_filename(response_name)
    ):
        return url_name
    return response_name


def detect_download_extension(path: Path) -> str:
    try:
        head = path.read_bytes()[:4096]
    except Exception:
        return ""
    lowered = head.lower()
    if head.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        return ".hwp"
    if head.startswith(b"PK\x03\x04"):
        if b"application/hwp+zip" in lowered or b"mimetypeapplication/hwp+zip" in lowered:
            return ".hwpx"
        try:
            with zipfile.ZipFile(path) as zf:
                names = set(zf.namelist())
        except zipfile.BadZipFile:
            names = set()
        if any(name.startswith("word/") for name in names):
            return ".docx"
        if any(name.startswith("xl/") for name in names):
            return ".xlsx"
        if any(name.startswith("ppt/") for name in names):
            return ".pptx"
        return ".zip"
    if head.startswith(b"%PDF"):
        return ".pdf"
    return ""


def unique_path(path: Path, reserved_names: set[str] | None = None) -> Path:
    reserved_names = reserved_names or set()
    if path.name not in reserved_names:
        return path
    stem = path.stem
    suffix = path.suffix
    for index in range(2, 10_000):
        candidate = path.with_name(f"{stem}-{index}{suffix}")
        if candidate.name not in reserved_names:
            return candidate
    raise RuntimeError(f"Could not find unique filename for {path}")


def is_probable_download_url(url: str) -> bool:
    parts = urlsplit(url)
    lowered = unquote((parts.path + "?" + parts.query)).lower()
    path_ext = Path(parts.path).suffix.lower()
    return path_ext in FILE_EXTENSIONS or any(marker.lower() in lowered for marker in DOWNLOAD_PATH_MARKERS)


def is_static_resource_url(url: str) -> bool:
    return Path(urlsplit(url).path).suffix.lower() in STATIC_EXTENSIONS


def attachment_identity(attachment: dict[str, str]) -> tuple[str, str]:
    return (attachment.get("url", ""), attachment.get("name", ""))


def add_attachment(
    attachments: list[dict[str, str]],
    seen: set[tuple[str, str]],
    name: str,
    url: str,
    source_page_url: str,
    source_site: str,
) -> None:
    name = normalize_text(name) or filename_from_url(url)
    item = {
        "name": name,
        "url": url,
        "source_page_url": source_page_url,
        "source_site": source_site,
    }
    key = attachment_identity(item)
    if key in seen:
        return
    seen.add(key)
    attachments.append(item)


def extract_download_urls_from_text(text: str, base_url: str) -> list[str]:
    urls: list[str] = []
    for match in re.finditer(r"""['"]([^'"]*(?:download|Download|ruleBoardDownload|flDownload)[^'"]*)['"]""", text):
        raw = html.unescape(match.group(1)).strip()
        if not raw or raw.startswith(("javascript:", "#")):
            continue
        urls.append(urljoin(base_url, raw))
    return urls


def collect_download_links(
    root: BeautifulSoup,
    base_url: str,
    source_page_url: str,
    source_site: str,
) -> list[dict[str, str]]:
    attachments: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for tag in root.find_all(["a", "area"], href=True):
        href = str(tag.get("href") or "").strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        url = urljoin(base_url, href)
        if not is_probable_download_url(url):
            continue
        img = tag.find("img")
        name = normalize_text(
            str(tag.get("title") or tag.get("download") or "")
            or tag.get_text(" ", strip=True)
            or (img.get("alt", "") if img else "")
            or filename_from_url(url)
        )
        add_attachment(attachments, seen, name, url, source_page_url, source_site)
    for tag in root.find_all(True):
        scriptish = " ".join(str(tag.get(attr) or "") for attr in ("onclick", "data-url", "data-href", "data-download-url"))
        for url in extract_download_urls_from_text(scriptish, base_url):
            if not is_probable_download_url(url):
                continue
            name = normalize_text(str(tag.get("title") or tag.get("download") or "") or tag.get_text(" ", strip=True))
            add_attachment(attachments, seen, name or filename_from_url(url), url, source_page_url, source_site)
    return attachments


def collect_preview_resources(root: BeautifulSoup, base_url: str) -> list[dict[str, str]]:
    resources: list[dict[str, str]] = []
    seen: set[str] = set()
    for tag in root.find_all(["iframe", "frame", "embed", "object"]):
        raw = tag.get("src") or tag.get("data")
        if not raw:
            continue
        url = urljoin(base_url, str(raw))
        if url in seen or is_static_resource_url(url) or is_probable_download_url(url):
            continue
        seen.add(url)
        title = normalize_text(str(tag.get("title") or tag.get("name") or tag.get("id") or "preview"))
        resources.append({"title": title or "preview", "url": url})
    return resources


def download_attachment(
    session: requests.Session,
    url: str,
    category: str,
    slug: str,
    fallback_name: str,
    reserved_names: set[str] | None = None,
) -> dict[str, Any]:
    resp = fetch(session, url, stream=True)
    filename = select_download_filename(resp, url, fallback_name)
    if not Path(filename).suffix:
        path_ext = Path(urlsplit(url).path).suffix
        if path_ext:
            filename += path_ext

    file_dir = PATHS.attachment_dir(category, slug)
    file_dir.mkdir(parents=True, exist_ok=True)
    reserved = reserved_names if reserved_names is not None else set()
    filename = unique_attachment_filename(filename, reserved)
    target = file_dir / filename
    final_url = resp.url
    content_type = resp.headers.get("Content-Type", "")
    try:
        with target.open("wb") as handle:
            for chunk in resp.iter_content(chunk_size=1024 * 256):
                if chunk:
                    handle.write(chunk)
    except (OSError, requests.RequestException):
        try:
            target.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    finally:
        resp.close()

    detected_ext = detect_download_extension(target)
    if detected_ext and target.suffix.lower() != detected_ext:
        reserved.discard(target.name)
        renamed_name = unique_attachment_filename(target.with_suffix(detected_ext).name, reserved)
        renamed = target.with_name(renamed_name)
        target.replace(renamed)
        target = renamed

    return {
        "name": target.name,
        "saved_name": target.name,
        "saved_path": target,
        "downloaded": True,
        "content_type": content_type,
        "final_url": final_url,
    }


def extract_preview_texts(
    session: requests.Session,
    resources: list[dict[str, str]],
    referer: str,
) -> list[dict[str, str]]:
    previews: list[dict[str, str]] = []
    for resource in resources:
        url = resource.get("url", "")
        if not url:
            continue
        preview = {
            "title": resource.get("title", "preview"),
            "url": url,
            "text": "",
            "content_type": "",
        }
        try:
            resp = fetch(session, url, headers={"Referer": referer})
            content_type = resp.headers.get("Content-Type", "")
            preview["content_type"] = content_type
            if "text/" in content_type.lower() or "html" in content_type.lower() or "xml" in content_type.lower():
                preview["text"] = html_to_text(resp.text)
            else:
                preview["error"] = f"Skipped non-text preview content: {content_type}"
        except Exception as exc:
            preview["error"] = str(exc)
            log.warning("Preview text extraction failed: %s (%s)", url, exc)
        previews.append(preview)
    return previews


def parse_tree_payload(raw: dict[str, Any]) -> dict[str, Any]:
    tree_data = raw.get("datas", {}).get("treeData", raw)
    if isinstance(tree_data, str):
        tree_data = json.loads(html.unescape(tree_data))
    if "mixedNodes" not in tree_data and "ruleTree" in tree_data:
        tree_data["mixedNodes"] = tree_data["ruleTree"]
    return tree_data


def node_title(node: dict[str, Any]) -> str:
    data = node.get("data") or {}
    return normalize_text(data.get("name") or node.get("text") or "")


def normalize_tree_nodes(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_id = {str(node.get("id")): node for node in nodes if node.get("id")}

    def path_for(node_id: str) -> list[dict[str, str]]:
        path: list[dict[str, str]] = []
        seen: set[str] = set()
        current = by_id.get(node_id)
        while current:
            current_id = str(current.get("id") or "")
            if not current_id or current_id in seen:
                break
            seen.add(current_id)
            data = current.get("data") or {}
            path.append(
                {
                    "id": current_id,
                    "lid": str(data.get("lid") or ""),
                    "title": node_title(current),
                    "kind_type": str(data.get("kindType") or current.get("type") or ""),
                }
            )
            parent_id = str(current.get("parent") or "")
            current = by_id.get(parent_id)
        return list(reversed(path))

    normalized: list[dict[str, Any]] = []
    for node in nodes:
        node_id = str(node.get("id") or "")
        if not node_id:
            continue
        data = node.get("data") or {}
        parent_id = str(node.get("parent") or "")
        parent_node = by_id.get(parent_id) or {}
        parent_data = parent_node.get("data") or {}
        path = path_for(node_id)
        kind_type = str(data.get("kindType") or node.get("type") or "")
        normalized.append(
            {
                "id": node_id,
                "parent_id": parent_id,
                "title": node_title(node),
                "kind_type": kind_type,
                "lid": str(data.get("lid") or ""),
                "parent_lid": str(parent_data.get("lid") or ""),
                "parent_title": node_title(parent_node) if parent_node else "",
                "issue": str(data.get("issue") or ""),
                "effective": str(data.get("eff") or ""),
                "law_path": html.unescape(str(data.get("url") or "")),
                "depth": max(0, len(path) - 1),
                "path_ids": [item["id"] for item in path],
                "path_lids": [item["lid"] for item in path if item["lid"]],
                "path_titles": [item["title"] for item in path if item["title"]],
                "is_law_crawl_target": kind_type in {"hak", "gyu"},
            }
        )
    return normalized


def save_rule_tree(raw_payload: dict[str, Any], tree: dict[str, Any]) -> None:
    nodes = tree.get("mixedNodes") or []
    if not isinstance(nodes, list):
        log.warning("Could not save rule tree; mixedNodes is not a list")
        return

    OUTPUT_TREE.mkdir(parents=True, exist_ok=True)
    normalized = normalize_tree_nodes(nodes)
    fetched_at = now_kst()

    (OUTPUT_TREE / "rule_tree_raw.json").write_text(
        json.dumps(
            {
                "fetched_at": fetched_at,
                "source_url": f"{RULE_URL}/loadTree.do",
                "payload": raw_payload,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    (OUTPUT_TREE / "rule_tree_nodes.json").write_text(
        json.dumps(
            {
                "fetched_at": fetched_at,
                "source_url": f"{RULE_URL}/loadTree.do",
                "num_nodes": len(normalized),
                "num_law_crawl_targets": sum(1 for node in normalized if node["is_law_crawl_target"]),
                "nodes": normalized,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    log.info("Saved rule tree nodes=%d to %s", len(normalized), OUTPUT_TREE)


def load_law_nodes(session: requests.Session, save_tree: bool = True) -> list[LawNode]:
    payload = fetch(session, f"{RULE_URL}/loadTree.do").json()
    tree = parse_tree_payload(payload)
    nodes = tree.get("mixedNodes") or []
    if save_tree:
        save_rule_tree(payload, tree)
    by_id = {str(node.get("id")): node for node in nodes if node.get("id")}

    law_nodes: list[LawNode] = []
    for node in nodes:
        data = node.get("data") or {}
        kind_type = str(data.get("kindType") or node.get("type") or "")
        if kind_type not in {"hak", "gyu"}:
            continue

        parent_id = str(node.get("parent") or "")
        parent_node = by_id.get(parent_id) or {}
        parent_data = parent_node.get("data") or {}
        law_nodes.append(
            LawNode(
                lid=str(data.get("lid") or "").strip(),
                title=normalize_text(data.get("name") or node.get("text") or ""),
                kind_type=kind_type,
                issue=str(data.get("issue") or ""),
                effective=str(data.get("eff") or ""),
                law_path=html.unescape(str(data.get("url") or "")),
                parent_lid=str(parent_data.get("lid") or ""),
                parent_title=normalize_text(parent_data.get("name") or parent_node.get("text") or ""),
            )
        )
    return [node for node in law_nodes if node.lid and node.law_path]


def with_query_param(url: str, **params: str) -> str:
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query.update(params)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def find_lsw_url(session: requests.Session, law_path: str) -> str | None:
    wrapper_url = urljoin(LAW_URL, law_path)
    wrapper_url = with_query_param(wrapper_url, type="HTML")
    resp = fetch(session, wrapper_url, headers={"Referer": f"{RULE_URL}/schRegAndRuleList.do"})
    soup = BeautifulSoup(resp.text, "html.parser")

    iframe = soup.find("iframe", src=True)
    if iframe:
        return urljoin(LAW_URL, str(iframe["src"]))

    hidden = soup.find("input", {"id": "url"})
    if hidden and hidden.get("value"):
        return str(hidden["value"])
    return None


def extract_ajax_params(lsw_html: str) -> dict[str, str]:
    soup = BeautifulSoup(lsw_html, "html.parser")
    params = {
        "schlPubRulSeq": "",
        "joTpYn": "Y",
        "languageType": "KO",
        "chrClsCd": "010202",
        "urlMode": "schlPubRulLsInfoP",
        "schlPubRulId": "",
        "prmlNo": "",
        "prmlYd": "",
        "efYd": "",
        "preview": "",
    }

    for key, input_id in {
        "schlPubRulSeq": "schlPubRulSeq",
        "schlPubRulId": "lsId",
        "prmlNo": "prmlNo",
        "prmlYd": "prmlYd",
        "efYd": "efYd",
    }.items():
        tag = soup.find("input", {"id": input_id})
        if tag and tag.get("value") is not None:
            params[key] = str(tag["value"])

    script_text = "\n".join(script.get_text("\n") for script in soup.find_all("script"))
    for key in params:
        if params[key]:
            continue
        match = re.search(rf'{re.escape(key)}\s*:\s*"?([^",\n]+)"?', script_text)
        if match:
            params[key] = match.group(1).strip()

    return params


def parse_law_ajax_html(raw_html: str) -> tuple[str, list[dict[str, str]], list[dict[str, str]]]:
    soup = BeautifulSoup(raw_html, "html.parser")
    root = soup.select_one("#conScroll") or soup.select_one("#contentBody") or soup.body or soup

    attachments = collect_download_links(root, LAW_URL + "/LSW/", LAW_URL, "law.go.kr")
    preview_resources = collect_preview_resources(root, LAW_URL + "/LSW/")

    for tag in root.select("input, script, style, noscript, svg, canvas, iframe, frame, embed, object"):
        tag.decompose()

    return normalize_text(root.get_text("\n", strip=True)), attachments, preview_resources


def crawl_law_node(
    session: requests.Session,
    node: LawNode,
    download_files: bool,
) -> dict[str, Any] | None:
    try:
        lsw_url = find_lsw_url(session, node.law_path)
        if not lsw_url:
            raise RuntimeError("Could not find law.go.kr LSW iframe URL")

        lsw_resp = fetch(session, lsw_url, headers={"Referer": f"{RULE_URL}/schRegAndRuleList.do"})
        params = extract_ajax_params(lsw_resp.text)
        ajax_url = f"{LAW_URL}/LSW/schlPubRulInfoR.do?{urlencode(params)}"
        ajax_resp = fetch(session, ajax_url, headers={"Referer": lsw_url})
        content, attachments, preview_resources = parse_law_ajax_html(ajax_resp.text)
        preview_texts = extract_preview_texts(session, preview_resources, ajax_url)
    except Exception as exc:
        log.warning("Law crawl failed for LID=%s title=%s: %s", node.lid, node.title, exc)
        return None

    source_id = f"law:{node.lid}"
    slug = document_slug("pknu_rule", source_id)
    attachment_texts: list[dict[str, Any]] = []
    file_preview_texts: list[dict[str, Any]] = []
    if download_files:
        attachments = save_attachments(session, attachments, "pknu_rule_law", slug)
        attachment_texts = extract_attachment_texts(attachments)
        file_preview_texts = build_file_preview_texts(attachment_texts)
    else:
        attachments = [
            normalize_attachment(item, index=index, project_root=PROJECT_ROOT)
            for index, item in enumerate(attachments, start=1)
        ]
    combined_content = dedupe_join_texts(
        content,
        *(preview.get("text", "") for preview in preview_texts),
        *(preview.get("text", "") for preview in file_preview_texts),
    )

    doc = {
        "slug": slug,
        "title": node.title,
        "url": lsw_url,
        "category": "pknu_rule_law",
        "subcategory": "school_rule" if node.kind_type == "hak" else "regulation",
        "type": node.kind_type,
        "date": node.effective,
        "source_site": "pknu_rule/law.go.kr",
        "source_id": node.lid,
        "parent_lid": node.parent_lid,
        "parent_title": node.parent_title,
        "issued_at": node.issue,
        "effective_at": node.effective,
        "content": combined_content,
        "preview_texts": preview_texts,
        "attachments": attachments,
        "crawled_at": datetime.now().isoformat(timespec="seconds"),
    }
    document_type = "law" if node.kind_type == "hak" else "regulation"
    doc = apply_common_schema(
        doc,
        source_dataset="pknu_rule",
        source_id=source_id,
        source_site=LAW_URL,
        document_type=document_type,
        content_source="law_ajax_html",
        published_at=node.issue,
        effective_at=node.effective,
        metadata={
            "source_type": node.kind_type,
            "lid": node.lid,
            "parent_lid": node.parent_lid,
            "parent_title": node.parent_title,
            "issued_at": node.issue,
            "effective_at": node.effective,
        },
        crawled_at=doc.get("crawled_at"),
    )
    remove_redundant_legacy_fields(doc)
    save_document(doc, ajax_resp.text, "pknu_rule_law")
    return doc


# ── Extra law/rule docs discovered outside loadTree.do ──────────────────────
# loadTree.do (see load_law_nodes) only exposes ~85 top-level 학칙/규정 nodes.
# PKNU's own site search surfaces additional article-level deep links (e.g.
# a specific 학칙 조항 for 출석/결석 인정 기준) under a completely different
# URL scheme, /rule/searchSchRegAndRuleView.do?no=<id>, that never appears in
# that tree at all. Each such wrapper page embeds the same law.go.kr LSW
# iframe crawl_law_node() already knows how to fetch; only the discovery
# step (wrapper -> iframe src) differs, since the wrapper here lives on
# pknu.ac.kr rather than resolving directly against law.go.kr like tree
# node paths do. Multiple `no=` values can point at the same schlPubRulSeq
# (different article anchors within one document).
EXTRA_LAW_SEARCH_NOS: dict[str, str] = {
    "2094848": "국립부경대학교 학칙 (출석·결석 인정 및 성적 정정·이의신청 조항)",
    "2096708": "국립부경대학교 학칙 (학생생활관 입사신청 절차 조항)",
}


def find_lsw_url_from_pknu_wrapper(session: requests.Session, wrapper_url: str) -> str | None:
    """searchSchRegAndRuleView.do?no=<id> wrapper page -> its embedded law.go.kr LSW iframe URL."""
    resp = fetch(session, wrapper_url, headers={"Referer": f"{RULE_URL}/schRegAndRuleList.do"})
    soup = BeautifulSoup(resp.text, "html.parser")
    iframe = soup.find("iframe", src=True)
    if iframe:
        return html.unescape(str(iframe["src"]))
    return None


def crawl_extra_law_search_no(
    session: requests.Session,
    no: str,
    fallback_title: str,
    download_files: bool,
) -> dict[str, Any] | None:
    wrapper_url = f"{RULE_URL}/searchSchRegAndRuleView.do?no={no}"
    try:
        lsw_url = find_lsw_url_from_pknu_wrapper(session, wrapper_url)
        if not lsw_url:
            raise RuntimeError("no iframe found in searchSchRegAndRuleView wrapper")

        lsw_resp = fetch(session, lsw_url, headers={"Referer": wrapper_url})
        params = extract_ajax_params(lsw_resp.text)
        ajax_url = f"{LAW_URL}/LSW/schlPubRulInfoR.do?{urlencode(params)}"
        ajax_resp = fetch(session, ajax_url, headers={"Referer": lsw_url})
        content, attachments, preview_resources = parse_law_ajax_html(ajax_resp.text)
        preview_texts = extract_preview_texts(session, preview_resources, ajax_url)
    except Exception as exc:
        log.warning("Extra law-search crawl failed for no=%s: %s", no, exc)
        return None

    slug = slug_for("law_extra_search", no)
    attachment_texts: list[dict[str, Any]] = []
    file_preview_texts: list[dict[str, Any]] = []
    if download_files:
        attachments = save_attachments(session, attachments, "pknu_rule_law", slug)
        attachment_texts = extract_attachment_texts(attachments)
        file_preview_texts = build_file_preview_texts(attachment_texts)
    combined_content = dedupe_join_texts(
        content,
        *(preview.get("text", "") for preview in preview_texts),
        *(preview.get("text", "") for preview in file_preview_texts),
    )

    doc = {
        "slug": slug,
        "title": fallback_title,
        "url": wrapper_url,
        "category": "pknu_rule_law",
        "subcategory": "school_rule",
        "type": "hak",
        "date": "",
        "source_site": "pknu_rule/law.go.kr",
        "source_id": f"search_no_{no}",
        "parent_lid": "",
        "parent_title": "",
        "issued_at": "",
        "effective_at": "",
        "content": combined_content,
        "html_text": content,
        "html_text_source": "law_ajax_html",
        "page_content": content,
        "preview_texts": preview_texts,
        "attachments": attachments,
        "attachment_texts": attachment_texts,
        "file_preview_texts": file_preview_texts,
        "crawled_at": datetime.now().isoformat(timespec="seconds"),
    }
    save_document(doc, ajax_resp.text, "pknu_rule_law")
    return doc


def crawl_extra_law_searches(session: requests.Session, download_files: bool) -> int:
    saved = 0
    for no, fallback_title in EXTRA_LAW_SEARCH_NOS.items():
        log.info("Crawling extra law search no=%s", no)
        if crawl_extra_law_search_no(session, no, fallback_title, download_files):
            saved += 1
    return saved


def parse_last_page(soup: BeautifulSoup) -> int:
    last = 1
    for anchor in soup.select("ul.paging a[href]"):
        href = str(anchor["href"])
        match = re.search(r"pageIndex=(\d+)", href)
        if match:
            last = max(last, int(match.group(1)))
    return last


def parse_bylaw_list_page(html_text: str, list_url: str, page_index: int) -> tuple[list[BylawItem], int]:
    soup = BeautifulSoup(html_text, "html.parser")
    items: list[BylawItem] = []
    pattern = re.compile(r"onLoadPage\((\d+)\s*,\s*(\d+)\)")

    for anchor in soup.select("section.dtLaw_list a[href]"):
        href = str(anchor.get("href") or "")
        match = pattern.search(href)
        if not match:
            continue
        title = normalize_text(anchor.get("title") or anchor.get_text(" ", strip=True))
        items.append(
            BylawItem(
                bbs_id=match.group(1),
                board_seq=match.group(2),
                title=title,
                list_url=list_url,
                page_index=page_index,
            )
        )
    return items, parse_last_page(soup)


def load_bylaw_list_items(session: requests.Session, max_pages: int | None) -> list[BylawItem]:
    first_url = f"{RULE_URL}/bylawsAndGuidelineList.do?pageIndex=1"
    first = fetch(session, first_url)
    items, last_page = parse_bylaw_list_page(first.text, first_url, 1)
    if max_pages:
        last_page = min(last_page, max_pages)

    for page_index in range(2, last_page + 1):
        page_url = f"{RULE_URL}/bylawsAndGuidelineList.do?pageIndex={page_index}"
        resp = fetch(session, page_url)
        page_items, _ = parse_bylaw_list_page(resp.text, page_url, page_index)
        items.extend(page_items)
    return items


def save_attachments(
    session: requests.Session,
    attachments: list[dict[str, Any]],
    category: str,
    slug: str,
) -> list[dict[str, Any]]:
    saved: list[dict[str, Any]] = []
    reserved_names: set[str] = set()
    for idx, attachment in enumerate(attachments, start=1):
        item = dict(attachment)
        url = str(item.get("url") or "").strip()
        if not url:
            item.update(
                build_attachment(
                    index=idx,
                    name=item.get("name") or f"attachment-{idx}",
                    url="",
                    project_root=PROJECT_ROOT,
                    error=attachment_error("MISSING_URL", "attachment URL is empty", False),
                    legacy=item,
                )
            )
            saved.append(item)
            continue
        try:
            fallback = item.get("name") or f"attachment-{idx}"
            download = download_attachment(session, url, category, slug, fallback, reserved_names)
            item.update(
                build_attachment(
                    index=idx,
                    name=download["name"],
                    url=url,
                    final_url=download["final_url"],
                    saved_path=download["saved_path"],
                    downloaded=True,
                    project_root=PROJECT_ROOT,
                    content_type=download["content_type"],
                    legacy=item,
                )
            )
        except Exception as exc:
            item["download_error"] = str(exc)
            item.update(
                build_attachment(
                    index=idx,
                    name=item.get("name") or f"attachment-{idx}",
                    url=url,
                    project_root=PROJECT_ROOT,
                    error=attachment_error("DOWNLOAD_FAILED", exc, True),
                    legacy=item,
                )
            )
            log.warning("Attachment download failed: %s (%s)", item.get("url"), exc)
        saved.append(item)
    return saved


def extract_attachment_texts(attachments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    previews: list[dict[str, Any]] = []
    try:
        from preprocessing import SUPPORTED_EXTS, extract_blocks
    except Exception as exc:
        reason = f"preprocessing.extract_blocks import failed: {exc}"
        for attachment in attachments:
            if attachment.get("saved_path"):
                attachment["extract_error"] = reason
                attachment["text"] = attachment_text(
                    "failed", extractor="rule_preprocessing", error=reason
                )
        return previews

    for attachment in attachments:
        saved_path = attachment.get("saved_path")
        if not saved_path:
            continue

        path = PROJECT_ROOT / saved_path
        preview: dict[str, Any] = {
            "name": attachment.get("name", ""),
            "saved_path": str(saved_path),
            "url": attachment.get("url", ""),
            "text": "",
            "num_blocks": 0,
            "num_chars": 0,
        }
        try:
            if not path.exists():
                raise FileNotFoundError(path)
            if path.stat().st_size == 0:
                reason = "empty attachment file"
                preview["skipped"] = reason
                attachment["extract_skipped"] = reason
                attachment["text"] = attachment_text(
                    "skipped", extractor="rule_preprocessing", error=reason
                )
                previews.append(preview)
                continue
            if path.suffix.lower() not in SUPPORTED_EXTS:
                reason = f"unsupported extension: {path.suffix.lower() or '(none)'}"
                preview["skipped"] = reason
                attachment["extract_skipped"] = reason
                attachment["text"] = attachment_text(
                    "skipped", extractor="rule_preprocessing", error=reason
                )
                previews.append(preview)
                continue
            blocks = extract_blocks(path)
            text = dedupe_join_texts(*(str(block.get("text", "")) for block in blocks))
            preview.update({"text": text, "num_blocks": len(blocks), "num_chars": len(text)})
            attachment["extracted_text_chars"] = len(text)
            attachment["text"] = attachment_text(
                "success",
                content=text,
                characters=len(text),
                extractor="rule_preprocessing",
            )
        except Exception as exc:
            preview["error"] = str(exc)
            attachment["extract_error"] = str(exc)
            attachment["text"] = attachment_text(
                "failed", extractor="rule_preprocessing", error=exc
            )
            log.warning("Attachment text extraction failed: %s (%s)", saved_path, exc)
        previews.append(preview)
    return previews


def build_file_preview_texts(attachment_texts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    file_previews: list[dict[str, Any]] = []
    for attachment_text in attachment_texts:
        preview = dict(attachment_text)
        preview["source"] = "downloaded_attachment"
        preview["preview_equivalent"] = True
        file_previews.append(preview)
    return file_previews


def load_bylaw_preview_map(session: requests.Session) -> dict[str, dict[str, str]]:
    url = f"{RULE_URL}/byRows.do"
    try:
        payload = fetch(session, url, headers={"Referer": f"{RULE_URL}/main.do"}).json()
    except Exception as exc:
        log.warning("Could not load bylaw preview rows from %s: %s", url, exc)
        return {}

    preview_map: dict[str, dict[str, str]] = {}
    for row in payload.get("datas", []) or []:
        board_seq = str(row.get("boardSeq") or "").strip()
        if not board_seq:
            continue
        content_html = str(row.get("cont") or "")
        preview_map[board_seq] = {
            "title": normalize_text(str(row.get("title") or "")),
            "content": html_to_text(content_html),
            "content_html": content_html,
            "opn_at": str(row.get("opnAt") or ""),
            "point_text": normalize_text(str(row.get("pointText") or "")),
        }
    return preview_map


def parse_bylaw_detail(html_text: str, item: BylawItem, detail_url: str) -> dict[str, Any]:
    soup = BeautifulSoup(html_text, "html.parser")
    title = normalize_text((soup.find("h4") or {}).get_text(" ", strip=True)) or item.title

    author = ""
    date = ""
    for meta_item in soup.select(".subTableDtl li"):
        text = normalize_text(meta_item.get_text(" ", strip=True))
        if "작성자" in text:
            author = normalize_text(text.replace("작성자", "", 1))
        if "작성일" in text:
            date_match = re.search(r"\d{4}-\d{2}-\d{2}", text)
            if date_match:
                date = date_match.group(0)

    detail_base_url = f"{RULE_URL}/"
    attachments = collect_download_links(soup, detail_base_url, detail_url, "pknu_rule")
    preview_resources = collect_preview_resources(soup, detail_base_url)

    content_root = soup.select_one(".subTableDtl > section") or soup.select_one(".subTableDtl") or soup.body or soup
    for tag in content_root.select("script, style, noscript, svg, canvas, form, iframe, frame, embed, object"):
        tag.decompose()

    return {
        "title": title,
        "author": author,
        "date": date,
        "content": normalize_text(content_root.get_text("\n", strip=True)),
        "content_html": str(content_root),
        "attachments": attachments,
        "preview_resources": preview_resources,
    }


def crawl_bylaw_item(
    session: requests.Session,
    item: BylawItem,
    download_files: bool,
    preview_entry: dict[str, str] | None = None,
) -> dict[str, Any] | None:
    detail_url = f"{RULE_URL}/bylawsAndGuidelineView.do?bbsId={item.bbs_id}&no={item.board_seq}"
    try:
        resp = fetch(session, detail_url, headers={"Referer": item.list_url})
        detail = parse_bylaw_detail(resp.text, item, detail_url)
        preview_texts = extract_preview_texts(session, detail["preview_resources"], detail_url)
    except Exception as exc:
        log.warning("Bylaw crawl failed for no=%s title=%s: %s", item.board_seq, item.title, exc)
        return None

    source_id = f"bylaw:{item.bbs_id}:{item.board_seq}"
    slug = document_slug("pknu_rule", source_id)
    attachments = detail["attachments"]
    attachment_texts: list[dict[str, Any]] = []
    file_preview_texts: list[dict[str, Any]] = []
    if download_files:
        attachments = save_attachments(session, attachments, "pknu_rule_bylaw", slug)
        attachment_texts = extract_attachment_texts(attachments)
        file_preview_texts = build_file_preview_texts(attachment_texts)
    else:
        attachments = [
            normalize_attachment(item, index=index, project_root=PROJECT_ROOT)
            for index, item in enumerate(attachments, start=1)
        ]

    preview_entry = preview_entry or {}
    preview_content = preview_entry.get("content", "")
    combined_content = dedupe_join_texts(
        detail["content"],
        preview_content,
        *(preview.get("text", "") for preview in preview_texts),
        *(preview.get("text", "") for preview in file_preview_texts),
    )

    doc = {
        "slug": slug,
        "title": detail["title"],
        "url": detail_url,
        "category": "pknu_rule_bylaw",
        "subcategory": "bylaw" if item.bbs_id == "1" else "guideline",
        "type": "bylaw_guideline",
        "date": detail["date"],
        "source_site": "pknu_rule",
        "source_id": item.board_seq,
        "bbs_id": item.bbs_id,
        "author": detail["author"],
        "content": combined_content,
        "content_html": detail["content_html"],
        "preview_content": preview_content,
        "preview_content_html": preview_entry.get("content_html", ""),
        "preview_texts": preview_texts,
        "opn_at": preview_entry.get("opn_at", ""),
        "point_text": preview_entry.get("point_text", ""),
        "attachments": attachments,
        "crawled_at": datetime.now().isoformat(timespec="seconds"),
    }
    document_type = "bylaw" if item.bbs_id == "1" else "guideline"
    doc = apply_common_schema(
        doc,
        source_dataset="pknu_rule",
        source_id=source_id,
        source_site=RULE_URL,
        document_type=document_type,
        content_source="pknu_rule_html",
        published_at=detail["date"],
        author=detail["author"],
        metadata={
            "source_type": "bylaw_guideline",
            "bbs_id": item.bbs_id,
            "board_seq": item.board_seq,
            "opn_at": preview_entry.get("opn_at", ""),
            "point_text": preview_entry.get("point_text", ""),
        },
        crawled_at=doc.get("crawled_at"),
    )
    remove_redundant_legacy_fields(doc)
    save_document(doc, resp.text, "pknu_rule_bylaw")
    return doc


def crawl_laws(
    session: requests.Session,
    max_items: int | None,
    download_files: bool,
    save_tree: bool,
    state: dict[str, Any],
) -> CrawlStats:
    nodes = load_law_nodes(session, save_tree=save_tree)
    if max_items:
        nodes = nodes[:max_items]
    log.info("Found %s law/rule nodes", len(nodes))

    stats = CrawlStats(discovered=len(nodes))
    for index, node in enumerate(nodes, start=1):
        log_event(log, logging.DEBUG, "document_discovered", source_id=node.lid, url=node.law_path, title=node.title)
        log.info("Crawling law %s/%s LID=%s %s", index, len(nodes), node.lid, node.title)
        stats.requested += 1
        slug = document_slug("pknu_rule", f"law:{node.lid}")
        old_hash = load_existing_content_hash(PATHS.document_json("pknu_rule_law", slug))
        doc = crawl_law_node(session, node, download_files)
        if doc:
            outcome = classify_document(old_hash, doc.get("content_hash"))
            setattr(stats, outcome, getattr(stats, outcome) + 1)
            stats.count_attachments(doc.get("attachments"))
            log_attachment_events(log, doc.get("attachments"), source_id=f"law:{node.lid}")
            event = "document_unchanged" if outcome == "unchanged" else "document_saved"
            log_event(log, logging.INFO, event, source_id=f"law:{node.lid}", url=doc.get("url"), status=outcome)
            state.setdefault("items", {})[f"law:{node.lid}"] = {
                "source_id": f"law:{node.lid}", "slug": slug,
                "content_hash": doc.get("content_hash", ""),
                "last_seen_at": now_kst(), "miss_count": 0, "status": "active",
            }
        else:
            stats.failed += 1
            log_event(log, logging.ERROR, "document_failed", source_id=f"law:{node.lid}", url=node.law_path)
    return stats


def crawl_bylaws(
    session: requests.Session, max_pages: int | None, download_files: bool,
    state: dict[str, Any],
) -> CrawlStats:
    items = load_bylaw_list_items(session, max_pages)
    preview_map = load_bylaw_preview_map(session)
    log.info("Found %s bylaw/guideline list items", len(items))

    stats = CrawlStats(discovered=len(items))
    for index, item in enumerate(items, start=1):
        log_event(log, logging.DEBUG, "document_discovered", source_id=item.board_seq, url=item.list_url, title=item.title)
        log.info("Crawling bylaw %s/%s no=%s %s", index, len(items), item.board_seq, item.title)
        stats.requested += 1
        slug = document_slug("pknu_rule", f"bylaw:{item.bbs_id}:{item.board_seq}")
        old_hash = load_existing_content_hash(PATHS.document_json("pknu_rule_bylaw", slug))
        doc = crawl_bylaw_item(session, item, download_files, preview_map.get(item.board_seq))
        if doc:
            outcome = classify_document(old_hash, doc.get("content_hash"))
            setattr(stats, outcome, getattr(stats, outcome) + 1)
            stats.count_attachments(doc.get("attachments"))
            log_attachment_events(log, doc.get("attachments"), source_id=f"bylaw:{item.bbs_id}:{item.board_seq}")
            event = "document_unchanged" if outcome == "unchanged" else "document_saved"
            log_event(log, logging.INFO, event, source_id=f"bylaw:{item.bbs_id}:{item.board_seq}", url=doc.get("url"), status=outcome)
            state.setdefault("items", {})[f"bylaw:{item.bbs_id}:{item.board_seq}"] = {
                "source_id": f"bylaw:{item.bbs_id}:{item.board_seq}", "slug": slug,
                "content_hash": doc.get("content_hash", ""),
                "last_seen_at": now_kst(), "miss_count": 0, "status": "active",
            }
        else:
            stats.failed += 1
            log_event(log, logging.ERROR, "document_failed", source_id=f"bylaw:{item.bbs_id}:{item.board_seq}", url=item.list_url)
    return stats


def load_existing_content_hash(path: Path) -> str:
    if not path.exists():
        return ""
    try:
        return str(json.loads(path.read_text(encoding="utf-8")).get("content_hash") or "")
    except (OSError, json.JSONDecodeError, TypeError):
        return ""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Crawl PKN rule pages into files/rule/output.")
    parser.add_argument("--laws", action="store_true", help="Crawl school rules/regulations from the rule tree.")
    parser.add_argument("--bylaws", action="store_true", help="Crawl bylaws/guidelines list and detail pages.")
    parser.set_defaults(download_files=True)
    parser.set_defaults(save_tree=True)
    parser.add_argument(
        "--download-files",
        dest="download_files",
        action="store_true",
        help="Download referenced attachments (default).",
    )
    parser.add_argument(
        "--no-download-files",
        dest="download_files",
        action="store_false",
        help="Skip attachment downloads and attachment text extraction.",
    )
    parser.add_argument("--max-law-items", type=int, default=None, help="Limit law/rule items for smoke tests.")
    parser.add_argument("--max-bylaw-pages", type=int, default=None, help="Limit bylaw list pages for smoke tests.")
    parser.add_argument(
        "--save-tree",
        dest="save_tree",
        action="store_true",
        help="Save the raw and normalized rule tree when crawling --laws (default).",
    )
    parser.add_argument(
        "--no-save-tree",
        dest="save_tree",
        action="store_false",
        help="Do not save the rule tree payload while crawling --laws.",
    )
    return parser.parse_args()


def main() -> int:
    configure_logging()
    args = parse_args()
    if not args.laws and not args.bylaws:
        args.laws = True
        args.bylaws = True

    mode = "smoke" if args.max_law_items is not None or args.max_bylaw_pages is not None else "full"
    result = RunResult(dataset="rule", mode=mode)
    if args.laws != args.bylaws:
        result.run_id += "-laws" if args.laws else "-bylaws"
    set_run_id(log_context, result.run_id)
    log_event(log, logging.INFO, "run_started", mode=mode, laws=args.laws, bylaws=args.bylaws)
    try:
        state, state_origin = load_state_with_migration(PATHS)
        initial_items = deepcopy(state["items"])
        log_event(log, logging.INFO, "state_loaded", path=STATE_FILE, entries=len(state["items"]), origin=state_origin)
        session = build_session()
        if args.laws:
            log_event(log, logging.INFO, "section_started", section="laws")
            result.stats.add(crawl_laws(session, args.max_law_items, args.download_files, args.save_tree, state))
            log_event(log, logging.INFO, "section_finished", section="laws")

            log_event(log, logging.INFO, "section_started", section="extra_law_searches")
            extra_total = len(EXTRA_LAW_SEARCH_NOS)
            extra_saved = crawl_extra_law_searches(session, args.download_files)
            result.stats.add(
                CrawlStats(
                    discovered=extra_total,
                    requested=extra_total,
                    new=extra_saved,
                    failed=extra_total - extra_saved,
                )
            )
            log_event(log, logging.INFO, "section_finished", section="extra_law_searches", saved=extra_saved)
        if args.bylaws:
            log_event(log, logging.INFO, "section_started", section="bylaws")
            result.stats.add(crawl_bylaws(session, args.max_bylaw_pages, args.download_files, state))
            log_event(log, logging.INFO, "section_finished", section="bylaws")
        save_state_delta(PATHS, state, initial_items)
        log_event(log, logging.INFO, "state_saved", path=STATE_FILE, entries=len(state["items"]))
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
    result_path = result.save(PROJECT_ROOT)
    if result.status != "failed":
        log_event(log, logging.INFO, "run_finished", status=result.status, stats=result.stats.to_dict(), result_path=result_path)
    log_run_result(log, result, result_path)
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
