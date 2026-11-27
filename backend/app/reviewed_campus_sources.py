"""Reviewed passages from the existing campus crawl, selected by explicit scope."""

from __future__ import annotations

from functools import lru_cache
import json
from pathlib import Path
import re


_YEAR = re.compile(r"(?<!\d)((?:19|20)\d{2})(?=(?!\d)|\d{4}(?!\d))")
_SCHOOL = re.compile(r"(?<![가-힣])([가-힣]{2,}(?:대학교|대))(?=\s|[에의은는이가을를와과(),.?]|$)")
_HOME_SCHOOLS = {"부경대", "부경대학교", "국립부경대", "국립부경대학교"}


def _compact(text: str) -> str:
    return re.sub(r"[^0-9a-z가-힣]", "", text.casefold())


@lru_cache(maxsize=1)
def _records() -> tuple[dict, ...]:
    return tuple(json.loads(Path(__file__).with_suffix(".json").read_text(encoding="utf-8")))


def matching_sources(question: str) -> list[dict]:
    """Require the named topic, requested fact and documented year together."""
    text = _compact(question)
    if not text:
        return []
    years = {int(value) for value in _YEAR.findall(question)}
    schools = set(_SCHOOL.findall(question)) - _HOME_SCHOOLS
    matches = []
    for record in _records():
        year = record.get("year")
        if year is not None and year not in years:
            continue
        if years - {year, *record.get("also_years", ())} or schools - set(record.get("schools", ())):
            continue
        if any(term in text for term in record.get("exclude_terms", ())):
            continue
        if all(any(term in text for term in group) for group in record["terms"]):
            matches.append(record)
    return matches


def source_rows(question: str) -> list[dict]:
    """Expose verified answers as cited evidence through the ordinary RAG path."""
    rows = []
    for record in matching_sources(question):
        source = record["source"]
        attachment = source.get("attachment") or {}
        identifier = "reviewed:" + record["id"]
        rows.append({
            "id": identifier,
            "source_id": identifier,
            "title": source["title"],
            "url": source["url"],
            "content": record["answer"],
            "metadata": {
                "doc_title": source["title"],
                "source_dataset": source["dataset"],
                "ingestion_dataset": source["dataset"],
                "rpc_name": "reviewed_campus_sources",
                "published_at": source.get("published_at"),
                "reviewed_campus_source": True,
                "attachment_name": attachment.get("name"),
                "attachment_url": attachment.get("url"),
                "archive_member_original_name": attachment.get("member"),
            },
        })
    return rows
