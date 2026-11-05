'Main website collectors navigation.'

from __future__ import annotations
from bs4 import BeautifulSoup
from datetime import datetime
from scripts.crawlers.main.collectors.static_pages import _STATIC_REMOVE, crawl_static_pages, static_page_subcategory
from scripts.crawlers.common.main_static import clean_text
from scripts.crawlers.common.schema import CrawlStats
from scripts.crawlers.main.common.http import fetch, is_html_response
from scripts.crawlers.main.common.storage import BASE_URL, PROJECT_ROOT, write_json
from scripts.crawlers.main.paths import page_json, page_root
from scripts.crawlers.main.routes import REDIRECT_PAGE_TARGETS, ROUTES
from typing import Any
from urllib.parse import urljoin, urlparse
import json
import re
import requests


def parse_page_choices(html: str, page_id: int) -> list[dict]:
    """Read a configured selection list; only registered sibling pages may be followed."""
    route = ROUTES[page_id]
    options = route.options
    soup = BeautifulSoup(html, "lxml")
    body = soup.select_one("#subCont")
    container = body.select_one(options["selection_selector"]) if body else None
    if container is None:
        raise ValueError("page selection list missing")
    choices = []
    for item in container.select(options.get("selection_item_selector", "li")):
        anchor = item.find("a")
        if anchor is None:
            raise ValueError("page selection item has no anchor")
        name_node = item.select_one(options["choice_name_selector"]) if options.get("choice_name_selector") else anchor
        number_node = item.select_one(options["choice_number_selector"]) if options.get("choice_number_selector") else None
        name = clean_text(name_node) if name_node else ""
        if not name:
            raise ValueError("page selection name missing")
        choice = {"order": len(choices) + 1, "name": name, "number": clean_text(number_node) if number_node else None,
                  "selected": "active" in item.get("class", []) or "current" in item.get("class", []),
                  "url": None, "page_id": None, "status": "unavailable", "followed": False}
        href = str(anchor.get("href") or "").strip()
        if href:
            target = urljoin(route.url, href)
            parsed = urlparse(target)
            match = re.fullmatch(r"/main/(\d+)", parsed.path)
            destination = ROUTES.get(int(match[1])) if match else None
            if (parsed.scheme != "https" or parsed.netloc != "www.pknu.ac.kr" or parsed.query or parsed.fragment
                    or destination is None or destination.handler != "static"
                    or destination.category_path != route.category_path):
                raise ValueError(f"page selection destination outside registered siblings: {target}")
            choice.update(url=target, page_id=destination.page_id, status="pending")
        choices.append(choice)
    if not choices or not any(item["url"] for item in choices):
        raise ValueError("page selection destinations missing")
    return choices


def crawl_linked_pages(session: requests.Session, state: dict, page_id: int,
                       full_resync: bool) -> tuple[CrawlStats, dict]:
    """Collect explicitly selected static siblings and save one navigable page index."""
    route = ROUTES[page_id]
    output = page_json(PROJECT_ROOT, page_id)
    stats = CrawlStats(discovered=1)
    detail = {"page_id": page_id, "route": "linked_pages", "url": route.url,
              "output": output.relative_to(PROJECT_ROOT).as_posix()}
    try:
        if not route.follow_links:
            raise ValueError("linked-pages collection requires follow_links")
        if output.is_file() and not full_resync:
            cached = json.loads(output.read_text(encoding="utf-8"))
            if cached.get("status") == "collected" and all(
                    (PROJECT_ROOT / choice["document_json"]).is_file()
                    for choice in cached.get("choices", []) if choice.get("url")):
                stats.unchanged = 1
                detail.update(status="unchanged", choice_count=len(cached["choices"]),
                              collected_count=cached["collected_count"],
                              unavailable_count=cached["unavailable_count"])
                return stats, detail
        stats.requested = 1
        response = fetch(session, route.url)
        if not is_html_response(response, route.url):
            raise ValueError(f"page selection response unverified: {response.status_code} {response.url}")
        choices = parse_page_choices(response.text, page_id)
        soup = BeautifulSoup(response.text, "lxml")
        title = route.options.get("document_title") or clean_text(soup.select_one("#subCont .subTitle"))
        source = page_root(PROJECT_ROOT, page_id) / "html" / f"main_{page_id}.html"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(response.text, encoding="utf-8")
        existing = {}
        for path in output.parent.glob("*.json"):
            try:
                document = json.loads(path.read_text(encoding="utf-8"))
                if document.get("type") == "page" and document.get("content"):
                    existing[document.get("url")] = path
            except (OSError, ValueError):
                continue
        pending = tuple(dict.fromkeys(choice["page_id"] for choice in choices if choice["url"]
                                      and (full_resync or choice["url"] not in existing)))
        results = []
        stats.add(crawl_static_pages(session, state, full_resync, pending, route_results=results))
        by_id = {item["page_id"]: item for item in results}
        for choice in choices:
            if not choice["url"]:
                continue
            result = by_id.get(choice["page_id"])
            if result and result.get("output") and result["status"] in {"new", "updated", "unchanged"}:
                path = PROJECT_ROOT / result["output"]
            elif not full_resync and choice["url"] in existing:
                path = existing[choice["url"]]
                stats.unchanged += 1
                stats.discovered += 1
            else:
                choice.update(status="failed", reason=(result or {}).get("reason", "result missing"))
                continue
            choice.update(status="collected", followed=bool(result),
                          document_json=path.relative_to(PROJECT_ROOT).as_posix())
            document = json.loads(path.read_text(encoding="utf-8"))
            link = {"page_id": page_id, "url": route.url, "document_json": detail["output"],
                    "order": choice["order"], "name": choice["name"]}
            links = document.setdefault("page_group_links", [])
            if link not in links:
                links.append(link)
                write_json(path, document)
        collected = sum(choice["status"] == "collected" for choice in choices)
        failed = sum(choice["status"] == "failed" for choice in choices)
        unavailable = sum(choice["status"] == "unavailable" for choice in choices)
        selected = next((choice for choice in choices if choice["selected"]), None)
        document = {"page_id": page_id, "title": title, "url": route.url,
                    "category": route.category_path[0], "subcategory": static_page_subcategory(page_id),
                    "status": "needs_review" if failed else "collected", "choices": choices,
                    "content": "\n".join(f"{choice['number'] or choice['order']} {choice['name']}"
                                           + (" [연결 주소 없음]" if not choice["url"] else "") for choice in choices),
                    "selected_document_json": selected.get("document_json") if selected else None,
                    "collected_count": collected, "unavailable_count": unavailable, "failed_count": failed,
                    "source_html": source.relative_to(PROJECT_ROOT).as_posix(),
                    "crawled_at": datetime.now().isoformat()}
        existed = output.is_file()
        write_json(output, document)
        outcome = "updated" if existed else "new"
        setattr(stats, outcome, getattr(stats, outcome) + 1)
        detail.update(status="needs_review" if failed else outcome, choice_count=len(choices),
                      collected_count=collected, unavailable_count=unavailable, children=results)
    except (requests.RequestException, ValueError, KeyError, OSError) as exc:
        stats.failed += 1
        detail.update(status="failed", reason=f"{type(exc).__name__}: {exc}")
    return stats, detail


