"""Priority scoring for main RAG datasets such as PKNU notice/student_life."""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Iterable
from datetime import date, datetime
from typing import Any

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

DATASET_BANDS = {
    "pknu_student_life": ("student_life", 0.60, 0.35),
    "pknu_main": ("main", 0.60, 0.35),
    "pknu_syllabus": ("syllabus", 0.35, 0.24),
    "pknu_notice": ("notice", 0.35, 0.24),
}


def normalize_text(value: object) -> str:
    return " ".join(str(value or "").replace("\u00a0", " ").lower().split())


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


def parse_main_date(value: object) -> date | None:
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
        parsed = parse_main_date(text)
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
        return 0.88
    if age_years <= 2:
        return 0.72
    if age_years <= 3:
        return 0.55
    if age_years <= 5:
        return 0.30
    return 0.15


def metadata_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return normalize_text(value) in {"1", "true", "yes", "y"}


def document_kind(title: str, metadata: dict[str, Any]) -> str:
    explicit = normalize_text(metadata.get("document_kind") or metadata.get("attachment_kind"))
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
    if normalize_text(metadata.get("source_kind")) in {"attachment", "file"}:
        return "attachment"
    return "post"


def document_kind_adjustment(kind: str) -> float:
    if kind == "form":
        return -0.12
    if kind == "appendix_table":
        return -0.06
    return 0.0


def source_quality_score(metadata: dict[str, Any], content: str) -> float:
    length_score = min(1.0, math.log10(max(len(content), 10)) / 4.0)
    has_title = bool(normalize_text(metadata.get("doc_title") or metadata.get("source_file")))
    has_url = bool(normalize_text(metadata.get("doc_url") or metadata.get("source_page_url")))
    metadata_score = 0.5 + (0.25 if has_title else 0.0) + (0.25 if has_url else 0.0)
    return min(1.0, 0.65 * length_score + 0.35 * metadata_score)


def rule_overlap_score(content: str, rule_features: set[str]) -> tuple[float, int, int]:
    features = content_features(content)
    total = sum(features.values())
    if total <= 0 or not rule_features:
        return 0.0, total, 0
    matched = sum(count for feature, count in features.items() if feature in rule_features)
    return matched / total, total, matched


def calculate_main_priority(
    *,
    dataset: str,
    content: str,
    rule_features: set[str],
    metadata: dict[str, Any],
    title: str = "",
    today: date | None = None,
) -> tuple[float, dict[str, float | int | str | bool | None]]:
    dataset_kind, band_base, band_span = DATASET_BANDS[dataset]
    overlap, feature_count, matched_count = rule_overlap_score(content, rule_features)
    latest_date = extract_latest_date(
        metadata.get("published_at"),
        metadata.get("updated_at"),
        (metadata.get("crawl") or {}).get("crawled_at") if isinstance(metadata.get("crawl"), dict) else None,
        metadata.get("date"),
        metadata.get("crawled_at"),
        metadata.get("doc_title"),
        metadata.get("source_file"),
        title,
    )
    recency = recency_score(latest_date, today=today)
    source_quality = source_quality_score(metadata, content)
    kind = document_kind(title, metadata)
    kind_adjustment = document_kind_adjustment(kind)

    within_band = 0.45 * overlap + 0.35 * recency + 0.20 * source_quality + kind_adjustment
    within_band = max(0.0, min(1.0, within_band))
    score = max(0.0, min(1.0, band_base + band_span * within_band))
    return score, {
        "rule": "main_dataset_rule_overlap_recency_doc_kind_v1",
        "dataset_kind": dataset_kind,
        "band_base": band_base,
        "band_span": band_span,
        "rule_content_overlap": overlap,
        "feature_count": feature_count,
        "matched_feature_count": matched_count,
        "recency_score": recency,
        "source_quality_score": source_quality,
        "document_kind": kind,
        "document_kind_adjustment": kind_adjustment,
        "latest_date": latest_date.isoformat() if latest_date else None,
        "notice_topic": metadata.get("notice_topic"),
        "content_chars": len(content),
    }
