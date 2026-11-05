"""Shared text cleanup helpers for RAG preprocessing."""

from __future__ import annotations

import re

CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
ZERO_WIDTH_CHARS = re.compile(r"[\u200b-\u200f\ufeff]")
BOX_DRAWING_CHARS = re.compile(r"[\u2500-\u257f]+")
DOT_LEADERS = re.compile(r"[\.\u00b7\u2024\u2027\u2219\u22c5\u30fb\u318d]{4,}")
CARET_MARKERS = re.compile(r"(?:\(?\^+\s*\d+[.)]?\)?\s*){2,}")
SINGLE_CARET_MARKER = re.compile(r"\(?\^+\s*\d+[.)]?\)?")
REPEATED_PUNCT = re.compile(r"([^\w\s\uac00-\ud7a3])\1{3,}")
QUESTION_RUN = re.compile(r"\?{2,}")
SPREADSHEET_FORMULA = re.compile(r"=[A-Z][A-Z0-9_]*\([^)\s]*\)[^\s]*")
FORMAT_ARTIFACT = re.compile(r"%[A-Za-z](?:[,;]+)?")
SPACED_HANGUL_RUN = re.compile(
    r"(?<![\uac00-\ud7a3])(?:[\uac00-\ud7a3]\s+){3,}[\uac00-\ud7a3](?![\uac00-\ud7a3])"
)
MULTISPACE = re.compile(r"[ \t]{2,}")


def compact_metadata(value: dict) -> dict:
    """Remove empty optional values, preserving false/zero and schema date/crawl keys."""
    def clean(item):
        if isinstance(item, dict):
            result = {}
            for key, child in item.items():
                child = clean(child)
                if child is None or (isinstance(child, str) and not child.strip()):
                    continue
                if isinstance(child, (dict, list)) and not child:
                    continue
                result[key] = child
            return result
        if isinstance(item, list):
            # Preserve list positions (for example table cell alignment).
            return [clean(child) for child in item]
        return item
    result = clean(value)
    for key in ("published_at", "crawl"):
        if key in value and key not in result:
            result[key] = value[key]
    return result


def has_searchable_body(document: dict) -> bool:
    """Metadata fallback is provenance, not extracted document content."""
    if document.get("used_metadata_fallback") is True or document.get("metadata_only") is True:
        return False
    chunks = document.get("chunks") or []
    return any(isinstance(chunk, dict) and str(chunk.get("text") or "").strip()
               and chunk.get("type") != "metadata_fallback" for chunk in chunks)


def _collapse_spaced_hangul(match: re.Match[str]) -> str:
    return match.group(0).replace(" ", "")


def normalize_whitespace(text: object) -> str:
    if text is None:
        return ""
    value = str(text)
    value = value.replace("\u00a0", " ")
    value = ZERO_WIDTH_CHARS.sub("", value)
    value = CONTROL_CHARS.sub(" ", value)
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    value = "\n".join(MULTISPACE.sub(" ", line).strip() for line in value.splitlines())
    return value.strip()


def is_noise_line(line: str) -> bool:
    compact = line.strip()
    if not compact:
        return True
    if re.fullmatch(r"[-=*_~\s]{3,}", compact):
        return True
    if re.fullmatch(r"(?:\(?\^+\s*\d+[.)]?\)?\s*)+", compact):
        return True
    if re.fullmatch(r"\d+\s*/\s*\d+", compact):
        return True
    return False


def clean_extracted_text(text: object) -> str:
    """Clean extracted document text while preserving legal/article structure."""

    value = normalize_whitespace(text)
    if not value:
        return ""

    value = BOX_DRAWING_CHARS.sub(" ", value)
    value = CARET_MARKERS.sub(" ", value)
    value = SINGLE_CARET_MARKER.sub(" ", value)
    value = SPREADSHEET_FORMULA.sub(" ", value)
    value = FORMAT_ARTIFACT.sub(" ", value)
    value = QUESTION_RUN.sub(" ", value)
    value = DOT_LEADERS.sub(" ", value)
    value = REPEATED_PUNCT.sub(r"\1\1", value)
    value = SPACED_HANGUL_RUN.sub(_collapse_spaced_hangul, value)

    cleaned_lines: list[str] = []
    previous = ""
    duplicate_run = 0
    for raw_line in value.splitlines():
        line = normalize_whitespace(raw_line)
        line = SPREADSHEET_FORMULA.sub(" ", line)
        line = FORMAT_ARTIFACT.sub(" ", line)
        line = QUESTION_RUN.sub(" ", line)
        line = DOT_LEADERS.sub(" ", line)
        line = REPEATED_PUNCT.sub(r"\1\1", line)
        line = MULTISPACE.sub(" ", line).strip()
        if is_noise_line(line):
            continue
        if line == previous:
            duplicate_run += 1
            if duplicate_run >= 1:
                continue
        else:
            duplicate_run = 0
        cleaned_lines.append(line)
        previous = line

    value = "\n".join(cleaned_lines)
    value = re.sub(r"\n{3,}", "\n\n", value)
    value = MULTISPACE.sub(" ", value)
    return value.strip()
