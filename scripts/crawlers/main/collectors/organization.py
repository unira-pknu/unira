"""Collect CMS organization charts, linked introductions and public staff widgets."""

from __future__ import annotations
from bs4 import BeautifulSoup, Comment
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from scripts.crawlers.common.main_static import (
    extract_structure,
    parse_system_organization_chart,
    render_sections,
)
from scripts.crawlers.common.schema import CrawlStats
from scripts.crawlers.main.common.http import fetch, is_html_response
from scripts.crawlers.main.common.storage import BASE_URL, PROJECT_ROOT
from scripts.crawlers.main.paths import page_json, page_root
from scripts.crawlers.main.routes import ROUTES
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit
import hashlib
import json
import logging
import re
import requests
import time


PAGE_URL = "https://www.pknu.ac.kr/main/533"
WIDGET_URL = "https://www.pknu.ac.kr/org/getMngList.do"
DEPARTMENT_ID = 249
MAX_JSON_BYTES = 512 * 1024
MAX_HTML_BYTES = 2 * 1024 * 1024
log = logging.getLogger("pknu_student_life")


def _text(value) -> str:
    return re.sub(r"\s+", " ", value.get_text(" ", strip=True)).strip() if value else ""


def _contact_fields(root) -> dict[str, str]:
    contacts = {}
    for group in root.select(".con05_sub_wrap > div, .con03_bottom_l"):
        lists = group.find_all("ul", recursive=False)
        if len(lists) != 2:
            continue
        labels = [_text(item) for item in lists[0].find_all("li", recursive=False)]
        values = [_text(item) for item in lists[1].find_all("li", recursive=False)]
        contacts.update(zip(labels, values))
    labels = {"위치": "location", "전화번호": "phone", "팩스번호": "fax"}
    fields = {key: contacts.get(label, "") for label, key in labels.items()}
    # Keep other displayed fields under their source labels before removing
    # the contact layout from the body (e.g. an institution's head or hours).
    fields.update({label: value for label, value in contacts.items()
                   if label not in labels and value})
    return fields


def _widget_rows(session, department_id: int, page_url: str) -> list[dict]:
    with session.post(WIDGET_URL, data={"dept": str(department_id)}, timeout=20,
                      stream=True, headers={"Referer": page_url,
                                            "X-Requested-With": "XMLHttpRequest",
                                            "Accept": "application/json"}) as response:
        if response.status_code != 200:
            raise ValueError(f"HTTP_{response.status_code}")
        if response.url != WIDGET_URL or "json" not in response.headers.get("Content-Type", "").lower():
            raise ValueError("unexpected organization response or redirect")
        payload = bytearray()
        for chunk in response.iter_content(8192):
            if len(payload) + len(chunk) > MAX_JSON_BYTES:
                raise ValueError("organization response exceeds limit")
            payload.extend(chunk)
    body = json.loads(payload)
    rows = body.get("response") if isinstance(body, dict) else None
    if not isinstance(rows, list):
        raise ValueError("organization staff list is invalid")
    return rows


def _staff_fields(raw: dict, retrieved_at: str) -> dict:
    """Use the fields displayed by the widget, preserving responsibility lines."""
    return {"source_record_id": raw.get("mngSeq"), "sort": raw.get("sort"),
            "team": str(raw.get("belongs") or "").strip(),
            "position": str(raw.get("position") or "").strip(),
            "phone": str(raw.get("tel") or "").strip(),
            "responsibilities": str(raw.get("jobDesc") or "").replace("\r\n", "\n").replace("\r", "\n").strip(),
            "source_url": WIDGET_URL, "retrieved_at": retrieved_at}


