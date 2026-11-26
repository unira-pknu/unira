"""Course-list entities and user-only follow-up context, shared by both planners."""
from __future__ import annotations

import re

INSTRUCTOR = re.compile(r"(?<![가-힣])([가-힣]{2,4})(?:[ \t]+(?:전임|명예|퇴임|겸임|초빙|객원|석좌|부|조))?\s*교수(?:님)?(?=이|가|의|께|에게|한테|은|는|도|을|를|\s|[,.?!]|$)")
_NON_NAMES = {
    "담당", "어떤", "무슨", "해당", "전임", "명예", "퇴임", "겸임", "초빙", "객원", "석좌",
    "맡은", "신임", "취임한", "부임한", "역임한", "선임된", "임명된",
}
COURSES = re.compile(r"강의명|과목명|강좌명|담당\s*과목|(?:강의|수업|강좌|과목).{0,12}(?:목록|리스트|전체|모두)|(?:진행|가르치|담당|수업).{0,15}(?:강의|과목|강좌)|(?:어떤|무슨)\s*(?:강의|과목|수업)")
PROFILE_REFERENCE = re.compile(r"(?:공개\s*)?프로필|교수\s*(?:소개|진)\s*(?:페이지|안내)|상세\s*소개")
COURSE_PAIR = re.compile(r"(?<![A-Za-z0-9])([A-Za-z]?\d{5,6})\s*[-–]\s*(\d{1,3})(?![A-Za-z0-9])")
COURSE_CODE = re.compile(r"학수번호\s*[:：]?\s*([A-Za-z]?\d{5,6})(?![A-Za-z0-9])")
SECTION = re.compile(r"(?<!\d)(\d{1,3})\s*분반|분반\s*[:：]?\s*(\d{1,3})(?!\d)")
BARE_COURSE_CODE = re.compile(r"(?<![A-Za-z0-9])([A-Za-z]?\d{5,6})(?![A-Za-z0-9])")
LEVELS = ("일반대학원", "산업대학원", "교육대학원", "경영대학원", "글로벌정책대학원", "수산해양대학원", "특수대학원", "전문대학원", "대학원", "학부")


def instructor_name(text: str) -> str | None:
    return next((match[1] for match in INSTRUCTOR.finditer(text) if match[1] not in _NON_NAMES), None)


def instructor_names(text: str) -> set[str]:
    return {m[1] for m in INSTRUCTOR.finditer(text) if m[1] not in _NON_NAMES}


def course_level(text: str) -> str | None:
    choices = [(text.index(level), -len(level), level) for level in LEVELS if level in text]
    if not choices:
        return None
    first = min(choices)
    broad = re.search(r"(?:전체|모든)\s*과정|학부.{0,5}대학원.{0,5}(?:모두|전체)|과정\s*상관없", text)
    return None if broad and broad.start() <= first[0] else first[2]


def is_course_list(text: str) -> bool:
    return bool(len(instructor_names(text)) == 1 and COURSES.search(text) and not PROFILE_REFERENCE.search(text))


def has_course_identity(text: str) -> bool:
    return bool(COURSE_PAIR.search(text) or COURSE_CODE.search(text)
                or (SECTION.search(text) and BARE_COURSE_CODE.search(text)))


def is_single_offering_question(text: str) -> bool:
    pairs = COURSE_PAIR.findall(text)
    codes = {code.upper() for code, _ in pairs}
    codes.update(code.upper() for code in COURSE_CODE.findall(text))
    if SECTION.search(text):
        codes.update(code.upper() for code in BARE_COURSE_CODE.findall(text))
    return len(codes) == 1 and len(set(pairs)) <= 1 and not re.search(r"그리고|아울러|별도로|비교", text)


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
