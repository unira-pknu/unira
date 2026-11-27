"""Keep answers to one department when a question names it.

School-wide notices attach one table covering every department (전과 세부
선발기준, 졸업자격인증제 실시 현황, 부·복수전공 선발기준). A question about
one department ("컴공으로 전과하려면?") then retrieves whichever slice of that
table scores best, often another department's rows, and the answer quotes
them: on 2026-10-26 "26학번 컴공 졸업인증" was answered with another
department's TOEIC 700 and "컴공 전과" with another department's 4 advisor
meetings. retrieval._focus_department() drops table chunks that list other
departments but not the asked one, and pulls in the asked department's slice
of the same table.
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Iterable, Optional, Tuple

logger = logging.getLogger(__name__)

# Department and major names come from the crawler registry, so a new or
# renamed department only needs a registry change. The repo checkout (school
# server, local runs) reads scripts/crawlers/departments/registry.json; the
# HF Space gets only backend/, so its deploy workflow copies the registry to
# app/department_registry.json.
REGISTRY_FILE = "department_registry.json"
_NAME_RE = re.compile(r"(학부|학과|전공)$")


def _registry_path() -> Optional[Path]:
    here = Path(__file__).resolve().parent
    candidates = [Path(os.environ["DEPARTMENT_REGISTRY_PATH"])] if os.environ.get("DEPARTMENT_REGISTRY_PATH") else []
    candidates += [here / REGISTRY_FILE, here.parents[1] / "scripts" / "crawlers" / "departments" / "registry.json"]
    return next((path for path in candidates if path.is_file()), None)


def load_department_names(path: Optional[Path]) -> Tuple[str, ...]:
    """Department and major names in the registry (college names left out).

    Entries without a 학부/학과/전공 ending are colleges, and names with spaces
    ("공과대학 자유전공학부") are display labels, not what tables print. Majors
    may be listed in one string ("컴퓨터공학전공, 인공지능전공").
    """
    if path is None:
        logger.warning("department registry not found; department focus only knows aliases")
        return ()
    registry = json.loads(path.read_text(encoding="utf-8"))
    names = set()
    for entry in registry.get("departments", []):
        values = [entry.get("name"), entry.get("department"), *(entry.get("majors") or [])]
        for value in values:
            for name in str(value or "").split(","):
                name = name.strip()
                if _NAME_RE.search(name) and not re.search(r"\s", name):
                    names.add(name)
    return tuple(sorted(names))


DEPARTMENT_NAMES: Tuple[str, ...] = load_department_names(_registry_path())

# User names and older labels absent from today's department registry.
ALIASES = {
    "컴공": ("컴퓨터·인공지능공학부", "컴퓨터공학전공"),
    "컴과": ("컴퓨터·인공지능공학부", "컴퓨터공학전공"),
    # Full-form user names need the same dataset scope as the abbreviation.
    # This is routing identity, not proof of equal curricula across cohorts.
    "컴퓨터공학과": ("컴퓨터·인공지능공학부", "컴퓨터공학전공"),
    "컴퓨터공학부": ("컴퓨터·인공지능공학부", "컴퓨터공학전공"),
    "기계공학전공": ("기계공학전공", "기계공학부"),
    "스마트모빌리티": ("스마트모빌리티공학과",),
}
# A chunk naming at least this many other departments is a slice of a
# per-department table.
TABLE_MIN_DEPARTMENTS = 3

_STRIP_RE = re.compile(r"[\s·ㆍ・]+")


def normalize(text: Optional[str]) -> str:
    """Drop spaces and middle dots: tables write "컴퓨터·인공 지능공학부"."""
    return _STRIP_RE.sub("", text or "")


def _stem(name: str) -> str:
    """"기계공학부" -> "기계공학", so "기계공학전공" rows also match."""
    for suffix in ("전공", "부", "과"):
        if name.endswith(suffix) and len(name) - len(suffix) >= 3:
            return name[: -len(suffix)]
    return name


_NORMALIZED = tuple(sorted({normalize(name) for name in DEPARTMENT_NAMES}, key=len, reverse=True))


def focus_variants(query_text: Optional[str]) -> Optional[Tuple[str, ...]]:
    """Normalized names of the department the question is about, or None."""
    query = normalize(query_text)
    if not query:
        return None
    names = [name for alias, targets in ALIASES.items() if alias in query for name in targets]
    names += [name for name in _NORMALIZED if name in query]
    if not names:
        return None
    variants = {normalize(name) for name in names} | {_stem(normalize(name)) for name in names}
    return tuple(sorted(variants, key=len, reverse=True))


def mentions(text: Optional[str], variants: Iterable[str]) -> bool:
    body = normalize(text)
    return any(variant in body for variant in variants)


# Per-department tables number their rows: "117 | 정보융합대학 | 컴퓨터·인공지능공학부 | ...".
_ROW_START_RE = re.compile(r"(?:(?<=\s)|^)(?=\d{1,3} \| )")
_BODY_MARKER = "\n본문:"


def keep_own_rows(text: Optional[str], variants: Tuple[str, ...]) -> Optional[str]:
    """The chunk with only the asked department's table rows, or None to keep it as is.

    Only for slices of a per-department table that mix the asked department
    with others: the answer model otherwise quotes a neighbouring row
    ("26학번 컴공 졸업인증" got another department's TOEIC 700 from the row
    next to 컴퓨터공학전공's, 2026-10-26).
    """
    content = text or ""
    marker = content.find(_BODY_MARKER)
    head, body = (content[: marker + len(_BODY_MARKER)], content[marker + len(_BODY_MARKER):]) if marker >= 0 else ("", content)
    if sum(1 for name in _NORMALIZED if name in normalize(body)) < TABLE_MIN_DEPARTMENTS:
        return None
    parts = _ROW_START_RE.split(body)
    rows = [part for part in parts[1:] if part.strip()]
    if len(rows) < TABLE_MIN_DEPARTMENTS:
        return None
    own = [row for row in rows if any(variant in normalize(row) for variant in variants)]
    if not own or len(own) == len(rows):
        return None
    # Text before the first numbered row is the tail of a row cut off in the
    # previous chunk; keep it only when it is about the asked department.
    lead = parts[0] if any(variant in normalize(parts[0]) for variant in variants) else ""
    return head + lead + " ".join(row.strip() for row in own) + " (표의 다른 학과 행은 생략)"


def is_other_departments_table(text: Optional[str], variants: Tuple[str, ...]) -> bool:
    """A per-department table slice that lists other departments, not this one."""
    body = normalize(text)
    if any(variant in body for variant in variants):
        return False
    others = sum(1 for name in _NORMALIZED if name in body)
    return others >= TABLE_MIN_DEPARTMENTS