def inspect_link_hub(session: requests.Session, page_id: int) -> dict[str, Any]:
    url = f"{BASE_URL}/main/{page_id}"
    result: dict[str, Any] = {"page_id": page_id, "route": "link_hub", "url": url}
    try:
        resp = fetch(session, url)
        if not is_html_response(resp, url):
            raise ValueError(f"link_hub_response_unverified: HTTP {resp.status_code}, {resp.url}")
        soup = BeautifulSoup(resp.text, "lxml")
        body = soup.select_one("#subCont")
        if body is None:
            raise ValueError("link_hub_body_missing")
        body = BeautifulSoup(str(body), "lxml")
        for element in body.select(_STATIC_REMOVE):
            element.decompose()
        links = []
        seen = set()
        for element in body.select("a[href], iframe[src]"):
            raw_url = (element.get("href") or element.get("src") or "").strip()
            if not raw_url or raw_url.startswith("#") or urlparse(raw_url).scheme.lower() in {"javascript", "mailto", "tel", "data"}:
                continue
            target = urljoin(url, raw_url)
            if urlparse(target).scheme not in {"http", "https"} or target in seen:
                continue
            seen.add(target)
            links.append({"label": element.get_text(" ", strip=True) or "embedded content",
                          "url": target, "kind": "iframe" if element.name == "iframe" else "link"})
        embedded_pdfs = sorted({str(element.get("data-id")) for element in body.select(".uploadPdf[data-id]")})
        if not links and not embedded_pdfs:
            raise ValueError("link_hub_targets_missing")
        result.update(status="success", links=links, embedded_pdf_media_ids=embedded_pdfs)
    except (requests.RequestException, ValueError) as exc:
        result.update(status="failed", reason=str(exc), retryable=isinstance(exc, requests.RequestException))
    return result


def inspect_redirect(session: requests.Session, page_id: int) -> dict[str, Any]:
    url = f"{BASE_URL}/main/{page_id}"
    expected = REDIRECT_PAGE_TARGETS[page_id]
    result: dict[str, Any] = {"page_id": page_id, "route": "redirect", "url": url,
                              "expected_destination": expected}
    try:
        first = fetch(session, url, stream=True, allow_redirects=False)
        location = urljoin(url, first.headers.get("Location", ""))
        result.update(http_status=first.status_code, destination=location)
        first.close()
        if first.status_code not in {301, 302, 303, 307, 308} or location != expected:
            raise ValueError("redirect_destination_unverified")
        if not urlparse(location).hostname.endswith(".pknu.ac.kr"):
            raise ValueError("redirect_destination_outside_pknu")
        destination = fetch(session, location, stream=True)
        result.update(destination_http_status=destination.status_code,
                      final_url=destination.url,
                      content_type=destination.headers.get("Content-Type", ""))
        destination.close()
        if not urlparse(result["final_url"]).hostname.endswith(".pknu.ac.kr"):
            raise ValueError("redirect_final_url_outside_pknu")
        if destination.status_code in {401, 403}:
            result.update(status="access_restricted", retryable=False)
        elif destination.status_code == 200:
            result.update(status="reachable", content_verified=False)
        else:
            result.update(status="failed", reason="destination_http_error",
                          retryable=destination.status_code == 429 or destination.status_code >= 500)
    except (requests.RequestException, ValueError) as exc:
        result.update(status="failed", reason=str(exc), retryable=isinstance(exc, requests.RequestException))
    return result
