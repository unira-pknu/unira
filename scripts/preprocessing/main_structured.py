"""Expose primary calendar/tuition JSON as text without indexing run summaries."""
from __future__ import annotations

from collections import defaultdict
from datetime import date
import json
from pathlib import Path

from scripts.crawlers.common.schema import apply_common_schema


def structured_kind(data: object) -> str | None:
    if not isinstance(data, dict):
        return None
    if (data.get("page_id") == 31 and data.get("url") == "https://www.pknu.ac.kr/main/31"
            and isinstance(data.get("events"), list) and data["events"]):
        return "academic_calendar"
    if (data.get("page_url") == "https://www.pknu.ac.kr/main/102"
            and data.get("source_url") == "https://irumi.pknu.ac.kr/uni/enro/uniEnroView"
            and all(isinstance(data.get(k), list) for k in ("tuition", "departments", "installments"))
            and any(data[k] for k in ("tuition", "departments", "installments"))):
        return "tuition"
    return None


def is_structured_main_document(path: Path) -> bool:
    if not (path.name.startswith("main_31_") or path.name == "main_102.json"):
        return False
    try:
        return structured_kind(json.loads(path.read_text(encoding="utf-8-sig"))) is not None
    except (OSError, ValueError):
        return False


def _context(row: dict) -> str:
    parts = [f"{row.get('academic_year')}학년도 {row.get('semester')}학기"]
    for field in ("university_or_college", "college", "department_group", "department", "category"):
        value = row.get(field)
        if value and str(value) not in parts:
            parts.append(str(value))
    return " / ".join(parts)


def _money(value: object) -> str:
    if value is None:
        return "원본 미기재"
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"Invalid collected tuition amount: {value!r}")
    return f"{value:,}원"


def _tuition_lines(data: dict) -> list[str]:
    lines = ["분납 합계 비교는 산술 검사이며, 납부 대상 자격의 독립 검증을 뜻하지 않습니다."]
    for row in data["tuition"]:
        student = {"new": "신입생", "continuing": "재학생"}.get(row.get("student_type"), "원본 미지정")
        lines.append(f"정규 등록금: {_context(row)} / 학생구분 {student}. "
                     f"입학금 {_money(row.get('admission_fee_krw'))}, 수업료 {_money(row.get('tuition_krw'))}, "
                     f"총액 {_money(row.get('total_krw'))}. 공식 대조 상태: {row.get('official_validation', 'not_checked')}.")
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for row in data["installments"]:
        key = tuple(row.get(k) for k in ("academic_year", "semester", "university_or_college",
                    "category_code", "grade", "installment_count", "domestic_foreign_type"))
        groups[key].append(row)
    for rows in groups.values():
        first = rows[0]
        payments = "; ".join(f"{r.get('installment_sequence')}회차 {_money(r.get('total_krw'))}"
                             for r in sorted(rows, key=lambda r: r.get("installment_sequence", 0)))
        warnings = sorted({str(w) for r in rows for w in r.get("warnings", [])})
        states = sorted({str(r.get("regular_sum_check", "not_checked")) for r in rows})
        lines.append(f"등록금 분할납부: {_context(first)} / 조회 학년 {first.get('grade')} / "
                     f"{first.get('installment_count')}회 분납 / 내외국인 구분 {first.get('domestic_foreign_type')}. "
                     f"{payments}. 정규 등록금 합계 비교 상태: {', '.join(states)}. "
                     f"검토 표시: {', '.join(warnings) if warnings else '없음'}.")
    for row in data["departments"]:
        lines.append(f"등록금 계열 분류: {_context(row)}. 학과코드 {row.get('department_code')}, "
                     f"계열코드 {row.get('category_code')}. 공식 대조 상태: {row.get('official_validation', 'not_checked')}.")
    for row in data.get("verified_department_tuition", []):
        lines.append(f"공식 대조된 학과별 등록금: {_context(row)} / 학생구분 {row.get('student_type')}. "
                     f"입학금 {_money(row.get('admission_fee_krw'))}, 수업료 {_money(row.get('tuition_krw'))}, "
                     f"총액 {_money(row.get('total_krw'))}. 공식 대조 상태: {row.get('official_validation')}.")
    return lines


def normalize_structured_main(data: dict) -> dict:
    kind = structured_kind(data)
    if kind is None:
        return data
    if kind == "academic_calendar":
        year = data["year"]
        lines = []
        for event in data["events"]:
            start, end = date.fromisoformat(event["start_date"]), date.fromisoformat(event["end_date"])
            if end < start:
                raise ValueError("Accepted calendar event has an inverted date range")
            description = str(event.get("description") or "").strip()
            extra = f" / {description}" if description and description != event["title"] else ""
            lines.append(f"{year}학년도 학사일정: {event['title']}{extra}. "
                         f"시작 {event['start_date']} {event.get('start_time', '')} / "
                         f"종료 {event['end_date']} {event.get('end_time', '')}.")
        page_id, title, section = 31, data["title"], "학사안내"
        warnings = ["REJECTED_CALENDAR_EVENTS_RETAINED_IN_RAW"] if data.get("rejected_events") else []
        semester = None
    else:
        year, semester = data["academic_year"], data["semester"]
        lines = _tuition_lines(data)
        page_id, title, section = 102, f"{year}학년도 {semester}학기 등록금 조회 자료", "학사정보"
        warnings = list(data.get("warnings", []))
    if not lines:
        raise ValueError("Primary structured data has no accepted records")
    url = f"https://www.pknu.ac.kr/main/{page_id}"
    return apply_common_schema(
        {"title": title, "url": url, "content": "\n".join(lines),
         "category": "대학생활", "subcategory": section, "attachments": [],
         "crawl": {"status": "partial_success" if warnings else "success", "warnings": warnings}},
        source_dataset="pknu_main", source_id=f"main:{page_id}:structured:{year}:{semester or 0}",
        source_site="https://www.pknu.ac.kr", document_type="guide",
        content_source="pknu_main_structured_json", crawled_at=data.get("retrieved_at"),
        metadata={"document_kind": kind, "academic_year": year, "semester": semester,
                  "source_review_status": data.get("status"), "data_source_url": data.get("source_url")},
    )
