"""Course-list entities and user-only follow-up context, shared by both planners."""
from __future__ import annotations

import re

INSTRUCTOR = re.compile(r"(?<![가-힣])([가-힣]{2,4})\s*교수(?:님)?(?=이|가|의|께서|은|는|도|\s|[,.?!]|$)")
COURSES = re.compile(r"강의명|과목명|강좌명|담당\s*과목|(?:강의|수업|강좌|과목).{0,12}(?:목록|리스트|전체|모두)|(?:진행|가르치|담당|수업).{0,15}(?:강의|과목|강좌)|(?:어떤|무슨)\s*(?:강의|과목|수업)")
LEVELS = ("일반대학원", "산업대학원", "교육대학원", "경영대학원", "글로벌정책대학원", "수산해양대학원", "특수대학원", "전문대학원", "대학원", "학부")


def instructor_name(text: str) -> str | None:
    match = INSTRUCTOR.search(text)
    return match[1] if match and match[1] not in {"담당", "어떤", "무슨", "해당"} else None


def instructor_names(text: str) -> set[str]:
    return {m[1] for m in INSTRUCTOR.finditer(text) if m[1] not in {"담당", "어떤", "무슨", "해당"}}


def course_level(text: str) -> str | None:
    choices = [(text.index(level), -len(level), level) for level in LEVELS if level in text]
    if not choices:
        return None
    first = min(choices)
    broad = re.search(r"(?:전체|모든)\s*과정|학부.{0,5}대학원.{0,5}(?:모두|전체)|과정\s*상관없", text)
    return None if broad and broad.start() <= first[0] else first[2]


def is_course_list(text: str) -> bool:
    return bool(len(instructor_names(text)) == 1 and COURSES.search(text))


def _followup(text: str) -> bool:
    if re.search(r"강의명|과목명|강좌명|(?:전체|모든|그|해당)\s*(?:강의|수업|과목|강좌)|(?:강의|수업|과목|강좌)\s*(?:목록|이름)", text):
        return True
    # An answer to the academic-term/level clarification, not a new topic.
    leftover = re.sub(r"20\d{2}\s*[-/]\s*[12]|20\d{2}|[12]\s*학기|\d+\s*분반|학년도|이번\s*학기|지난\s*학기|다음\s*학기|학부|[가-힣]*대학원|전체\s*학기|모든\s*학기|전체\s*과정|모든\s*과정|만|이요|요|입니다|알려\s*줘|알려주세요|으로|로|년|그럼|그러면|[-/.,?!\s]", "", text)
    return bool(text.strip()) and not leftover


def resolve_course_question(question: str, history: list[dict] | None = None) -> str:
    """Carry only explicit user course conditions; ignore previous assistant answers."""
    if instructor_name(question) or not _followup(question):
        return question
    fragments = [question]
    for turn in reversed(history or []):
        if turn.get("role") != "user":
            continue
        text = turn.get("content", "")
        if is_course_list(text):
            return " / ".join([*fragments, text])
        if _followup(text):
            fragments.append(text)
        else:
            break
    return question
