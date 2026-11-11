"""Bounded, question-focused evidence, with intact table rows and provenance."""

from __future__ import annotations

from dataclasses import dataclass
import logging
import re
from typing import Callable

from .excerpts import _score, _search_terms

logger = logging.getLogger(__name__)

# A target is allowed to grow for a complete sentence, exception or table row.
# Still bound every source even when a document contains a very large table.
PROTECTED_BLOCK_MAX_CHARS = 4000
_ADDRESS_QUESTION = re.compile(r"주소|홈페이지|사이트|URL|링크|접속", re.I)
CHUNK_TABLES = {
    "match_rag_documents": "rag_chunks",
    "match_rule_documents": "rule_chunks",
    "match_pknu_notice_documents": "pknu_notice_chunks",
    "match_pknu_main_documents": "pknu_main_chunks",
    "match_pknu_syllabus_documents": "pknu_syllabus_chunks",
}
_QUALIFIER = re.compile(r"다만|단[,，\s]|예외|제외|제출|구비|첨부|유의|주의|불가|미만|이상|이하|초과")
_SEPARATOR = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*$")
_HEADING = re.compile(r"^(?:#{1,6}\s|제\s*\d+\s*[조장절]|[①-⑳]|\d+[.)]\s)|[:：]$")
_GAP = "\n[… 중간 생략 …]\n"
_COLUMN_LABELS = frozenset({
    "구분", "유형", "대상", "항목", "학과", "학년", "학기", "제출서류", "구비서류",
    "신청기간", "인정기간", "기간", "지원금액", "금액", "기준", "지원기준", "비고",
    "학점", "연락처", "성명", "담당", "교과목명", "담당교수",
})


@dataclass(frozen=True)
class _Unit:
    text: str
    table: bool = False