def collect_main_533_org(session, page_url: str, html: str) -> dict:
    """Fetch staff rows with exactly the widget request visible in page JS."""
    now = datetime.now(timezone.utc).isoformat()
    result = {"status": "needs_review", "review_status": "pending",
              "page_id": 533, "url": page_url, "source_url": WIDGET_URL,
              "retrieved_at": now, "warnings": [], "staff": []}
    if page_url != PAGE_URL:
        result["warnings"].append("ORG_PAGE_URL_UNVERIFIED")
        return result
    soup = BeautifulSoup(html, "lxml")
    root = soup.select_one("#subCont .content_wrap")
    if root is None:
        result["warnings"].append("ORG_CONTENT_ROOT_MISSING")
        return result
    widget = root.select_one(".getOrgMngList[data-id]")
    if widget is None or widget.get("data-id") != str(DEPARTMENT_ID):
        result["warnings"].append("ORG_WIDGET_ID_UNVERIFIED")
        return result
    title = _text(root.select_one("h4.subNameH4"))
    contact = _contact_fields(root)
    if title != "앵커사업부 소개" or not all(contact.values()):
        result["warnings"].append("ORG_STATIC_CONTENT_UNVERIFIED")
    result.update(title="앵커사업부", section_title=title,
                  contact=contact, department_id=DEPARTMENT_ID)

    try:
        rows = _widget_rows(session, DEPARTMENT_ID, page_url)
        if not rows:
            raise ValueError("organization staff list is empty or invalid")
        seen = set()
        for raw in rows:
            if (not isinstance(raw, dict) or raw.get("dept") != DEPARTMENT_ID
                    or raw.get("deptNm") != "앵커사업부"):
                raise ValueError("staff row belongs to unexpected department")
            record_id = raw.get("mngSeq")
            if not isinstance(record_id, int) or record_id in seen:
                raise ValueError("missing or duplicate staff record ID")
            seen.add(record_id)
            staff = _staff_fields(raw, now)
            # The public page displays these four fields. Names and e-mail
            # exist in some API records but are not displayed by this widget.
            if not staff["team"] or not staff["position"] or not staff["phone"]:
                result["warnings"].append("ORG_STAFF_FIELD_MISSING")
            result["staff"].append(staff)
        result["staff"].sort(key=lambda item: (item["sort"] if isinstance(item["sort"], int) else 10**9,
                                               item["source_record_id"]))
    except Exception as exc:
        result["warnings"].append(f"ORG_WIDGET_FAILED: {type(exc).__name__}: {exc}")
    result["staff_count"] = len(result["staff"])
    if result["staff_count"] and not result["warnings"]:
        result["status"] = "collected"
    return result


def _target_url(href: str, source_url: str) -> str | None:
    parsed = urlsplit(urljoin(source_url, href))
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username
            or parsed.password or parsed.port not in {None, 443}
            or not (parsed.hostname == "pknu.ac.kr" or parsed.hostname.endswith(".pknu.ac.kr"))):
        return None
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path or "/", parsed.query, ""))


def _fetch_html(session, url: str) -> tuple[str, str]:
    """Follow same-site redirects only; reject navigation to another homepage."""
    current = url
    for _ in range(4):
        with session.get(current, timeout=15, stream=True, allow_redirects=False) as response:
            if response.status_code in {301, 302, 303, 307, 308}:
                target = _target_url(response.headers.get("Location", ""), current)
                if not target or urlsplit(target).hostname != urlsplit(url).hostname:
                    raise ValueError("organization page redirects to a different site")
                current = target
                continue
            response.raise_for_status()
            if "html" not in response.headers.get("Content-Type", "").lower():
                raise ValueError("organization page is not HTML")
            payload = bytearray()
            for chunk in response.iter_content(16384):
                if len(payload) + len(chunk) > MAX_HTML_BYTES:
                    raise ValueError("organization page exceeds size limit")
                payload.extend(chunk)
            return payload.decode("utf-8-sig"), current
    raise ValueError("organization page redirect limit exceeded")


