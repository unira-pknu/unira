"""Notice lists use dates and scope, never the top semantic-search matches."""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, time

from .query_plan import KST, SearchTask
from .source_scope import checked_rows, scope_reason

logger = logging.getLogger(__name__)
PAGE_SIZE = 100
MAX_PAGES = 5
MAX_OPEN_CANDIDATES = 50
SOURCE_FIELDS = "id,title,url,parent_url,metadata"


@dataclass
class ListingResult:
    rows: list[dict]
    answer: str


def timestamp(value, *, end_of_day: bool = False) -> datetime | None:
    if not value:
        return None
    value = str(value).strip()
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if len(value) == 10:
            dt = datetime.combine(dt.date(), time.max if end_of_day else time.min)
        return dt.replace(tzinfo=KST) if dt.tzinfo is None else dt.astimezone(KST)
    except ValueError:
        return None


def posted_at(source: dict) -> datetime | None:
    m = source.get("metadata") or {}
    return next((dt for key in ("published_at", "posted_at", "date") if (dt := timestamp(m.get(key)))), None)


def _boundary(metadata: dict, keys: tuple[str, ...], end: bool = False) -> datetime | None:
    return next((dt for key in keys if (dt := timestamp(metadata.get(key), end_of_day=end))), None)


def application_period(source: dict, text: str) -> tuple[datetime, datetime] | None:
    """Only application-labelled ranges; posting/semester/document dates do not count."""
    m = source.get("metadata") or {}
    start = _boundary(m, ("application_start", "application_start_at", "recruitment_start", "apply_start"))
    end = _boundary(m, ("application_end", "application_end_at", "recruitment_end", "apply_end", "deadline"), True)
    if start and end:
        return (start, end) if start <= end else None
    # Keep dates with their labels. Do not treat 서류제출/가구원동의 as the application deadline.
    label = re.compile(r"(?:신청|접수|모집)\s*(?:기간|일시)\s*[:：]?\s*([^\n]{0,200})")
    date = r"(?:(20\d{2})\s*[.년/-]\s*)?(\d{1,2})\s*[.월/-]\s*(\d{1,2})\s*[.일]?\s*(?:\([월화수목금토일]\))?\s*(?:(\d{1,2})\s*[:시]\s*(\d{2})?\s*분?)?"
    period = re.compile(date + r"\s*(?:~|∼|～|부터)\s*" + date)
    found = []
    for match in label.finditer(text):
        hit = period.search(match[1])
        if not hit:
            continue
        sy, sm, sd, sh, smin, ey, em, ed, eh, emin = hit.groups()
        published = posted_at(source)
        year = int(sy or ey) if sy or ey else (published.year if published else None)
        if year is None:
            continue
        try:
            start = datetime(year, int(sm), int(sd), int(sh or 0), int(smin or 0), tzinfo=KST)
            end_year = int(ey) if ey else year + (int(em) < int(sm))
            end = datetime(end_year, int(em), int(ed), int(eh or 23), int(emin or (59 if not eh else 0)), 59 if not eh else 0, tzinfo=KST)
        except ValueError:
            continue
        if start <= end:
            found.append((start, end))
    # Conflicting application periods require original-document confirmation.
    unique = set(found)
    return next(iter(unique)) if len(unique) == 1 else None


def _sources(client, table: str, task: SearchTask):
    """Page a date-ordered, department-filtered source catalogue (not chunk rankings)."""
    for page in range(MAX_PAGES):
        # Canonical published_at is set by ingestion. PostgreSQL DESC defaults
        # to NULLS FIRST; undated attachments must not consume every page.
        query = client.table(table).select(SOURCE_FIELDS).eq("status", "active").filter("metadata->>published_at", "not.is", "null")
        if task.target.department_id:
            query = query.eq("department_id", task.target.department_id)
        if task.conditions.topic and task.conditions.kind == "open":
            topic = task.conditions.topic
            pattern = topic.replace("장학", "%장학") if topic.endswith("장학") and topic != "장학" else topic
            query = query.ilike("title", "%" + pattern + "%")
        response = query.order("metadata->>published_at", desc=True, nullsfirst=False).order("id").range(page * PAGE_SIZE, (page + 1) * PAGE_SIZE - 1).execute()
        rows = response.data or []
        yield rows, len(rows) < PAGE_SIZE
        if len(rows) < PAGE_SIZE:
            break


