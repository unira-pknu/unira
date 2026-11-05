"""Collect the public monthly academic schedule behind /main/31.

Run from the repository root:
    python -m scripts.crawlers.main.collectors.academic_calendar --year 2026
    python -m scripts.crawlers.main.collectors.academic_calendar --year 2026 --months 9

The page HTML contains an empty schedule table. Its getMonthList JavaScript
loads the actual rows from /getScheduleList.do, one month at a time.
"""

from __future__ import annotations
from bs4 import BeautifulSoup
from calendar import monthrange
from datetime import date, datetime, timezone
from pathlib import Path
from scripts.crawlers.main.common.http import build_session
from zoneinfo import ZoneInfo
import argparse
import json
import re
import requests
import time


ROOT = Path(__file__).resolve().parents[4]
PAGE_URL = "https://www.pknu.ac.kr/main/31"
API_URL = "https://www.pknu.ac.kr/getScheduleList.do"
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
SITE_ID_RE = re.compile(r"\bsteId\s*:\s*['\"]([A-Za-z0-9]+)['\"]")
DATE_RE = re.compile(r"\d{8}")


def _read_response(response: requests.Response) -> bytes:
    if response.status_code != 200:
        raise ValueError(f"HTTP {response.status_code}: {response.url}")
    payload = bytearray()
    for chunk in response.iter_content(16384):
        if len(payload) + len(chunk) > MAX_RESPONSE_BYTES:
            raise ValueError("response exceeds size limit")
        payload.extend(chunk)
    return bytes(payload)