def parse_organization_intro(html: str, url: str) -> dict:
    """Extract the current introduction body; record its links without following."""
    soup = BeautifulSoup(html, "lxml")
    root = (soup.select_one("#subCont .content_wrap") or soup.select_one("#sbCont .sub-content")
            or soup.select_one("#sbCont") or soup.select_one("#subCont")
            or soup.select_one(".sub-content") or soup.select_one("#content .content_wrap"))
    if root is None:
        raise ValueError("organization introduction content container missing")
    title = _text(soup.title) or _text(root.select_one("h3, h4"))
    references = []
    seen = set()
    for anchor in root.select("a[href]"):
        href = anchor["href"].strip()
        if href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        target = urljoin(url, href)
        if urlsplit(target).scheme not in {"http", "https"} or target in seen:
            continue
        seen.add(target)
        label = _text(anchor)
        references.append({"label": label, "url": target, "followed": False,
                           "role": "homepage_shortcut" if "홈페이지" in label else "reference"})
    widgets = list(dict.fromkeys(int(widget["data-id"])
                   for widget in root.select(".getOrgMngList[data-id]")
                   if str(widget["data-id"]).isdigit()))
    contact = _contact_fields(root)
    body = deepcopy(root)
    for comment in body.find_all(string=lambda value: isinstance(value, Comment)):
        comment.extract()
    for element in body.select("script, style, noscript, .subMenu, .subTab, .subTitle, .edtDay, "
                               ".getOrgMngList, .paging, .brdSch, .brdList, form, nav, footer, "
                               ".con05_sub_wrap, .con03_bottom_l"):
        element.decompose()
    for anchor in body.select("a"):
        if _text(anchor).replace(" ", "") in {"홈페이지", "홈페이지바로가기"}:
            anchor.decompose()
    # College lists wrap each li in an a rather than putting the a inside the li.
    for anchor in body.select("ul > a, ol > a"):
        if anchor.find("li", recursive=False):
            anchor.unwrap()
    # Some editors leave a blank trailing row. Remove it only when no row
    # spans depend on its position; malformed grids otherwise remain flagged.
    for table in body.select("table"):
        if not table.select("[rowspan]"):
            for row in table.select("tr"):
                if (row.find_parent("table") is table
                        and not row.find(["th", "td"], recursive=False) and not _text(row)):
                    row.decompose()
    warnings = []
    try:
        structure = extract_structure(body)
    except ValueError as exc:
        warnings.append(f"INTRO_STRUCTURE_UNVERIFIED: {exc}")
        text = re.sub(r"\n{3,}", "\n\n", body.get_text("\n", strip=True))
        structure = {"sections": [{"heading": "", "level": 0,
                     "blocks": [{"type": "paragraph", "text": text}]}],
                     "table_count": 0, "flow_count": 0, "organization_chart_count": 0,
                     "content": text}
    labels = {"location": "위치", "phone": "전화번호", "fax": "팩스번호"}
    if any(contact.values()):
        structure["sections"].insert(0, {"heading": "연락처", "level": 1, "blocks": [
            {"type": "paragraph", "text": "\n".join(f"{labels.get(key, key)}: {value}"
                                                    for key, value in contact.items() if value)}]})
        structure["content"] = render_sections(structure["sections"])
    return {"title": title, "url": url, "content": structure["content"],
            "contact": contact, "structured_content": structure,
            "reference_links": references, "widget_ids": widgets, "warnings": warnings}


def _intro_navigation(html: str, url: str, organization_name: str) -> list[str]:
    """Select introduction menus within a directly linked external site."""
    soup = BeautifulSoup(html, "lxml")
    root_path = urlsplit(url).path.rstrip("/")
    candidates = []
    for anchor in soup.select("a[href]"):
        label = _text(anchor)
        compact = label.replace(" ", "")
        if "홈페이지" in label or "바로가기" in label:
            continue
        if not (compact in {"기관소개", "센터소개", "조직및업무안내", "주요사업", "담당자안내", "조직안내"}
                or (organization_name.replace(" ", "") in compact and "소개" in compact)):
            continue
        target = _target_url(anchor["href"], url)
        if (target and target != url and urlsplit(target).hostname == urlsplit(url).hostname
                and (not root_path or urlsplit(target).path.startswith(root_path + "/"))
                and target not in candidates):
            candidates.append(target)
    return candidates[:4]


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _append_staff(document: dict, staff: list[dict]) -> None:
    document["staff"] = staff
    document["staff_count"] = len(staff)
    if not staff:
        return
    headers = ["소속", "직위", "전화번호", "담당업무"]
    rows = [[item[key] for key in ("team", "position", "phone", "responsibilities")] for item in staff]
    table = {"type": "table", "caption": "", "row_count": len(rows) + 1,
             "column_count": 4, "rows": [
                 [{"row": row_index, "column": column, "rowspan": 1, "colspan": 1,
                   "header": row_index == 0, "text": value}
                  for column, value in enumerate(row)]
                 for row_index, row in enumerate([headers, *rows])]}
    structure = document["structured_content"]
    structure["sections"].append({"heading": "직원 및 업무 안내", "level": 1, "blocks": [table]})
    structure["table_count"] += 1
    structure["content"] = render_sections(structure["sections"])
    document["content"] = structure["content"]


def _collect_intro_staff(session, document: dict, page_url: str) -> None:
    """Collect only the public staff widgets embedded in an introduction."""
    staff = []
    for department_id in document.pop("widget_ids", []):
        try:
            rows = _widget_rows(session, department_id, page_url)
            if any(not isinstance(row, dict) or row.get("dept") != department_id for row in rows):
                raise ValueError("staff belongs to an unexpected department")
            staff.extend(_staff_fields(row, datetime.now(timezone.utc).isoformat()) for row in rows)
        except Exception as exc:
            document["warnings"].append(f"ORG_WIDGET_FAILED: {type(exc).__name__}: {exc}")
    _append_staff(document, staff)


