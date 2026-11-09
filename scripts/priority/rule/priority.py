"""Priority scoring policy for rule documents.

Rule priority is a retrieval-time trust signal. It should not replace semantic
similarity; it should promote more authoritative and current rule sources among
otherwise relevant results.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable
from datetime import date, datetime
from typing import Any

DATE_PATTERN = re.compile(r"(20\d{2})[.\-/\ub144]\s*(\d{1,2})[.\-/\uc6d4]\s*(\d{1,2})")
COMPACT_DATE_PATTERN = re.compile(r"^(20\d{2})(\d{2})(\d{2})$")

DEPRECATED_KEYWORDS = (
    "\ud3d0\uc9c0",
    "\ud3d0\uae30",
    "\uc0ad\uc81c",
    "\uc2e4\ud6a8",
    "\uc885\ub8cc",
)

SCHOOL_RULE_TITLES = (
    "\ubd80\uacbd\ub300\ud559\uad50 \ud559\uce59",
    "\uad6d\ub9bd\ubd80\uacbd\ub300\ud559\uad50 \ud559\uce59",
)
FORM_ATTACHMENT_PATTERN = re.compile(
    "(\ubcc4\uc9c0\\s*(?:\uc81c)?\\s*\\d+(?:\\s*\uc758\\s*\\d+)?\\s*\ud638?\\s*(?:\uc11c\uc2dd)?|"
    "\uc11c\uc2dd\\s*(?:\uc81c)?\\s*\\d+(?:\\s*\uc758\\s*\\d+)?)"
)
APPENDIX_TABLE_PATTERN = re.compile(
    "(\ubcc4\ud45c\\s*(?:\uc81c)?\\s*\\d+(?:\\s*\uc758\\s*\\d+)?\\s*\ud638?)"
)


def normalize_text(value: object) -> str:
    return " ".join(str(value or "").replace("\u00a0", " ").split()).lower()


def parse_rule_date(value: object) -> date | None:
    if not value:
        return None
    text = str(value).strip()
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
    year, month, day = (int(part) for part in match.groups())
    try:
        return date(year, month, day)
    except ValueError:
        return None


def extract_latest_date(*values: object) -> date | None:
    dates: list[date] = []
    for value in values:
        text = str(value or "")
        parsed = parse_rule_date(text)
        if parsed:
            dates.append(parsed)
        for year, month, day in DATE_PATTERN.findall(text):
            try:
                dates.append(date(int(year), int(month), int(day)))
            except ValueError:
                continue
    return max(dates) if dates else None


def authority_score(metadata: dict[str, Any], tree_info: dict[str, Any] | None = None) -> float:
    return authority_band(metadata, tree_info)[1]


def metadata_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return normalize_text(value) in {"1", "true", "yes", "y"}


def document_kind(title: str, metadata: dict[str, Any]) -> str:
    explicit_kind = normalize_text(metadata.get("document_kind") or metadata.get("attachment_kind"))
    if explicit_kind in {"form", "appendix_table", "attachment", "rule_text"}:
        return explicit_kind
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
                metadata.get("source_attachment_path"),
            )
        )
    )
    if APPENDIX_TABLE_PATTERN.search(haystack):
        return "appendix_table"
    if FORM_ATTACHMENT_PATTERN.search(haystack):
        return "form"
    if normalize_text(metadata.get("source_kind")) == "file":
        return "attachment"
    return "rule_text"


def document_kind_adjustment(kind: str) -> float:
    if kind == "form":
        return -0.07
    if kind == "appendix_table":
        return -0.035
    return 0.0


def authority_band(metadata: dict[str, Any], tree_info: dict[str, Any] | None = None) -> tuple[str, float, float]:
    tree_kind = normalize_text(tree_info.get("kind_type") if tree_info else "")
    if tree_kind == "hak":
        return "school_rule", 0.90, 0.099
    if tree_kind == "gyu":
        return "regulation", 0.70, 0.199
    if tree_kind in {"se", "ji"}:
        return "bylaw_guideline", 0.50, 0.199

    subcategory = normalize_text(metadata.get("subcategory"))
    doc_type = normalize_text(metadata.get("doc_type"))

    if subcategory == "school_rule" or doc_type == "hak":
        return "school_rule", 0.90, 0.099
    if subcategory == "regulation" or doc_type == "gyu":
        return "regulation", 0.70, 0.199
    if subcategory in {"bylaw", "guideline"} or doc_type in {"bylaw_guideline", "se", "ji"}:
        return "bylaw_guideline", 0.50, 0.199
    return "unknown", 0.35, 0.149


def is_top_school_rule(title: str, metadata: dict[str, Any], tree_info: dict[str, Any] | None = None) -> bool:
    values = [
        title,
        metadata.get("doc_title"),
        metadata.get("source_file"),
        tree_info.get("title") if tree_info else "",
    ]
    haystack = normalize_text("\n".join(str(value or "") for value in values))
    subcategory = normalize_text(metadata.get("subcategory"))
    doc_type = normalize_text(metadata.get("doc_type"))
    return (
        any(name in haystack for name in SCHOOL_RULE_TITLES)
        and (subcategory == "school_rule" or doc_type == "hak")
    )


def recency_score(rule_date: date | None, *, today: date | None = None) -> float:
    if rule_date is None:
        return 0.45
    today = today or date.today()
    age_days = max(0, (today - rule_date).days)
    age_years = age_days / 365.25
    if age_years <= 1:
        return 1.0
    if age_years <= 3:
        return 0.8
    if age_years <= 7:
        return 0.55
    return 0.3


def tree_score(tree_info: dict[str, Any] | None, authority_kind: str) -> float:
    if not tree_info:
        return 0.50 if authority_kind in {"school_rule", "regulation"} else 0.35

    depth = tree_info.get("depth")
    try:
        depth_value = int(depth)
    except (TypeError, ValueError):
        depth_value = 3

    if authority_kind == "school_rule":
        return 1.0 if depth_value <= 1 else 0.90
    if authority_kind == "regulation":
        if depth_value <= 2:
            return 1.0
        if depth_value == 3:
            return 0.80
        return 0.65
    if authority_kind == "bylaw_guideline":
        if depth_value <= 2:
            return 0.85
        if depth_value == 3:
            return 0.70
        return 0.55
    return 0.40


def source_quality_score(metadata: dict[str, Any], content: str) -> float:
    length_score = min(1.0, math.log10(max(len(content), 10)) / 4.0)
    kind = normalize_text(metadata.get("source_kind"))
    ext = normalize_text(metadata.get("source_ext"))
    kind_score = 0.75
    if kind == "file":
        kind_score = 1.0
    elif kind == "html":
        kind_score = 0.85
    elif kind == "json":
        kind_score = 0.75
    if ext in {".hwp", ".hwpx", ".pdf", ".docx", ".doc"}:
        kind_score = max(kind_score, 0.95)

    has_title = bool(normalize_text(metadata.get("doc_title") or metadata.get("source_file")))
    has_url = bool(normalize_text(metadata.get("doc_url") or metadata.get("source_page_url")))
    metadata_score = 0.5 + (0.25 if has_title else 0.0) + (0.25 if has_url else 0.0)
    return min(1.0, 0.45 * length_score + 0.35 * kind_score + 0.20 * metadata_score)


def is_deprecated(title: str, content: str) -> bool:
    haystack = normalize_text(f"{title}\n{content[:1000]}")
    return any(keyword in haystack for keyword in DEPRECATED_KEYWORDS)


def calculate_rule_priority(
    *,
    title: str,
    content: str,
    metadata: dict[str, Any],
    tree_info: dict[str, Any] | None = None,
    today: date | None = None,
) -> tuple[float, dict[str, float | int | str | bool | None]]:
    authority_kind, band_base, band_span = authority_band(metadata, tree_info)
    date_label = "revision_date" if authority_kind == "bylaw_guideline" else "effective_date"
    priority_date = extract_latest_date(
        tree_info.get("effective") if tree_info and authority_kind in {"school_rule", "regulation"} else None,
        metadata.get("effective_at"),
        metadata.get("date"),
        metadata.get("doc_title"),
        metadata.get("source_file"),
        title,
        content[:2500] if authority_kind == "bylaw_guideline" else "",
    )
    authority = authority_score(metadata, tree_info)
    recency = recency_score(priority_date, today=today)
    tree = tree_score(tree_info, authority_kind)
    source_quality = source_quality_score(metadata, content)
    deprecated = is_deprecated(title, content)
    kind = document_kind(title, metadata)
    kind_adjustment = document_kind_adjustment(kind)

    top_school_rule = is_top_school_rule(title, metadata, tree_info)
    if top_school_rule:
        score = 1.0
    else:
        within_band = 0.50 * recency + 0.35 * tree + 0.15 * source_quality
        score = band_base + band_span * within_band + kind_adjustment
    score = max(0.0, min(1.0, score))
    return score, {
        "rule": "rule_authority_date_tree_quality_doc_kind_v3",
        "authority_kind": authority_kind,
        "authority_score": authority,
        "band_base": band_base,
        "band_span": band_span,
        "recency_score": recency,
        "tree_score": tree,
        "source_quality_score": source_quality,
        "document_kind": kind,
        "document_kind_adjustment": kind_adjustment,
        "top_school_rule": top_school_rule,
        "deprecated": deprecated,
        "date_label": date_label,
        "priority_date": priority_date.isoformat() if priority_date else None,
        "tree_node_id": tree_info.get("id") if tree_info else None,
        "tree_depth": tree_info.get("depth") if tree_info else None,
        "content_chars": len(content),
    }


def aggregate_source_records(records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    sources: dict[str, dict[str, Any]] = {}
    for record in records:
        metadata = record.get("metadata") or {}
        if not isinstance(metadata, dict):
            metadata = {}
        source_slug = str(record.get("source_slug") or metadata.get("source_slug") or "")
        if not source_slug:
            continue
        source = sources.setdefault(
            source_slug,
            {
                "id": source_slug,
                "title": metadata.get("doc_title") or metadata.get("source_file") or source_slug,
                "metadata": metadata,
                "content_parts": [],
            },
        )
        source["content_parts"].append(str(record.get("text") or record.get("content") or ""))
    return [
        {
            "id": source["id"],
            "title": source["title"],
            "metadata": source["metadata"],
            "content": "\n\n".join(source["content_parts"]),
        }
        for source in sources.values()
    ]