def _site_id(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    if soup.select_one("#subCont #scheduleList") is None:
        raise ValueError("academic schedule table is missing")
    script = next((tag.get_text() for tag in soup.find_all("script")
                   if "function getMonthList" in tag.get_text()
                   and '"/getScheduleList.do"' in tag.get_text()), "")
    match = SITE_ID_RE.search(script)
    if not match:
        raise ValueError("academic schedule API or site ID has changed")
    return match.group(1)


def _date(value: object, field: str) -> str:
    if not isinstance(value, str) or not DATE_RE.fullmatch(value):
        raise ValueError(f"invalid {field}: {value!r}")
    return date(int(value[:4]), int(value[4:6]), int(value[6:8])).isoformat()


def _clean_text(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return re.sub(r"\s+", " ", BeautifulSoup(value, "lxml").get_text(" ", strip=True)).strip()


def parse_month(payload: bytes, *, year: int, month: int,
                site_id: str) -> tuple[list[dict], list[dict]]:
    """Validate the JSON rows before they can be written as schedule data."""
    data = json.loads(payload.decode("utf-8"))
    rows = data.get("list") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise ValueError("schedule response has no list")
    first = date(year, month, 1)
    last = date(year, month, monthrange(year, month)[1])
    events = []
    rejected = []
    for row in rows:
        if not isinstance(row, dict) or row.get("steId") != site_id:
            raise ValueError("schedule row has an unexpected site ID")
        event_id = row.get("schSeq")
        title = _clean_text(row.get("schNm"))
        try:
            start = _date(row.get("strDt"), "strDt")
            end = _date(row.get("endDt"), "endDt")
            if (isinstance(event_id, bool) or not isinstance(event_id, int)
                    or event_id <= 0 or not title):
                raise ValueError("invalid ID or title")
            if end < start:
                raise ValueError("end date precedes start date")
            if date.fromisoformat(start) > last or date.fromisoformat(end) < first:
                raise ValueError("event does not overlap requested month")
        except ValueError as exc:
            rejected.append({"source_record_id": event_id, "title": title,
                             "raw_start_date": row.get("strDt"),
                             "raw_end_date": row.get("endDt"),
                             "reason": str(exc)})
            continue
        events.append({"source_record_id": event_id, "title": title,
                       "description": _clean_text(row.get("schCont")),
                       "start_date": start, "end_date": end,
                       "start_time": str(row.get("strTm") or "").strip(),
                       "end_time": str(row.get("endTm") or "").strip()})
    return events, rejected


def collect_calendar(session: requests.Session, year: int, months: list[int],
                     *, delay: float = 0.2) -> dict:
    if not 2000 <= year <= 2100 or not months or any(not 1 <= m <= 12 for m in months):
        raise ValueError("year or months are out of range")
    months = sorted(set(months))
    with session.get(PAGE_URL, timeout=30, stream=True, allow_redirects=False) as response:
        if response.url != PAGE_URL:
            raise ValueError("academic schedule page redirected")
        html = _read_response(response).decode("utf-8")
    site_id = _site_id(html)
    by_id: dict[int, dict] = {}
    rejected_by_key: dict[tuple, dict] = {}
    monthly_counts = {}
    for index, month in enumerate(months):
        if index:
            time.sleep(delay)
        with session.post(API_URL,
                          data={"year": str(year), "month": f"{month:02d}",
                                "day": str(monthrange(year, month)[1]),
                                "action": "month", "steId": site_id},
                          headers={"Referer": PAGE_URL,
                                   "X-Requested-With": "XMLHttpRequest",
                                   "Accept": "application/json"},
                          timeout=30, stream=True, allow_redirects=False) as response:
            if response.url != API_URL or "json" not in response.headers.get("Content-Type", "").lower():
                raise ValueError(f"unexpected schedule response: {response.url}")
            events, rejected = parse_month(_read_response(response), year=year,
                                           month=month, site_id=site_id)
        monthly_counts[f"{year}-{month:02d}"] = len(events)
        for item in rejected:
            key = (item["source_record_id"], item["raw_start_date"],
                   item["raw_end_date"], item["reason"])
            if key not in rejected_by_key:
                rejected_by_key[key] = {**item, "source_months": []}
            rejected_by_key[key]["source_months"].append(f"{year}-{month:02d}")
        for event in events:
            event_id = event["source_record_id"]
            if event_id in by_id:
                if {k: v for k, v in by_id[event_id].items() if k != "source_months"} != event:
                    raise ValueError(f"conflicting schedule record {event_id}")
                by_id[event_id]["source_months"].append(f"{year}-{month:02d}")
            else:
                by_id[event_id] = {**event, "source_months": [f"{year}-{month:02d}"]}
    events = sorted(by_id.values(), key=lambda item: (item["start_date"], item["end_date"],
                                                     item["source_record_id"]))
    rejected_events = sorted(rejected_by_key.values(), key=lambda item: (
        str(item["raw_start_date"]), str(item["source_record_id"])))
    return {"page_id": 31, "title": "학사일정", "url": PAGE_URL,
            "source_url": API_URL, "site_id": site_id, "year": year,
            "months_requested": months, "retrieved_at": datetime.now(timezone.utc).isoformat(),
            "status": "needs_review" if rejected_events else ("collected" if events else "empty"),
            "monthly_counts": monthly_counts, "event_count": len(events), "events": events,
            "rejected_count": len(rejected_events), "rejected_events": rejected_events}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--year", type=int, default=datetime.now(ZoneInfo("Asia/Seoul")).year)
    parser.add_argument("--months", nargs="+", type=int, metavar="MONTH",
                        help="Months to collect (1-12); defaults to the full calendar year")
    parser.add_argument("--output", type=Path, help="Output JSON path under this repository")
    args = parser.parse_args()
    months = args.months or list(range(1, 13))
    if not 2000 <= args.year <= 2100 or any(not 1 <= m <= 12 for m in months):
        parser.error("year must be 2000-2100 and months must be 1-12")
    suffix = "" if set(months) == set(range(1, 13)) else "_m" + "-".join(
        f"{month:02d}" for month in sorted(set(months)))
    from scripts.crawlers.main.paths import page_json
    target = args.output or page_json(ROOT, 31, f"main_31_{args.year}{suffix}.json")
    target = target.resolve()
    try:
        target.relative_to(ROOT)
    except ValueError:
        parser.error("output must be inside this repository")
    try:
        with build_session(PAGE_URL) as session:
            result = collect_calendar(session, args.year, months)
    except (requests.RequestException, ValueError, UnicodeError, json.JSONDecodeError) as exc:
        parser.exit(1, f"academic calendar crawl failed: {exc}\n")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(target)
    print(json.dumps({"output": target.relative_to(ROOT).as_posix(),
                      "status": result["status"], "event_count": result["event_count"],
                      "rejected_count": result["rejected_count"],
                      "monthly_counts": result["monthly_counts"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
