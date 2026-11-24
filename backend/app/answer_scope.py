"""Conservative checks for scope errors observed in generated answers."""
from __future__ import annotations

import re
from datetime import date

from . import department_focus as departments
from .grounding import unsupported_numbers
from .query_plan import department_catalog, target_for

_CITATION = re.compile(r"\[(\d+)\]")
_PERIOD = re.compile(
    r"(20\d{2})\s*[년./-]\s*(\d{1,2})\s*[월./-]\s*(\d{1,2})\s*[일.]?\s*"
    r"(?:\([월화수목금토일]\))?\s*(?:부터|~|∼|～)\s*"
    r"(?:(20\d{2})\s*[년./-]\s*)?(\d{1,2})\s*[월./-]\s*(\d{1,2})\s*[일.]?"
)


def _publisher_names(row: dict) -> list[str]:
    metadata = row.get("metadata") or {}
    names = [metadata.get("department_name")]
    identity = metadata.get("department_id")
    if identity:
        for entry in department_catalog():
            if entry.get("dataset") == identity:
                names.extend([entry.get("name"), entry.get("department"), *(entry.get("majors") or [])])
    return [name.strip() for value in names if value for name in str(value).split(",") if name.strip()]


def _mentions_cohort(text: str, year: int) -> bool:
    # A posting/amendment date is not an admission cohort. Only explicit
    # cohort labels/ranges count; do not infer coverage from a newer document.
    pattern = re.compile(r"(?<!\d)(20\d{2}|\d{2})\s*(학번|학년도|년)?\s*"
                         r"(?:(?:~|∼|～|[-–])\s*(20\d{2}|\d{2})\s*(?:학번|학년도|년)?\s*)?"
                         r"(이후|이전|이상|이하|부터|까지)?\s*(입학[생자]?|신입[생학])?")
    for match in pattern.finditer(text):
        first, unit, last, direction, admitted = match.groups()
        if unit != "학번" and not admitted:
            continue
        def full(value):
            number = int(value)
            return 2000 + number if number < 100 else number
        start = full(first)
        if last and start <= year <= full(last):
            return True
        if not last and ((direction in {"이후", "이상", "부터"} and year >= start)
                         or (direction in {"이전", "이하", "까지"} and year <= start)
                         or (not direction and year == start)):
            return True
    return False


def scope_problems(answer: str, rows: list[dict], question: str, *, today: date) -> list[str]:
    """Flag narrow, reproducible mistakes; source compatibility alone is not proof.

    Publishing departments are used only to reject an unqualified generalization,
    never to declare a student's eligibility or replace missing body evidence.
    """
    problems = []
    target = target_for(question)
    graduation = "졸업" in question and bool(re.search(r"학점|요건|기준", question))
    unscoped_graduation = (graduation and not target.department
                           and not any(((r.get("metadata") or {}).get("validated_target") or {}).get("department") for r in rows))
    all_cited = [rows[int(n)-1] for n in _CITATION.findall(answer) if 1 <= int(n) <= len(rows)]
    if graduation and target.admission_year and re.search(r"\d+\s*학점", answer):
        texts = [str(row.get("content") or "") for row in all_cited]
        cohort_texts = [text for text in texts if _mentions_cohort(text, target.admission_year)]
        credits = re.findall(r"\d+\s*학점", answer)
        # A common guide naming "22학번 이후" and a separate 2025 curriculum
        # table containing 132 must not jointly become a 2024-cohort total.
        if not cohort_texts or any(unsupported_numbers(credit, cohort_texts) for credit in credits):
            problems.append(f"질문은 {target.admission_year}학년도 입학생 기준이지만 인용 본문에 해당 입학연도의 적용 근거가 없습니다. "
                            "그 학생의 졸업학점을 확정하지 말고, 확인되는 공통 정보와 해당 학번 기준 미확인을 구분하세요")
    # A dotted date's final day ("27. 가능합니다") is not a sentence end.
    for sentence in re.split(r"\n+|(?<=[^\d][.!?])\s+(?=[^\d\s])", answer):
        cited = [rows[int(n)-1] for n in _CITATION.findall(sentence) if 1 <= int(n) <= len(rows)] or all_cited
        if unscoped_graduation and re.search(r"\d+\s*학점", sentence) and cited:
            publishers = [_publisher_names(row) for row in cited]
            # Unknown/common sources must not be guessed to be department-only.
            if all(publishers) and not any(departments.normalize(name) in departments.normalize(sentence)
                                           for names in publishers for name in names):
                problems.append("학과를 지정하지 않은 질문에 학과별 졸업학점을 전체 학생 기준처럼 제시했습니다. "
                                "학과·입학연도에 따라 다름을 먼저 밝히고, 학점을 쓰려면 해당 문장에 자료의 학과명을 명시하세요")
        if not re.search(r"신청|접수|모집", sentence) or not re.search(r"가능합니다|가능하며|가능하다|신청하세요|신청해야|접수합니다|진행됩니다|입니다", sentence):
            continue
        if re.search(r"종료|과거|지난\s*(?:일정|기간|학기)|당시|였습|이었습|진행됐|마감됐|마감되었|불가능|신청할\s*수\s*없", sentence):
            continue
        for match in _PERIOD.finditer(sentence):
            sy, sm, sd, ey, em, ed = (int(value) if value else None for value in match.groups())
            try:
                start = date(sy, sm, sd)
                end = date(ey or sy + (em < sm), em, ed)
            except ValueError:
                continue
            if start <= end < today:
                problems.append(f"{match.group(0)}는 오늘({today.isoformat()}) 기준 종료된 과거 신청 기간입니다. "
                                "현재 신청 가능하다고 안내하지 말고, 자료의 대상 시점과 종료 사실 및 현재 일정 미확인을 명시하세요")
    return list(dict.fromkeys(problems))


def scope_fallback(question: str) -> str:
    """Keep the unresolved condition visible after one unsuccessful repair."""
    target = target_for(question)
    if "졸업" in question and target.admission_year:
        return (f"제공된 자료만으로는 {target.admission_year}학년도 입학생의 졸업학점을 확정할 수 없습니다. "
                "소속 전공의 해당 입학연도 졸업요건 안내에서 확인이 필요합니다.")
    if "졸업" in question and not target.department:
        return "졸업학점은 학과·전공과 입학연도에 따라 달라집니다. 소속 학과·전공과 입학연도를 알려주세요."
    return "자료에서 현재 신청 가능한 기간을 확인하지 못했습니다. 최신 공지의 대상 학기와 신청 기간을 확인해야 합니다."
