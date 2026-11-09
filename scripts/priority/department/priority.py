"""Priority scoring policy for department homepage source documents."""

from __future__ import annotations

import re
from collections import Counter
from datetime import date, datetime
from typing import Any, Iterable

TOKEN_PATTERN = re.compile(r"[0-9a-zA-Z\uac00-\ud7a3]+")
DATE_PATTERN = re.compile(
    r"(20\d{2})(?:[.\-/\ub144]|\s*\ud559\ub144\ub3c4)?"
    r"\s*(\d{1,2}(?!\s*\ud559\uae30))?[.\-/\uc6d4]?"
    r"\s*(\d{1,2}(?!\s*\ud559\uae30))?"
)
COMPACT_DATE_PATTERN = re.compile(r"^(20\d{2})(\d{2})(\d{2})$")

# A trailing "-2 hakgi" (2nd semester) marker used to get misread by DATE_PATTERN
# as month "2" (e.g. "2026-2 hakgi" -> 2026-02-01). The lookahead above now blocks
# that; map semester numbers to an approximate month here instead so recency
# scoring still gets a usable date rather than falling back to year-only.
SEMESTER_PATTERN = re.compile(r"(20\d{2})\s*(?:\ub144|\ud559\ub144\ub3c4)?\s*[-.]?\s*([12])\s*\ud559\uae30")
_SEMESTER_MONTH = {1: 3, 2: 9}


def _semester_dates(text: str) -> list[date]:
    dates: list[date] = []
    for year_text, semester_text in SEMESTER_PATTERN.findall(text):
        month = _SEMESTER_MONTH.get(int(semester_text))
        if month is None:
            continue
        try:
            dates.append(date(int(year_text), month, 1))
        except ValueError:
            continue
    return dates
FORM_ATTACHMENT_PATTERN = re.compile(
    "(\ubcc4\uc9c0\\s*(?:\uc81c)?\\s*\\d+(?:\\s*\uc758\\s*\\d+)?\\s*\ud638?\\s*(?:\uc11c\uc2dd)?|"
    "\uc11c\uc2dd\\s*(?:\uc81c)?\\s*\\d+(?:\\s*\uc758\\s*\\d+)?|"
    "\uc11c\uc2dd|"
    "\uc591\uc2dd|"
    "\uc2e0\uccad\uc11c)"
)
APPENDIX_TABLE_PATTERN = re.compile(
    "(\ubcc4\ud45c\\s*(?:\uc81c)?\\s*\\d+(?:\\s*\uc758\\s*\\d+)?\\s*\ud638?)"
)

HIGH_VALUE_PAGE_KEYWORDS = (
    "\ud559\ubd80\uc548\ub0b4",
    "\ud559\uc0ac\uc548\ub0b4",
    "\ud559\ubd80\uc18c\uac1c",
    "\uc804\uacf5\uc18c\uac1c",
    "\ub300\ud559\uc6d0\uc18c\uac1c",
    "\uc878\uc5c5\uc694\uac74",
    "\uad50\uc721\uacfc\uc815",
    "\ubaa8\ub4c8\ud615\uad50\uc721\uacfc\uc815",
)
LOW_BOARD_KEYWORDS = (
    "\uacf5\uc9c0\uc0ac\ud56d",
    "\ud559\uacfc\uacf5\uc9c0",
    "\ub300\ud559\uc6d0\uacf5\uc9c0",
    "\uc0b0\uc5c5\ub300\ud559\uc6d0\uacf5\uc9c0",
    "\uad50\uc721\ub300\ud559\uc6d0\uacf5\uc9c0",
    "\ucde8\uc5c5",
    "\ucc44\uc6a9",
)


def normalize_text(text: str) -> str:
    return " ".join((text or "").replace("\u00a0", " ").lower().split())


def content_features(text: str) -> Counter[str]:
    features: Counter[str] = Counter()
    for token in TOKEN_PATTERN.findall(normalize_text(text)):
        features[f"tok:{token}"] += 1
        if len(token) >= 4:
            for index in range(len(token) - 2):
                features[f"tri:{token[index:index + 3]}"] += 1
    return features


def build_rule_feature_set(rule_contents: Iterable[str]) -> set[str]:
    features: set[str] = set()
    for content in rule_contents:
        features.update(content_features(content))
    return features


def build_reference_feature_set(contents: Iterable[str]) -> set[str]:
    return build_rule_feature_set(contents)