def list_notices(client, task: SearchTask) -> ListingResult:
    conditions = task.conditions
    tables = ["rag_sources"] if task.target.department_id else ["pknu_notice_sources"]
    sources: list[dict] = []
    exhausted = True
    for table in tables:
        exhausted_table = False
        for page, last in _sources(client, table, task):
            for source in page:
                source = {**source, "metadata": {**(source.get("metadata") or {}), "catalog_table": table}}
                m = source["metadata"]
                # Crawler is_notice means a *pinned* post, not "is a notice".
                if m.get("source_kind") in {"static_page", "rule", "attachment", "form"}:
                    continue
                date = posted_at(source)
                if not date or date > conditions.as_of or not (source.get("url") or source.get("parent_url")) or scope_reason(source, task):
                    continue
                if conditions.year and date.year != conditions.year:
                    # Explicit terms are checked against notice titles below.
                    if not conditions.term:
                        continue
                if conditions.term:
                    term = re.search(r"(20\d{2})\s*(?:학년도|년)?\s*[-.]?\s*([12])\s*학기", source["title"])
                    if not term or (int(term[1]), term[2]) != (conditions.year, conditions.term):
                        continue
                sources.append(source)
            if last:
                exhausted_table = True
                break
            # Latest queries can stop after a full page only when enough dated
            # notices remain; SQL already ordered the whole filtered catalogue.
            if conditions.kind == "latest" and len({s.get("url") or s["id"] for s in sources}) >= conditions.limit:
                break
            if conditions.kind == "open" and len(sources) >= MAX_OPEN_CANDIDATES:
                break
        exhausted &= exhausted_table
    sources.sort(key=lambda s: (posted_at(s), str(s["id"])), reverse=True)
    unique, seen = [], set()
    for source in sources:
        identity = (source.get("url") or source.get("parent_url") or str(source["id"])).split("#")[0]
        if identity not in seen:
            seen.add(identity)
            unique.append(source)
    rows, unknown = [], 0
    for source in unique[:MAX_OPEN_CANDIDATES] if conditions.kind == "open" else unique:
        m = dict(source.get("metadata") or {})
        content = f"게시일: {posted_at(source).isoformat()}\n공지: {source['title']}"
        if conditions.kind == "open":
            chunk_table = str(m["catalog_table"]).replace("_sources", "_chunks")
            chunks = client.table(chunk_table).select("content").eq("source_id", source["id"]).order("chunk_index").limit(31).execute().data or []
            if len(chunks) > 30:
                unknown += 1
                continue
            text = "\n".join(c["content"] for c in chunks)
            if scope_reason({**source, "content": text}, task):
                continue
            period = application_period(source, text)
            if period is None:
                unknown += 1
                continue
            start, end = period
            if not start <= conditions.as_of <= end:
                continue
            m.update(application_start=start.isoformat(), application_end=end.isoformat())
            content += f"\n신청기간: {start.isoformat()} ~ {end.isoformat()}"
        m["doc_title"], m["doc_url"] = source["title"], source.get("url") or source.get("parent_url") or ""
        rows.extend(checked_rows([{**source, "source_id": source["id"], "content": content, "metadata": m,
                                   "similarity": 1.0, "_source_row": True}], task))
        if len(rows) >= conditions.limit:
            break
    if not rows:
        answer = "게시일과 적용 대상이 확인된 공지를 찾지 못했습니다." if conditions.kind == "latest" else "확인한 공지에서 현재 신청 기간과 대상 조건을 함께 확인할 수 있는 모집을 찾지 못했습니다. 현재 가능한 모집이 없다는 뜻은 아닙니다."
        return ListingResult([], answer)
    logger.info("notice listing kind=%s department=%s sources=%d rows=%d unknown_periods=%d exhausted=%s",
                conditions.kind, task.target.department_id, len(unique), len(rows), unknown, exhausted)
    scope = task.target.department or task.target.school
    lead = (f"{scope}의 게시일이 확인된 최신 공지 {len(rows)}개입니다." if conditions.kind == "latest" else
            f"{conditions.as_of.astimezone(KST):%Y-%m-%d %H:%M} KST 기준, 확인한 공지 중 신청 기간이 진행 중인 목록입니다. 개인별 지원 자격은 각 공지의 조건을 확인해야 합니다.")
    lines = [lead, ""]
    for index, row in enumerate(rows, 1):
        m = row["metadata"]
        detail = (f"신청 마감 {m['application_end']}" if conditions.kind == "open" else f"게시일 {posted_at(row):%Y-%m-%d}")
        category = str(m.get("subcategory") or "")
        category_note = f" ({category})" if category else ""
        lines.append(f"{index}. {row['title']}{category_note} — {detail} [{index}]")
    if conditions.kind == "open" and (unknown or not exhausted):
        lines += ["", "신청 기간을 확인하지 못한 공지는 목록에서 제외했습니다. 수집된 공지 범위에서 확인한 결과입니다."]
    elif conditions.kind == "latest" and len(rows) < conditions.limit:
        lines += ["", f"요청하신 {conditions.limit}개 중 조건을 확인한 {len(rows)}개만 표시했습니다."]
    return ListingResult(rows, "\n".join(lines))
