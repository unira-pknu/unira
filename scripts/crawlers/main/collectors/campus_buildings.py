"""Collect campus buildings and the floor/room details shown by their widget."""

from __future__ import annotations

from bs4 import BeautifulSoup
from datetime import datetime, timezone
import json
import re
import requests

from scripts.crawlers.common.schema import CrawlStats
from scripts.crawlers.main.common.documents import content_hash
from scripts.crawlers.main.common.http import fetch, is_html_response
from scripts.crawlers.main.common.storage import BASE_URL, PROJECT_ROOT, log, write_json
from scripts.crawlers.main.paths import page_json, page_root
from scripts.crawlers.main.routes import ROUTES


PARSER_VERSION = 1
DETAIL_URL = f"{BASE_URL}/buildingInfoAjax.do"
FLOOR_TYPES = {"0": ("above_ground", "지상"), "1": ("basement", "지하"),
               "2": ("rooftop", "옥탑")}


def _text(value) -> str:
    soup = BeautifulSoup(str(value or ""), "lxml")
    for element in soup.select("script, style"):
        element.decompose()
    return re.sub(r"\s+", " ", soup.get_text(" ", strip=True)
                  .replace("\u200b", "").replace("\ufeff", "")).strip()


def _floor_key(row: dict) -> tuple[str, int]:
    floor_type, number = str(row.get("layerTyp")), str(row.get("layer"))
    if floor_type == "1":
        number = re.sub(r"^B", "", number, flags=re.I)
    if floor_type not in FLOOR_TYPES or not re.fullmatch(r"\d+", number):
        raise ValueError("invalid floor")
    return floor_type, int(number)


def parse_campus_buildings(html: str) -> dict:
    """Use the visible list, never map markers, coordinates or search filters."""
    soup = BeautifulSoup(html, "lxml")
    root = soup.select_one(".campusMap_conts")
    if root is None:
        raise ValueError("campus building list is missing")
    scripts = "\n".join(script.get_text() for script in soup.select("script:not([src])"))
    widget = re.search(r"function\s+buildingInfoAjax\s*\(code\)\s*\{(.*?)success\s*:",
                       scripts, re.S)
    if not widget or not re.search(r'url\s*:\s*[\'"]/buildingInfoAjax\.do[\'"]', widget[1]):
        raise ValueError("building detail widget source changed")
    campus = re.search(r'var\s+stat\s*=\s*[\'"]([DY])[\'"]', widget[1])
    if not campus or not re.search(r"data\s*:\s*\{\s*code\s*:\s*code\s*,\s*stat\s*:\s*stat\s*\}", widget[1]):
        raise ValueError("building detail widget parameters changed")
    name = _text(root.select_one(".campusMap_title p"))
    name = re.sub(r"\s*건물번호$", "", name).strip()
    if name != {"D": "대연캠퍼스", "Y": "용당캠퍼스"}[campus[1]]:
        raise ValueError("campus heading and widget disagree")
    buildings = []
    seen = set()
    for entry in root.select(".campusList_scroll > .campusList_conts"):
        labels = entry.select(".campusList_conts_open > ul > li")
        detail = entry.select_one("a.campusList_detail[onclick]")
        code = re.search(r"buildingInfoAjax\(['\"](B\d+)['\"]\)",
                         detail.get("onclick", "") if detail else "")
        if len(labels) != 2 or not code or code[1] in seen:
            raise ValueError("invalid or duplicate building list entry")
        number, title = (_text(label) for label in labels)
        if not number or not title:
            raise ValueError("building number or name is missing")
        seen.add(code[1])
        buildings.append({"building_code": code[1], "building_number": number, "name": title})
    if not buildings:
        raise ValueError("campus building list is empty")
    return {"campus": name, "campus_code": campus[1], "buildings": buildings}


def parse_building_details(payload: dict, building: dict, campus_code: str) -> dict:
    """Keep only visible building/floor/room fields, matching the page's status filter."""
    code = building["building_code"]
    response = payload.get("response") if isinstance(payload, dict) else None
    if not isinstance(response, dict) or response.get("campusCode") != code:
        raise ValueError("building detail response belongs to another building")
    for key in ("deps1", "max", "deps2"):
        if not isinstance(response.get(key), list) or any(not isinstance(r, dict) for r in response[key]):
            raise ValueError(f"invalid building detail field: {key}")
    matches = [row for row in response["deps1"]
               if row.get("buildingCode") == code and str(row.get("stat")) != "1"]
    if len(matches) != 1:
        raise ValueError("active building identity is missing or duplicated")
    identity = matches[0]
    if (identity.get("campusTyp") != campus_code
            or _text(identity.get("buildingName")) != building["name"]
            or _text(identity.get("buildingNo")) != building["building_number"]):
        raise ValueError("building details disagree with the visible list")
    floors = {}
    for row in response["max"]:
        if str(row.get("stat")) == "1":
            continue
        floor_type, number = _floor_key(row)
        if row.get("buildingCode") != code:
            raise ValueError("floor belongs to another building")
        key = (floor_type, number)
        if key in floors:
            raise ValueError("duplicate floor")
        floors[key] = {"floor_type": FLOOR_TYPES[floor_type][0], "floor_number": number,
                       "label": f"{FLOOR_TYPES[floor_type][1]}{number}층", "rooms": []}
        if str(row["layer"]) != str(number):
            floors[key]["source_floor_number"] = str(row["layer"])
    for row in response["deps2"]:
        if str(row.get("stat")) == "1":
            continue
        key = _floor_key(row)
        if row.get("buildingCode") != code or key not in floors:
            raise ValueError("room belongs to an unexpected building or floor")
        floors[key]["rooms"].append({"room_number": _text(row.get("roomNo")),
                                    "name": _text(row.get("roomName"))})
    return {"floors": list(floors.values()), "floor_count": len(floors),
            "room_count": sum(len(floor["rooms"]) for floor in floors.values()),
            "photo_source_url": (f"{BASE_URL}/imageView.do?target=campus&cd={code}"
                                 if identity.get("filePath") and identity.get("svFileNm") else None),
            "detail_source": {"url": DETAIL_URL, "method": "POST",
                              "parameters": {"code": code, "stat": campus_code}},
            "detail_status": "collected"}


