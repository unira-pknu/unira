"""Identify faculty questions and select complete entries from faculty rosters."""

from __future__ import annotations

import re
from typing import Iterable
from urllib.parse import parse_qsl, urlsplit

from .course_identity import instructor_names
from .excerpts import _score, _search_terms

ROSTER = re.compile(r"교수\s*(?:진|소개|명단|목록|님들|이름|성함)")
PERSON_REFERENCE = re.compile(r"(?<![가-힣])(?:그\s*분|(?:그|해당)\s*교수(?:님)?)(?=\s|[의은는이가을를께]|$)")
PROFILE_FIELDS = re.compile(r"프로필|학력|학위|박사|석사|경력|이력|논문|저서|특허|연구실|실험실|호실|학회|출신\s*대학|(?:방문|선임)\s*연구원")
PERSON_FIELDS = re.compile(r"이메일|메일\s*주소|전화\s*번호|연락처|사무실|연구\s*(?:분야|주제)|직함|보직")
FACULTY_ROLE = r"(?:(?:(?:전임|명예|퇴임|겸임|초빙|객원|석좌|부|조)\s*)?교수|학과장|전공\s*주임|강사)"
_BODY = re.compile(r"(?m)^본문:[ \t]*")
_RESEARCH_QUESTION = re.compile(r"연구\s*(?:분야|주제)")
_RESEARCH_FIELD = re.compile(r"연구\s*분야\s*:\s*\S")
_PROFILE_SOURCE_LIMIT = 8
_PROFILE_CHUNK_LIMIT = 40


def is_roster_source(metadata: dict) -> bool:
    return any(ROSTER.search(value) or value == "교수" for key in ("category", "subcategory", "doc_title", "title")
               if (value := str(metadata.get(key) or "")))


def faculty_card(question: str, metadata: dict, parts: Iterable[str], *, max_chars: int) -> str | None:
    if not is_roster_source(metadata):
        return None
    names = instructor_names(question)
    if len(names) != 1:
        return None
    name = re.escape(next(iter(names)))
    start = re.compile(rf"(?<![가-힣]){name}\s*{FACULTY_ROLE}[^:\n]{{0,40}}?(?:이메일|전화번호|연구\s*분야)\s*:")
    needs_research_field = bool(_RESEARCH_QUESTION.search(question))
    incomplete = False
    segments: list[str] = []
    for part in parts:
        marker = _BODY.search(part)
        body = (part[marker.end():] if marker else part).strip()
        if not body:
            continue
        if segments:
            previous = segments[-1]
            overlap = next((n for n in range(min(256, len(previous), len(body)), 23, -1)
                            if previous[-n:] == body[:n]), 0)
            if overlap:
                segments[-1] = previous + body[overlap:]
                continue
        segments.append(body)
    for segment in segments:
        # The final, unterminated fragment might be a cut-off address or field.
        for entry in segment.split("상세보기")[:-1]:
            match = start.search(entry)
            if not match:
                continue
            card = entry[match.start():].strip()
            # Some indexed chunks repeat their overlap. A truncated copy of
            # the card can then end at the preceding person's detail link.
            if needs_research_field and not _RESEARCH_FIELD.search(card):
                incomplete = True
                continue
            if len(card) <= max_chars:
                return card
    if incomplete:
        return "(이 발췌에서는 해당 교수의 연구분야가 포함된 완전한 항목을 확인할 수 없습니다.)"
    return None


def profile_chunks(client, *, department: str, name: str, question: str, top_k: int) -> list[dict]:
    """Read an active faculty source by the exact name stored in its subcategory."""
    sources = (client.table("rag_sources")
               .select("id,source_slug,source_type,title,url,parent_url,metadata")
               .eq("status", "active")
               .eq("category", "교수진")
               .eq("subcategory", name)
               .contains("metadata", {"ingestion_dataset": department})
               .order("processed_at", desc=True)
               .order("id")
               .limit(_PROFILE_SOURCE_LIMIT)
               .execute().data or [])
    # Several professors share one CMS path, so preserve person-specific query parameters.
    if len(sources) >= _PROFILE_SOURCE_LIMIT:
        return []
    title = re.compile(rf"^{re.escape(name)}\s+{FACULTY_ROLE}(?=\s|[/（(]|$)")
    profiles = {}
    for source in sources:
        url = urlsplit(source.get("url") or "")
        host = url.hostname or ""
        if (url.scheme in {"http", "https"} and (host == "pknu.ac.kr" or host.endswith(".pknu.ac.kr"))
                and title.match(str(source.get("title") or ""))):
            identity = (host, url.path.rstrip("/"), tuple(sorted(parse_qsl(url.query, keep_blank_values=True))))
            profiles.setdefault(identity, source)
    if len(profiles) != 1:
        return []
    source = next(iter(profiles.values()))
    chunks = (client.table("rag_chunks")
              .select("id,source_id,chunk_id,chunk_index,content,metadata")
              .eq("source_id", source["id"])
              .order("chunk_index")
              .limit(_PROFILE_CHUNK_LIMIT)
              .execute().data or [])
    terms = _search_terms(question)
    rows = [{**source, **chunk, "metadata": {
                **(source.get("metadata") or {}), **(chunk.get("metadata") or {}),
                "rpc_name": "match_rag_documents",
            }} for chunk in chunks if str(chunk.get("content") or "").strip()]
    return sorted(rows, key=lambda row: -_score(row["content"], terms))[:min(top_k, 6)]
