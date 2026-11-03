"""Numbers an answer states that its sources don't back.

The answer model fills gaps with plausible figures: on 2026-10-27 "졸업하려면
총 몇 학점?" was answered "총 130학점 이상" in 2-3 of 3 runs although no
retrieved chunk contained 130 (the per-department 132/140/160 table was not
retrieved). It also borrows a real figure from the wrong source: "졸업학점
50학점" came from a 교직과목 table. The prompt already forbids both
(system.txt rules 13, 20); generation.generate_answer() uses this check to
send such an answer back once instead of trusting the prompt alone.
"""

from __future__ import annotations

import re
from typing import Iterable, List, Sequence

# A figure with the unit students act on. Bare numbers ("1.", "[3]") are list
# markers and citations; dates are left out because answers rewrite
# "2026. 10. 27." as "2026년 10월 27일".
_CLAIM_RE = re.compile(
    r"(?<![\d.])(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s*"
    r"(학점|만\s*원|천\s*원|원|명|점|%|퍼센트|배수|배|학기|시간|개월)"
)
_DECIMAL_RE = re.compile(r"(?<![\d.])\d\.\d{1,2}(?![\d.])")  # GPA-style 3.50
_CITE_RE = re.compile(r"\[(\d{1,2})\]")


def _numbers(value: str) -> set:
    """"3.50" also matches "3.5"; "2,000,000" also matches "2000000"."""
    plain = value.replace(",", "")
    forms = {value, plain}
    if "." in plain:
        forms.add(plain.rstrip("0").rstrip("."))
    return forms


def _found(number: str, text: str) -> bool:
    flat = text.replace(",", "")
    return any(
        re.search(rf"(?<![\d.]){re.escape(form)}(?![\d])", source)
        for form in _numbers(number)
        for source in (text, flat)
    )


def _claims(text: str) -> List[tuple]:
    """(number, claim-with-unit) pairs; citation markers are not figures."""
    clean = _CITE_RE.sub(" ", text)
    claims = [(m.group(1), m.group(0).strip()) for m in _CLAIM_RE.finditer(clean)]
    claims += [(m.group(0), m.group(0)) for m in _DECIMAL_RE.finditer(clean)]
    return claims


def unsupported_numbers(answer: str, texts: Iterable[str]) -> List[str]:
    """Figures in `answer` (with their unit) that appear in none of `texts`."""
    haystack = " ".join(texts)
    missing: List[str] = []
    for number, claim in _claims(answer):
        if not _found(number, haystack) and claim not in missing:
            missing.append(claim)
    return missing


def miscited_numbers(answer: str, question: str, sources: Sequence[str]) -> List[str]:
    """Figures a line attributes to sources [n] that don't contain them although
    another source does (a real number borrowed from the wrong document).

    Only lines that cite something are checked; a figure that is in no source
    at all is unsupported_numbers()'s case, not this one."""
    flagged: List[str] = []
    for line in answer.split("\n"):
        cited = {int(n) for n in _CITE_RE.findall(line) if 1 <= int(n) <= len(sources)}
        if not cited:
            continue
        pool = question + " " + " ".join(sources[n - 1] for n in sorted(cited))
        for number, claim in _claims(line):
            if _found(number, pool):
                continue
            if any(_found(number, text) for text in sources) and claim not in flagged:
                flagged.append(claim)
    return flagged
