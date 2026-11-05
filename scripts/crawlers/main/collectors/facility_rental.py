"""Collect all building/room selections offered by the facility rental widget."""

from __future__ import annotations

from bs4 import BeautifulSoup
from datetime import datetime, timezone
import json
import re
import requests
from urllib.parse import urljoin, urlsplit

from scripts.crawlers.common.main_static import extract_structure
from scripts.crawlers.common.schema import CrawlStats
from scripts.crawlers.main.common.documents import content_hash
from scripts.crawlers.main.common.http import fetch, is_html_response
from scripts.crawlers.main.common.storage import BASE_URL, PROJECT_ROOT, log, write_json
from scripts.crawlers.main.paths import page_json, page_root
from scripts.crawlers.main.routes import ROUTES


PARSER_VERSION = 1
ROOM_LIST_URL = f"{BASE_URL}/facility/roomList.do"
DETAIL_URL = f"{BASE_URL}/facility/view.do"


def _text(value) -> str:
    return re.sub(r"\s+", " ", str(value or "").replace("\u200b", "")
                  .replace("\ufeff", "")).strip()


def parse_building_options(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    root = soup.select_one("#subCont #buildingList")
    scripts = "\n".join(script.get_text() for script in soup.select("script:not([src])"))
    if root is None or soup.select_one("#roomList") is None:
        raise ValueError("building or room selector is missing")
    for endpoint, parameter, selector in (("roomList", "cd", "buildingList"),
                                          ("view", "no", "roomList")):
        pattern = (rf"url\s*:\s*['\"]/facility/{endpoint}\.do['\"]\s*,.*?"
                   rf"data\s*:\s*\{{\s*{parameter}\s*:\s*\$\(['\"]#{selector} option:selected['\"]\)\.val\(\)")
        if not re.search(pattern, scripts, re.S):
            raise ValueError("facility widget source or selection parameters changed")
    buildings, seen = [], set()
    for option in root.select("option[value]"):
        code, name = option["value"].strip(), _text(option.get_text(" ", strip=True))
        if not code:
            continue
        if not code.isdigit() or not name or code in seen:
            raise ValueError("invalid or duplicate building option")
        seen.add(code)
        buildings.append({"building_code": code, "name": name})
    if not buildings:
        raise ValueError("building selector is empty")
    return buildings


def parse_room_options(payload: dict, building: dict) -> list[dict]:
    rows = payload.get("list") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise ValueError("invalid room selection response")
    rooms, seen = [], set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("invalid room option")
        code, name = str(row.get("facSeq", "")), _text(row.get("facNm"))
        if (not code.isdigit() or not name or code in seen
                or _text(row.get("buildingNm")) != building["name"]):
            raise ValueError("room option is duplicated or belongs to another building")
        seen.add(code)
        rooms.append({"facility_id": code, "name": name})
    return rooms


def parse_facility_detail(payload: dict, building: dict, room: dict) -> dict:
    result = payload.get("result") if isinstance(payload, dict) else None
    if (not isinstance(result, dict) or str(result.get("facSeq")) != room["facility_id"]
            or str(result.get("buildingCd")) != building["building_code"]
            or _text(result.get("buildingNm")) != building["name"]
            or _text(result.get("facNm")) != room["name"]):
        raise ValueError("facility details disagree with the selected building or room")
    body = BeautifulSoup(f'<div class="content_wrap">{result.get("cont") or ""}</div>', "lxml")
    for element in body.select("script, style, noscript"):
        element.decompose()
    root = body.select_one(".content_wrap")
    structure = extract_structure(root) if root.get_text(" ", strip=True) or root.select_one("img") else None
    references = []
    for anchor in root.select("a[href]"):
        url = urljoin(BASE_URL, anchor["href"])
        if urlsplit(url).scheme in {"http", "https"}:
            references.append({"text": _text(anchor.get_text(" ", strip=True)), "url": url, "followed": False})
    files = payload.get("resultFile")
    if not isinstance(files, list):
        raise ValueError("invalid facility photo list")
    images = []
    for photo in files:
        if (not isinstance(photo, dict) or str(photo.get("facSeq")) != room["facility_id"]
                or not str(photo.get("fafSeq", "")).isdigit()):
            raise ValueError("photo belongs to another facility")
        images.append({"name": _text(photo.get("fileNm")),
                       "source_url": f"{BASE_URL}/imageView.do?target=facility&cd={photo['fafSeq']}",
                       "downloaded": False})
    seats, area, use = (_text(result.get(field)) for field in ("seats", "area", "use"))
    intro = "\n".join((f"{building['name']} · {room['name']}", f"좌석수: {seats}",
                        f"면적: {area} m²", f"용도: {use}"))
    return {**room, "seats": seats, "area": area, "area_unit": "m²", "use": use,
            "content": intro + ("\n\n" + structure["content"] if structure else ""),
            "structured_content": structure, "images": images, "reference_links": references,
            "updated_at": _text(result.get("updDe")), "status": "collected",
            "source": {"url": DETAIL_URL, "method": "POST", "parameters": {"no": room["facility_id"]}}}


def _api(session, url: str, parameters: dict) -> dict:
    response = fetch(session, url, method="POST", data=parameters, allow_redirects=False)
    if (response.status_code != 200 or response.url != url
            or "json" not in response.headers.get("Content-Type", "").lower()):
        raise ValueError("unexpected facility API response")
    return response.json()


def crawl_facility_rental(session, page_id: int, full_resync: bool = False):
    route = ROUTES[page_id]
    target = page_json(PROJECT_ROOT, page_id)
    stats = CrawlStats(discovered=1)
    detail = {"page_id": page_id, "route": "facility_rental", "url": route.url,
              "output": target.relative_to(PROJECT_ROOT).as_posix()}
    try:
        if target.is_file() and not full_resync:
            cached = json.loads(target.read_text(encoding="utf-8"))
            if (cached.get("url") == route.url and cached.get("status") == "collected"
                    and cached.get("parser_version") == PARSER_VERSION):
                stats.unchanged = 1
                detail.update(status="unchanged", building_count=cached["building_count"],
                              facility_count=cached["facility_count"])
                return stats, detail
        stats.requested += 1
        response = fetch(session, route.url, allow_redirects=False)
        if not is_html_response(response, route.url):
            raise ValueError("unexpected facility page response")
        buildings = parse_building_options(response.text)
        source = page_root(PROJECT_ROOT, page_id) / "html" / f"main_{page_id}.html"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(response.text, encoding="utf-8")
        warnings = []
        for building in buildings:
            building.update(facilities=[], source={"url": ROOM_LIST_URL, "method": "POST",
                                                  "parameters": {"cd": building["building_code"]}})
            try:
                stats.requested += 1
                rooms = parse_room_options(_api(session, ROOM_LIST_URL, building["source"]["parameters"]), building)
                building["status"] = "collected" if rooms else "empty"
                for room in rooms:
                    try:
                        stats.requested += 1
                        facility = parse_facility_detail(_api(session, DETAIL_URL, {"no": room["facility_id"]}), building, room)
                    except (requests.RequestException, ValueError) as exc:
                        facility = {**room, "status": "failed", "error": str(exc)}
                        warnings.append({"building_code": building["building_code"],
                                         "facility_id": room["facility_id"], "error": str(exc)})
                    building["facilities"].append(facility)
                log.info("[FACILITY] %s: %s selections", building["name"], len(rooms))
            except (requests.RequestException, ValueError) as exc:
                building.update(status="failed", error=str(exc))
                warnings.append({"building_code": building["building_code"], "error": str(exc)})
        content = "\n\n".join(
            f"{building['name']}\n" + ("등록된 호실이 없습니다" if building["status"] == "empty" else
                "\n\n".join(facility.get("content") or f"{facility['name']}: 상세정보 수집 실패"
                            for facility in building["facilities"])) for building in buildings)
        document = {"id": f"main_{page_id}", "slug": f"main_{page_id}", "page_id": page_id,
                    "url": route.url, "title": "시설대관검색", "type": "page", "source_site": BASE_URL,
                    "source_dataset": "pknu_student_life", "category": route.category_path[0],
                    "subcategory": route.category_path[-1], "category_path": list(route.category_path),
                    "content": content, "content_hash": content_hash(content), "buildings": buildings,
                    "building_count": len(buildings), "facility_count": sum(len(b["facilities"]) for b in buildings),
                    "attachments": [], "failed_count": len(warnings), "warnings": warnings,
                    "status": "needs_review" if warnings else "collected", "parser_version": PARSER_VERSION,
                    "content_source": "facility_selection_api", "source_html": source.relative_to(PROJECT_ROOT).as_posix(),
                    "crawled_at": datetime.now(timezone.utc).isoformat()}
        previous = target.is_file()
        write_json(target, document)
        stats.updated = int(previous)
        stats.new = int(not previous)
        detail.update(status="needs_review" if warnings else "success", building_count=len(buildings),
                      facility_count=document["facility_count"], failed_count=len(warnings))
    except (requests.RequestException, ValueError, OSError) as exc:
        stats.failed += 1
        detail.update(status="failed", reason=str(exc))
    return stats, detail