def parse_department_date(value: object) -> date | None:
    if not value:
        return None
    text = str(value).strip()
    semester_dates = _semester_dates(text)
    if semester_dates:
        return max(semester_dates)
    compact = COMPACT_DATE_PATTERN.match(text)
    if compact:
        year, month, day = (int(part) for part in compact.groups())
        try:
            return date(year, month, day)
        except ValueError:
            return None
    for fmt in ("%Y-%m-%d", "%Y.%m.%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    match = DATE_PATTERN.search(text)
    if not match:
        return None
    year_text, month_text, day_text = match.groups()
    try:
        return date(int(year_text), int(month_text or 1), int(day_text or 1))
    except ValueError:
        return None


def extract_latest_date(*values: object) -> date | None:
    dates: list[date] = []
    for value in values:
        text = str(value or "")
        parsed = parse_department_date(text)
        if parsed:
            dates.append(parsed)
        dates.extend(_semester_dates(text))
        for year_text, month_text, day_text in DATE_PATTERN.findall(text):
            try:
                dates.append(date(int(year_text), int(month_text or 1), int(day_text or 1)))
            except ValueError:
                continue
    return max(dates) if dates else None


def recency_score(post_date: date | None, *, today: date | None = None) -> float:
    if post_date is None:
        return 0.35
    today = today or date.today()
    age_days = max(0, (today - post_date).days)
    age_years = age_days / 365.25
    if age_years <= 0.5:
        return 1.0
    if age_years <= 1:
        return 0.9
    if age_years <= 2:
        return 0.75
    if age_years <= 3:
        return 0.55
    if age_years <= 5:
        return 0.30
    return 0.15


def metadata_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return normalize_text(str(value)) in {"1", "true", "yes", "y"}


def document_kind(title: str, metadata: dict[str, Any]) -> str:
    explicit = normalize_text(str(metadata.get("document_kind") or metadata.get("attachment_kind") or ""))
    if explicit in {"post", "form", "appendix_table", "attachment"}:
        return explicit
    if metadata_bool(metadata.get("is_form")):
        return "form"
    if metadata_bool(metadata.get("is_appendix_table")):
        return "appendix_table"

    haystack = normalize_text(
        "\n".join(
            str(value or "")
            for value in (
                title,
                metadata.get("doc_title"),
                metadata.get("source_file"),
                metadata.get("attachment_name"),
                metadata.get("source_path"),
            )
        )
    )
    if APPENDIX_TABLE_PATTERN.search(haystack):
        return "appendix_table"
    if FORM_ATTACHMENT_PATTERN.search(haystack):
        return "form"
    if normalize_text(str(metadata.get("source_kind") or "")) in {"attachment", "file"}:
        return "attachment"
    return "post"


def document_kind_adjustment(kind: str) -> float:
    if kind == "form":
        return -0.12
    if kind == "appendix_table":
        return -0.06
    return 0.0


def page_role_score(title: str, metadata: dict[str, Any]) -> tuple[str, float]:
    haystack = normalize_text(
        "\n".join(
            str(value or "")
            for value in (
                metadata.get("category"),
                metadata.get("subcategory"),
                metadata.get("doc_type"),
                metadata.get("doc_title"),
                metadata.get("source_file"),
                title,
            )
        )
    )
    if any(keyword in haystack for keyword in HIGH_VALUE_PAGE_KEYWORDS):
        return "department_page", 1.0
    if any(keyword in haystack for keyword in LOW_BOARD_KEYWORDS):
        return "notice_or_job_board", 0.45
    return "general_department_content", 0.65


def feature_overlap_score(content: str, reference_features: set[str]) -> tuple[float, int, int]:
    features = content_features(content)
    total = sum(features.values())
    if total <= 0 or not reference_features:
        return 0.0, total, 0
    matched = sum(count for feature, count in features.items() if feature in reference_features)
    return matched / total, total, matched


def calculate_department_priority(
    department_content: str,
    rule_features: set[str],
    main_features: set[str] | None = None,
    metadata: dict | None = None,
    title: str = "",
    today: date | None = None,
) -> tuple[float, dict[str, float | int | str | None]]:
    """Score department-site priority using rule/main overlap, recency, and page role."""

    metadata = metadata or {}
    main_features = main_features or set()
    rule_overlap, feature_count, rule_matched = feature_overlap_score(department_content, rule_features)
    main_overlap, _, main_matched = feature_overlap_score(department_content, main_features)
    if feature_count <= 0:
        return 0.0, {
            "rule": "ce_rule_main_overlap_recency_page_doc_kind_v2",
            "feature_count": 0,
            "rule_matched_feature_count": 0,
            "main_matched_feature_count": 0,
            "rule_content_overlap": 0.0,
            "main_content_overlap": 0.0,
            "recency_score": 0.0,
            "page_role": None,
            "page_role_score": 0.0,
            "document_kind": None,
            "document_kind_adjustment": 0.0,
            "latest_ce_date": None,
        }

    latest_date = extract_latest_date(
        metadata.get("published_at"),
        metadata.get("updated_at"),
        (metadata.get("crawl") or {}).get("crawled_at") if isinstance(metadata.get("crawl"), dict) else None,
        metadata.get("date"),
        metadata.get("crawled_at"),
    )
    recency = recency_score(latest_date, today=today)
    page_role, page_score = page_role_score(title, metadata)
    kind = document_kind(title, metadata)
    kind_adjustment = document_kind_adjustment(kind)
    reference_overlap = max(rule_overlap, main_overlap)

    score = (
        0.38 * reference_overlap
        + 0.27 * recency
        + 0.30 * page_score
        + 0.05 * min(1.0, rule_overlap + main_overlap)
        + kind_adjustment
    )
    score = max(0.0, min(1.0, score))
    return score, {
        "rule": "ce_rule_main_overlap_recency_page_doc_kind_v2",
        "feature_count": feature_count,
        "rule_matched_feature_count": rule_matched,
        "main_matched_feature_count": main_matched,
        "rule_content_overlap": rule_overlap,
        "main_content_overlap": main_overlap,
        "reference_overlap": reference_overlap,
        "recency_score": recency,
        "page_role": page_role,
        "page_role_score": page_score,
        "document_kind": kind,
        "document_kind_adjustment": kind_adjustment,
        "latest_ce_date": latest_date.isoformat() if latest_date else None,
    }