def crawl_intro_page(session: requests.Session, page_id: int,
                     full_resync: bool = False) -> tuple[CrawlStats, dict[str, Any]]:
    """Collect a registered introduction and staff table, recording body links only."""
    route = ROUTES[page_id]
    target = page_json(PROJECT_ROOT, page_id)
    stats = CrawlStats(discovered=1, requested=1)
    detail = {"page_id": page_id, "route": "organization_intro", "url": route.url}
    try:
        response = fetch(session, route.url)
        if not is_html_response(response, route.url):
            raise ValueError(f"introduction_page_unverified: HTTP {response.status_code}, {response.url}")
        document = parse_organization_intro(response.text, route.url)
        _collect_intro_staff(session, document, route.url)
        source_path = page_root(PROJECT_ROOT, page_id) / "html" / f"main_{page_id}.html"
        source_path.parent.mkdir(parents=True, exist_ok=True)
        source_path.write_text(response.text, encoding="utf-8")
        document.update(
            page_id=page_id, category=route.category_path[0], subcategory=route.category_path[-1],
            type="page", retrieved_at=datetime.now(timezone.utc).isoformat(),
            source_html=source_path.relative_to(PROJECT_ROOT).as_posix(),
            status="needs_review" if document["warnings"] or not document["content"] else "collected",
            collection_scope={"body": True, "public_staff_widgets": True,
                              "homepage_shortcuts": "record_only", "reference_links": "record_only",
                              "download_attachments": False},
        )
        previous = target.is_file()
        if previous:
            prior = json.loads(target.read_text(encoding="utf-8"))
            if prior.get("page_id") == page_id and prior.get("url") == route.url:
                for key in ("organization_links", "organization_name"):
                    if key in prior:
                        document[key] = prior[key]
        _write_json(target, document)
        if previous:
            stats.updated += 1
        else:
            stats.new += 1
        detail.update(status="success" if document["status"] == "collected" else "needs_review",
                      output=target.relative_to(PROJECT_ROOT).as_posix(),
                      characters=len(document["content"]), staff_count=document["staff_count"],
                      table_count=document["structured_content"]["table_count"],
                      reference_link_count=len(document["reference_links"]),
                      warnings=document["warnings"])
    except (requests.RequestException, ValueError, OSError) as exc:
        stats.failed += 1
        detail.update(status="failed", reason=str(exc),
                      retryable=isinstance(exc, requests.RequestException))
    return stats, detail