def _render(campus: str, buildings: list[dict]) -> str:
    lines = [f"{campus} 건물 안내"]
    for building in buildings:
        lines.extend(["", f"{building['building_number']} {building['name']}"])
        if building.get("detail_status") != "collected":
            lines.append("상세정보 수집 실패")
        elif not building["floors"]:
            lines.append("등록된 층별·호실 안내 없음")
        for floor in building.get("floors", []):
            lines.append(floor["label"])
            for room in floor["rooms"]:
                lines.append(" · ".join(value for value in (room["room_number"], room["name"]) if value))
    return "\n".join(line.rstrip() for line in lines).strip()


def crawl_campus_buildings(session, page_id: int, full_resync: bool = False):
    """Category callback; completed page results are reused without network requests."""
    route = ROUTES[page_id]
    target = page_json(PROJECT_ROOT, page_id)
    output = target.relative_to(PROJECT_ROOT).as_posix()
    stats = CrawlStats(discovered=1)
    detail = {"page_id": page_id, "route": "campus_buildings", "url": route.url, "output": output}
    try:
        cached = {}
        if target.is_file() and not full_resync:
            cached = json.loads(target.read_text(encoding="utf-8"))
            if (cached.get("url") == route.url and cached.get("status") == "collected"
                    and cached.get("parser_version") == PARSER_VERSION
                    and cached.get("buildings")
                    and cached.get("building_count") == len(cached["buildings"])
                    and all(b.get("detail_status") == "collected" for b in cached["buildings"])):
                stats.unchanged = 1
                detail.update(status="unchanged", building_count=cached["building_count"],
                              floor_count=cached["floor_count"], room_count=cached["room_count"])
                return stats, detail
        stats.requested += 1
        response = fetch(session, route.url, allow_redirects=False)
        if not is_html_response(response, route.url):
            raise ValueError("unexpected campus page response")
        campus = parse_campus_buildings(response.text)
        source = page_root(PROJECT_ROOT, page_id) / "html" / f"main_{page_id}.html"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(response.text, encoding="utf-8")
        buildings, warnings = campus["buildings"], []
        reusable = {building["building_code"]: building for building in cached.get("buildings", [])
                    if building.get("detail_status") == "collected"}
        if cached.get("url") != route.url or cached.get("parser_version") != PARSER_VERSION:
            reusable = {}
        reused_count = 0
        for building in buildings:
            previous_building = reusable.get(building["building_code"])
            if (previous_building and previous_building.get("name") == building["name"]
                    and previous_building.get("building_number") == building["building_number"]
                    and previous_building.get("detail_source", {}).get("parameters", {}).get("stat") == campus["campus_code"]):
                building.update(previous_building)
                reused_count += 1
                continue
            try:
                stats.requested += 1
                response = fetch(session, DETAIL_URL, method="POST", allow_redirects=False,
                                 data={"code": building["building_code"], "stat": campus["campus_code"]})
                if (response.status_code != 200 or response.url != DETAIL_URL
                        or "json" not in response.headers.get("Content-Type", "").lower()):
                    raise ValueError("unexpected building detail HTTP response")
                building.update(parse_building_details(response.json(), building, campus["campus_code"]))
                log.info("[BUILDING] %s %s: %s floors, %s rooms", building["building_number"],
                         building["name"], building["floor_count"], building["room_count"])
            except (requests.RequestException, ValueError) as exc:
                building.update(detail_status="failed", error=str(exc))
                warnings.append({"building_code": building["building_code"], "error": str(exc)})
        content = _render(campus["campus"], buildings)
        document = {"id": f"main_{page_id}", "slug": f"main_{page_id}", "page_id": page_id,
                    "url": route.url, "title": f"{campus['campus']} 건물 안내", "type": "page",
                    "category": route.category_path[0], "subcategory": route.category_path[-1],
                    "category_path": list(route.category_path), "source_site": BASE_URL,
                    "source_dataset": "pknu_student_life", "content": content, "content_hash": content_hash(content),
                    "content_source": "html_and_building_details_api", "attachments": [], "images": [],
                    "campus": campus["campus"], "campus_code": campus["campus_code"], "buildings": buildings,
                    "building_count": len(buildings),
                    "floor_count": sum(b.get("floor_count", 0) for b in buildings),
                    "room_count": sum(b.get("room_count", 0) for b in buildings),
                    "failed_count": len(warnings), "warnings": warnings,
                    "status": "needs_review" if warnings else "collected", "parser_version": PARSER_VERSION,
                    "source_html": source.relative_to(PROJECT_ROOT).as_posix(),
                    "crawled_at": datetime.now(timezone.utc).isoformat()}
        previous = target.is_file()
        write_json(target, document)
        if previous:
            stats.updated = 1
        else:
            stats.new = 1
        detail.update(status="needs_review" if warnings else "success", building_count=len(buildings),
                      floor_count=document["floor_count"], room_count=document["room_count"],
                      failed_count=len(warnings), reused_count=reused_count)
    except (requests.RequestException, ValueError, OSError) as exc:
        stats.failed += 1
        detail.update(status="failed", reason=str(exc))
    return stats, detail