def _prose_units(line: str, target: int) -> list[_Unit]:
    if len(line) <= target:
        return [_Unit(line)]
    # Dates/decimals followed by digits are not sentence boundaries.
    sentences = re.split(r"(?<=[.!?。])\s+(?=[^\d\s])", line)
    result = []
    for sentence in sentences:
        if len(sentence) <= PROTECTED_BLOCK_MAX_CHARS:
            result.append(_Unit(sentence))
            continue
        # OCR may produce one unbroken paragraph. Overlapping windows keep
        # a late query hit selectable; omission markers describe the boundary.
        step = max(1, target // 2)
        for start in range(0, len(sentence), step):
            result.append(_Unit(sentence[start:start + target]))
            if start + target >= len(sentence):
                break
    return result


def _units(content: str, target: int) -> list[_Unit]:
    # Chunk boundaries may add blank lines between a table header and its
    # continuation. Empty lines are not cells; keep non-empty row text intact.
    lines = [line for line in content.splitlines() if line.strip()]
    result = []
    index = 0
    while index < len(lines):
        line = lines[index].strip(" \r")
        if not line.strip():
            index += 1
            continue
        delimiter = "|" if "|" in line else "\t"
        cells = [cell.strip(" *").replace(" ", "") for cell in line.split(delimiter) if cell.strip()]
        separator = index + 1 < len(lines) and "|" in line and _SEPARATOR.fullmatch(lines[index + 1])
        labelled = delimiter in line and sum(cell in _COLUMN_LABELS for cell in cells) >= 2
        if separator or labelled:
            # Repeat title, column names and separator for EACH selected row.
            title = next((lines[i].strip() for i in range(index - 1, max(-1, index - 6), -1)
                          if lines[i].strip()), "")
            if len(title) > 200 or "|" in title or "\t" in title:
                title = ""
            header = "\n".join(part for part in (title, line, lines[index + 1].strip() if separator else "") if part)
            index += 2 if separator else 1
            found = False
            while index < len(lines) and delimiter in lines[index] and lines[index].strip():
                # A subsequent table has its own title/columns; do not present
                # its rows under this table's header.
                next_cells = [c.strip(" *").replace(" ", "") for c in lines[index].split(delimiter) if c.strip()]
                if (_SEPARATOR.fullmatch(lines[index])
                        or sum(c in _COLUMN_LABELS for c in next_cells) >= 2
                        or (index + 1 < len(lines) and _SEPARATOR.fullmatch(lines[index + 1]))):
                    break
                result.append(_Unit(header + "\n" + lines[index].strip(" \r"), table=True))
                found = True
                index += 1
            if not found:
                result.append(_Unit(header, table=True))
            continue
        # Unrecognised table layouts stay atomic too: never slice a cell into
        # a convincing but incomplete number or discard the end of its row.
        if "\t" in line or "|" in line:
            result.append(_Unit(line, table=True))
        else:
            result.extend(_prose_units(line, target))
        index += 1
    return result


def select_evidence(content: str, *, question: str, target_chars: int, metadata: dict | None = None) -> str:
    """Select original passages; never summarise or invent missing table cells.

    target_chars is a soft target. Protected units can grow to 4000 chars
    (or an explicitly larger target); oversized table rows are omitted whole.
    """
    if target_chars <= 0 or not content:
        return ""
    hard_limit = max(target_chars, PROTECTED_BLOCK_MAX_CHARS)
    table = (metadata or {}).get("structured_table") or {}
    if table.get("kind") == "spreadsheet":
        # Ingestion already repeats sheet/row identity and column labels on
        # fragments. Keep this binding; never rebuild absent values from metadata.
        if len(content) <= hard_limit:
            return content
        return "(표 행이 근거 길이 한도를 초과하여 생략되었습니다. 원문 확인이 필요합니다.)"
    if len(content) <= target_chars:
        return content
    units = _units(content, target_chars)
    if not units:
        return ""
    terms = _search_terms(question)
    terms.update(term for term in ("신청", "제출", "서류", "예외", "기간", "조건", "자격", "등록", "학점", "장학")
                 if term in question)
    if _ADDRESS_QUESTION.search(question):
        # 2026-11-11: "중앙도서관 홈페이지 주소" kept the staff list ("서버·홈페이지
        # 유지보수") and cut the sentence naming http://libweb.pknu.ac.kr.
        terms.update(("http", "://", ".ac.kr"))
    scores = [_score(unit.text, terms) for unit in units]
    ranked = sorted(range(len(units)), key=lambda i: (-scores[i], i))
    best = ranked[0]
    chosen: set[int] = set()

    def rendered(indices: set[int]) -> str:
        parts = []
        previous = None
        for i in sorted(indices):
            if previous is not None:
                parts.append("\n" if i == previous + 1 else _GAP)
            parts.append(units[i].text)
            previous = i
        body = "".join(parts)
        return ("[… 앞부분 생략 …]\n" if indices and min(indices) > 0 else "") + body + (
            "\n[… 뒷부분 생략 …]" if indices and max(indices) < len(units) - 1 else ""
        )

    def add(i: int, budget: int) -> bool:
        if 0 <= i < len(units) and len(rendered(chosen | {i})) <= budget:
            chosen.add(i)
            return True
        return False

    if not add(best, hard_limit):
        # In particular, do not turn a truncated row into a factual statement.
        return "(관련 구간이 근거 길이 한도를 초과하여 생략되었습니다. 원문 확인이 필요합니다.)"
    if scores[best] > 0:
        # Preserve a nearby heading and immediate explanatory context. Qualifying
        # clauses/list items may exceed the soft target, never the hard bound.
        if best and _HEADING.search(units[best - 1].text):
            add(best - 1, hard_limit)
        for i in range(max(0, best - 1), min(len(units), best + 7)):
            if i != best and not units[i].table and _QUALIFIER.search(units[i].text):
                add(i, hard_limit)
        for i in (best - 1, best + 1, best + 2):
            add(i, target_chars)
    for i in ranked:
        if scores[best] > 0 and not scores[i]:
            continue
        add(i, target_chars)
    return rendered(chosen)


def _position(row: dict) -> int | None:
    value = row.get("chunk_index")
    if type(value) is int and value >= 0:
        return value
    return None


def _same_table_row(parent: dict, neighbor: dict) -> bool:
    left = (parent.get("metadata") or {}).get("structured_table") or {}
    right = (neighbor.get("metadata") or {}).get("structured_table") or {}
    if left.get("kind") != "spreadsheet" and right.get("kind") != "spreadsheet":
        return True
    # Different spreadsheet rows may be different courses, lecturers or terms.
    a, b = left.get("rows") or [], right.get("rows") or []
    return bool(left.get("kind") == right.get("kind") == "spreadsheet"
                and bool(left.get("sheet")) and left.get("sheet") == right.get("sheet")
                and len(a) == len(b) == 1 and a[0].get("source_row") is not None
                and a[0]["source_row"] == b[0].get("source_row"))


def supplement_neighbors(client, rows: list[dict], *, max_sources: int = 4,
                         scope_check: Callable[[dict, dict], bool] | None = None) -> list[dict]:
    """Read +/-1 positions from the SAME source and RPC table, after ranking.

    Neighbors enrich a citation, not its retrieval score. Keep each component
    verbatim and identifiable for evaluation. Failure retains the original hit.
    """
    result = list(rows)
    cache: dict[tuple[str, str, int], dict] = {}
    lookups = 0
    for offset, parent in enumerate(rows):
        if parent.get("_syllabus") or parent.get("_context_chunks"):
            continue  # Exact-offering syllabus already has its own evidence window.
        rpc = str((parent.get("metadata") or {}).get("rpc_name") or parent.get("_rpc_name") or "")
        table = CHUNK_TABLES.get(rpc)
        source = parent.get("source_id")
        position = _position(parent)
        if not table or not source or position is None:
            continue  # URL/slug/chunk_id are not a safe document+position identity.
        wanted = [i for i in (position - 1, position + 1) if i >= 0]
        missing = [i for i in wanted if (table, str(source), i) not in cache]
        if missing:
            if lookups >= max_sources:
                continue
            lookups += 1
            try:
                found = client.table(table).select(
                    "id,source_id,chunk_id,chunk_index,content,metadata"
                ).eq("source_id", source).in_("chunk_index", missing).limit(len(missing)).execute().data or []
            except Exception:
                logger.warning("evidence: adjacent chunk lookup failed table=%s", table, exc_info=True)
                continue
            for chunk in found:
                idx = _position(chunk)
                if str(chunk.get("source_id")) == str(source) and idx in wanted:
                    cache[(table, str(source), idx)] = chunk
        parts = [parent]
        for idx in wanted:
            chunk = cache.get((table, str(source), idx))
            if not chunk or not str(chunk.get("content") or "").strip():
                continue
            candidate = {**parent, **chunk, "metadata": {
                **(parent.get("metadata") or {}), **(chunk.get("metadata") or {}),
            }}
            # Do not inherit a parent's table identity onto an unlabelled neighbor.
            if "structured_table" not in (chunk.get("metadata") or {}):
                candidate["metadata"].pop("structured_table", None)
            if not _same_table_row(parent, candidate) or (scope_check and not scope_check(parent, candidate)):
                continue
            parts.append(candidate)
        if len(parts) == 1:
            continue
        parts.sort(key=lambda r: r["chunk_index"])
        components = [{"id": r.get("id"), "source_id": r["source_id"], "chunk_id": r.get("chunk_id"),
                       "chunk_index": r["chunk_index"], "content": str(r.get("content") or ""),
                       "metadata": dict(r.get("metadata") or {})} for r in parts]
        # A single newline lets a table header at the end of the previous
        # chunk remain connected to rows at the start of the next one.
        result[offset] = {**parent, "content": "\n".join(c["content"] for c in components),
                          "_context_chunks": components}
    return result