def collect_organization_chart(session, page_url: str, html: str, project_root: Path,
                               *, full_resync: bool = False, follow_links: bool = True) -> dict:
    """Link chart nodes to cached or newly collected introduction documents."""
    match = re.fullmatch(r"/main/(\d+)", urlsplit(page_url).path)
    if not match or urlsplit(page_url).hostname != "www.pknu.ac.kr":
        raise ValueError("organization chart URL is not a main website page")
    page_id = int(match.group(1))
    soup = BeautifulSoup(html, "lxml")
    chart = soup.select_one("#subCont .system_conts")
    if chart is None:
        raise ValueError("organization chart container missing")
    parsed = parse_system_organization_chart(chart)
    nodes = []
    for original in parsed["nodes"]:
        node = dict(original)
        parent = nodes[node["parent_order"] - 1] if node["parent_order"] else None
        node["path"] = [*(parent["path"] if parent else []), node["text"]]
        key = json.dumps([page_url, node["path"], node.get("href")], ensure_ascii=False)
        node["id"] = "org:" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
        node["parent_id"] = parent["id"] if parent else None
        node["url"] = _target_url(node["href"], page_url) if node["href"] else None
        node["children"] = []
        if parent:
            parent["children"].append(node["id"])
        nodes.append(node)
    if len({node["id"] for node in nodes}) != len(nodes):
        raise ValueError("organization chart has ambiguous duplicate nodes")
    destination = page_root(project_root, page_id)
    introduction_root = destination / "소개안내"
    html_path = destination / "html" / f"main_{page_id}.html"
    html_path.parent.mkdir(parents=True, exist_ok=True)
    html_path.write_text(html, encoding="utf-8")
    documents = []
    target_urls = dict.fromkeys(node["url"] for node in nodes if node["url"])
    if len(target_urls) > 200:
        raise ValueError("organization chart exceeds linked page limit")
    for url in target_urls if follow_links else []:
        members = [node for node in nodes if node["url"] == url]
        linked_page = re.fullmatch(r"/main/(\d+)", urlsplit(url).path)
        internal = urlsplit(url).hostname == "www.pknu.ac.kr" and linked_page is not None
        linked_id = int(linked_page.group(1)) if internal else None
        stem = f"main_{linked_id}" if internal else "site_" + hashlib.sha256(url.encode()).hexdigest()[:12]
        intro_route = ROUTES.get(linked_id) if internal else None
        registered_intro = intro_route is not None and intro_route.handler == "organization_intro"
        document_root = page_root(project_root, linked_id) if registered_intro else introduction_root
        path = document_root / "json" / "pages" / f"{stem}.json"
        reused = False
        try:
            cached = path
            if internal and not path.exists() and page_json(project_root, linked_id).is_file():
                cached = page_json(project_root, linked_id)
            if (cached.is_file() and not full_resync
                    and json.loads(cached.read_text(encoding="utf-8")).get("status") == "collected"):
                document = json.loads(cached.read_text(encoding="utf-8"))
                if document.get("url") != url:
                    raise ValueError("cached introduction URL mismatch")
                path = cached
                reused = True
                if not document.get("content") and document.get("contact"):
                    labels = {"location": "위치", "phone": "전화번호", "fax": "팩스번호"}
                    content = "\n".join(f"{labels.get(field, field)}: {value}"
                                        for field, value in document["contact"].items() if value)
                    document["structured_content"] = {"sections": [{"heading": document["title"] + " 소개",
                        "level": 1, "blocks": [{"type": "paragraph", "text": content}]}],
                        "table_count": 0, "flow_count": 0, "organization_chart_count": 0, "content": content}
                    document["content"] = content
                    _append_staff(document, document.get("staff", []))
                    document.update(category="대학소개", subcategory="조직도", type="page")
            else:
                time.sleep(0.2)
                source_html, final_url = _fetch_html(session, url)
                source_path = document_root / "html" / f"{stem}.html"
                source_path.parent.mkdir(parents=True, exist_ok=True)
                source_path.write_text(source_html, encoding="utf-8")
                navigation = _intro_navigation(source_html, final_url, members[0]["text"]) if not internal else []
                sources = []
                if navigation:
                    for index, target in enumerate(navigation, 1):
                        intro_html, intro_url = _fetch_html(session, target)
                        intro_path = source_path.with_name(f"{stem}_intro_{index}.html")
                        intro_path.write_text(intro_html, encoding="utf-8")
                        sources.append(parse_organization_intro(intro_html, intro_url))
                else:
                    sources.append(parse_organization_intro(source_html, final_url))
                document = sources[0]
                if len(sources) > 1:
                    for source in sources[1:]:
                        document["structured_content"]["sections"].extend(source["structured_content"]["sections"])
                        for key in ("table_count", "flow_count", "organization_chart_count"):
                            document["structured_content"][key] += source["structured_content"][key]
                        document["reference_links"].extend(source["reference_links"])
                        document["warnings"].extend(source["warnings"])
                    document["content"] = render_sections(document["structured_content"]["sections"])
                    document["structured_content"]["content"] = document["content"]
                _collect_intro_staff(session, document, final_url)
                document.update(page_id=linked_id, url=url, final_url=final_url,
                                source_pages=[source["url"] for source in sources],
                                category="대학소개", subcategory="조직도", type="page",
                                retrieved_at=datetime.now(timezone.utc).isoformat(),
                                source_html=source_path.relative_to(project_root).as_posix(),
                                status="needs_review" if document["warnings"] or not document["content"] else "collected")
            if registered_intro:
                document.update(category=intro_route.category_path[0],
                                subcategory=intro_route.category_path[-1])
            document["organization_links"] = [{"chart_url": page_url,
                "chart_json": page_json(project_root, page_id).relative_to(project_root).as_posix(),
                "node_id": node["id"], "path": node["path"]} for node in members]
            document["organization_name"] = members[0]["text"]
            _write_json(path, document)
            reference = {"url": url, "page_id": linked_id, "status": document.get("status", "collected"),
                         "document_json": path.relative_to(project_root).as_posix(), "reused": reused,
                         "characters": len(document.get("content", "")),
                         "staff_count": document.get("staff_count", 0),
                         "warnings": document.get("warnings", [])}
            for node in members:
                node["document_json"] = reference["document_json"]
                node["introduction_status"] = reference["status"]
            log.info("[ORG] %s: %s (%s)", members[0]["text"], reference["status"], "reused" if reused else "saved")
        except Exception as exc:
            reference = {"url": url, "page_id": linked_id, "status": "failed",
                         "error": f"{type(exc).__name__}: {exc}"}
            for node in members:
                node["introduction_status"] = "failed"
            log.warning("[ORG] %s: %s", members[0]["text"], reference["error"])
        documents.append(reference)
    edges = [{"from": node["parent_id"], "to": node["id"], "kind": node["kind"]}
             for node in nodes if node["parent_id"]]
    structure = {"sections": [{"heading": "조직도", "level": 1, "blocks": [parsed]}],
                 "organization_chart_count": 1, "table_count": 0, "flow_count": 0}
    structure["content"] = render_sections(structure["sections"])
    return {"page_id": page_id, "url": page_url, "title": _text(soup.title) or "조직도",
            "category": "대학소개", "subcategory": "조직도", "type": "page",
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
            "status": "needs_review" if any(doc["status"] in {"failed", "needs_review"} for doc in documents) else "collected",
            "content": structure["content"], "structured_content": structure,
            "source_html": html_path.relative_to(project_root).as_posix(),
            "nodes": nodes, "edges": edges, "node_count": len(nodes), "edge_count": len(edges),
            "linked_page_count": len(documents), "documents": documents,
            "failed_count": sum(doc["status"] == "failed" for doc in documents),
            "reused_count": sum(doc.get("reused", False) for doc in documents),
            "collection_scope": {"node_introductions": follow_links, "homepage_shortcuts": "record_only",
                                 "introduction_reference_links": "record_only", "download_attachments": False}}


def crawl_org_page(session: requests.Session, page_id: int = 533,
                   full_resync: bool = False) -> tuple[CrawlStats, dict[str, Any]]:
    """Collect a registered organization chart or public staff introduction."""
    from scripts.crawlers.main.routes import ROUTES

    url = f"{BASE_URL}/main/{page_id}"
    target = page_json(PROJECT_ROOT, page_id)
    stats = CrawlStats(discovered=1, requested=1)
    detail: dict[str, Any] = {"page_id": page_id, "route": "organization", "url": url,
                              "data_path": target.relative_to(PROJECT_ROOT).as_posix()}
    staff_only = ROUTES[page_id].options.get("organization_mode", "chart") == "staff"
    try:
        if not staff_only and target.is_file() and not full_resync:
            cached = json.loads(target.read_text(encoding="utf-8"))
            if (cached.get("url") == url and cached.get("status") == "collected"
                    and all((PROJECT_ROOT / item["document_json"]).is_file()
                            for item in cached.get("documents", []))):
                stats.requested = 0
                stats.unchanged = 1
                detail.update(status="unchanged", node_count=cached["node_count"],
                              linked_page_count=cached["linked_page_count"])
                return stats, detail
        response = fetch(session, url)
        if not is_html_response(response, url):
            raise ValueError(f"organization_page_unverified: HTTP {response.status_code}, {response.url}")
        if staff_only:
            data = collect_main_533_org(session, url, response.text)
            if not data.get("staff_count") or not data.get("contact"):
                raise ValueError("organization_content_unverified: " + "; ".join(data["warnings"]))
        else:
            data = collect_organization_chart(session, url, response.text, PROJECT_ROOT,
                                               full_resync=full_resync,
                                               follow_links=ROUTES[page_id].follow_links)
        previous = target.is_file()
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(target)
        if staff_only:
            detail.update(status="success" if data["status"] == "collected" else "partial_success",
                          staff_count=data["staff_count"], contact=data["contact"],
                          review_status=data["review_status"], warnings=data["warnings"])
        else:
            detail.update(status="success" if data["status"] == "collected" else "needs_review",
                          node_count=data["node_count"], edge_count=data["edge_count"],
                          linked_page_count=data["linked_page_count"],
                          failed_count=data["failed_count"], reused_count=data["reused_count"])
        if previous:
            stats.updated += 1
        else:
            stats.new += 1
    except (requests.RequestException, ValueError, OSError) as exc:
        stats.failed += 1
        detail.update(status="failed", reason=str(exc),
                      retryable=isinstance(exc, requests.RequestException))
    return stats, detail
