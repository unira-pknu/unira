"""Ground a search plan in the user's words before running any retrieval."""
from __future__ import annotations

import re
from datetime import datetime, timezone, timedelta
from functools import lru_cache
import json
from typing import Literal

from pydantic import BaseModel, Field

from . import department_focus as departments
from .freshness import current_term, query_term
from .course_identity import instructor_name, instructor_names, course_level, is_course_list, resolve_course_question

KST = timezone(timedelta(hours=9))
TOPICS = re.compile(r"휴학|복학|전과|복수전공|부전공|졸업|수강|공결|출석|성적|장학|등록금|교환학생|교환수학|기숙사|생활관|도서관|증명서|상담센터|보건진료소|와이파이|취업|강의계획|교재|공지|캡스톤|학생증")
REFERENT = re.compile(r"거기|그곳|그거|그것|해당\s*(?:신청|프로그램)|그\s*(?:신청|프로그램|장학금|부서)")
SCHOOL_ALIASES = {"부경대": "부경대학교", "제주대": "제주대학교", "부산대": "부산대학교", "창원대": "창원대학교", "충북대": "충북대학교"}
AUDIENCES = {"외국인": ("외국인", "유학생", "international student"), "내국인": ("내국인",),
             "대학원": ("대학원", "석사", "박사"), "학부": ("학부생", "학부과정"),
             "신입생": ("신입생", "신입학"), "편입생": ("편입생", "편입학"),
             "재학생": ("재학생",), "휴학생": ("휴학생",)}
PROGRAM_CONTEXTS = {"현장실습": r"현장실습|실습생", "교류프로그램": r"교류\s*수학|교류\s*프로그램|대학생\s*교류",
                    "교환학생": r"교환학생|교환\s*수학", "한국어연수": r"한국어\s*연수",
                    "대회참가": r"경진대회|경연대회|공모전", "해외연수": r"해외\s*연수|어학\s*연수",
                    "감염병": r"코로나|백신|감염병"}


class Target(BaseModel):
    school: str = "부경대학교"
    subject: str | None = None
    department: str | None = None
    department_id: str | None = None
    audiences: list[str] = Field(default_factory=list)
    contexts: list[str] = Field(default_factory=list)
    grade: int | None = None
    admission_year: int | None = None
    instructor: str | None = None
    level: str | None = None


class Conditions(BaseModel):
    kind: Literal["search", "latest", "open", "courses"] = "search"
    limit: int = Field(default=5, ge=1, le=20)
    order_by: Literal["relevance", "published_at_desc", "course_name"] = "relevance"
    as_of: datetime = Field(default_factory=lambda: datetime.now(KST))
    year: int | None = None
    term: str | None = None
    topic: str | None = None


class SearchTask(BaseModel):
    text: str
    purpose: Literal["general", "eligibility", "procedure", "deadline", "contact", "assessment", "textbook", "weekly", "prerequisites", "instructor", "course_list"] = "general"
    target: Target = Field(default_factory=Target)
    conditions: Conditions = Field(default_factory=Conditions)
    required_conditions: list[str] = Field(default_factory=list)


class QuestionPlan(BaseModel):
    route: Literal["search", "clarification", "small_talk", "refused"] = "search"
    tasks: list[SearchTask] = Field(default_factory=list, max_length=3)
    missing_fields: list[str] = Field(default_factory=list)
    clarification: str | None = None
    response_format: str | None = None
    resolved_question: str = ""

    @property
    def queries(self) -> list[str]:
        return [task.text for task in self.tasks]


@lru_cache(maxsize=1)
def department_catalog() -> tuple[dict, ...]:
    path = departments._registry_path()
    if path is None:
        return ()
    return tuple(json.loads(path.read_text(encoding="utf-8")).get("departments", []))


def department_identity(text: str) -> tuple[str | None, str | None]:
    normalized = departments.normalize(text)
    aliases = [name for alias, names in departments.ALIASES.items() if alias in normalized for name in names]
    names = aliases + [name for name in departments.DEPARTMENT_NAMES if departments.normalize(name) in normalized]
    if not names:
        return None, None
    name = max(names, key=len) if not aliases else aliases[-1]
    for entry in department_catalog():
        labels = [entry.get("name"), entry.get("department"), *(entry.get("majors") or [])]
        if any(departments.normalize(name) in departments.normalize(str(label or "")) for label in labels):
            return name, entry.get("dataset")
    return name, None


def _subject_names(text: str) -> set[str]:
    return set(TOPICS.findall(text))


def resolve_context(question: str, history: list[dict]) -> tuple[str, bool]:
    """Use only a user's explicit, unique subject; never an assistant's guess."""
    course_question = resolve_course_question(question, history)
    if course_question != question:
        return course_question, False
    previous = next((t.get("content", "") for t in reversed(history) if t.get("role") == "user"), "")
    slot_answer = re.search(r"재학생|신입생|편입생|대학원|외국인|\d+\s*(?:학기|분반|학년)|컴공|[가-힣]+(?:학과|전공)", question)
    if previous and slot_answer and not TOPICS.search(question) and len(_subject_names(previous)) == 1:
        return question + " / " + previous, False
    referential = bool(REFERENT.search(question))
    generic = bool(re.search(r"신청|언제까지|전화번호|연락처", question)) and not TOPICS.search(question)
    if not (referential or generic):
        return question, False
    # Named offices, departments and courses are already subjects.
    name = re.search(r"[가-힣A-Za-z·]+(?:센터|사무실|학과|전공|학부|프로그램|장학금|본부|[과처관원팀])", question)
    if name and not referential:
        return question, False
    residual = re.sub(r"\d+\s*학년(?:인데|인|이야|입니다)?|저는|제가|저|신청|언제|까지야|까지|전화번호|연락처|알려\s*줘|알려주세요|뭐|무엇|어떤|할\s*수|있어요|가능한지|가능해|해요|[?!.\s]", "", question)
    if len(residual) >= 2 and not referential:
        # An unfamiliar programme name should be resolved by the planner,
        # rather than rejected merely because it is absent from TOPICS.
        return question, False
    subjects = _subject_names(previous)
    # '도서관 전화번호와 증명서 발급' contains two possible referents.
    contact = bool(re.search(r"전화번호|연락처", question))
    place = bool(re.search(r"도서관|보건진료소|상담센터|[가-힣]+(?:사무실|센터|학과|본부|과|처|관|팀)", previous))
    broad_scholarship = subjects == {"장학"} and not re.search(r"[가-힣]{2,}장학금", previous)
    if previous and len(subjects) == 1 and not broad_scholarship and (not contact or place):
        return question + " / " + previous, False
    return question, True


def target_for(text: str) -> Target:
    school = next((full for alias, full in SCHOOL_ALIASES.items() if alias in text), None)
    explicit = re.search(r"([가-힣]+대학교)", text)
    if explicit:
        school = explicit[1].removeprefix("국립")
    name, identity = department_identity(text)
    subject = re.search(r"[가-힣A-Za-z0-9·]+(?:장학금|프로그램|센터|도서관)", text)
    if not subject and not TOPICS.search(text):
        subject = re.search(r"[가-힣A-Za-z·]{2,}(?:본부|사무실|과|처|관|팀)", text)
    grade = re.search(r"([1-6])\s*학년", text)
    admission = re.search(r"(?<!\d)(20\d{2}|\d{2})\s*학번", text)
    admission_year = int(admission[1]) if admission else None
    if admission_year is not None and admission_year < 100:
        admission_year += 2000 if admission_year <= datetime.now(KST).year % 100 else 1900
    return Target(school=school or "부경대학교", subject=subject[0] if subject else next(iter(sorted(_subject_names(text), key=len, reverse=True)), None),
                  department=name, department_id=identity, grade=int(grade[1]) if grade else None, admission_year=admission_year,
                  audiences=[key for key, words in AUDIENCES.items() if any(w in text.lower() for w in words)],
                  contexts=[key for key, pattern in PROGRAM_CONTEXTS.items() if re.search(pattern, text)],
                  instructor=instructor_name(text), level=course_level(text))


def purpose_for(text: str) -> str:
    if is_course_list(text):
        return "course_list"
    for purpose, pattern in (("contact", r"전화번호|연락처"), ("prerequisites", r"선수\s*과목"),
                             ("assessment", r"평가\s*(?:비율|방법)|배점"), ("textbook", r"교재"),
                             ("weekly", r"주차|주별"), ("instructor", r"담당\s*교수"),
                             ("eligibility", r"자격|지원\s*조건"), ("deadline", r"언제|마감|기한"),
                             ("procedure", r"방법|절차|어떻게")):
        if re.search(pattern, text):
            return purpose
    return "general"


def conditions_for(text: str) -> Conditions:
    kind = "search"
    if is_course_list(text):
        kind = "courses"
    elif re.search(r"공지|게시글|게시물", text) and re.search(r"최근|최신|마지막|새로", text):
        kind = "latest"
    elif re.search(r"(?:오늘|지금|현재).{0,30}(?:신청\s*가능|신청할\s*수|모집\s*중|접수\s*중)|(?:신청\s*가능|모집\s*중|접수\s*중).{0,15}(?:목록|알려|있나)", text):
        kind = "open"
    count = re.search(r"(?<!\d)(\d{1,2})\s*(?:개|건)", text)
    term_index = current_term() if re.search(r"이번\s*학기", text) else query_term(text)
    year = re.search(r"(20\d{2})\s*(?:년|학년도)", text)
    compact = re.sub(r"\s+", "", text)
    topic = next((w for w in ("교내장학", "국가장학", "교외장학", "장학", "교환학생", "취업", "기숙사") if w in compact), None)
    return Conditions(kind=kind, limit=max(1, min(20, int(count[1]))) if count else 5,
                      order_by="course_name" if kind == "courses" else ("published_at_desc" if kind != "search" else "relevance"),
                      year=term_index // 2 if term_index else (int(year[1]) if year else None),
                      term=str(term_index % 2 + 1) if term_index else None, topic=topic)


def rule_plan(question: str, history: list[dict] | None = None) -> QuestionPlan:
    history = history or []
    resolved, ambiguous = resolve_context(question, history)
    plan = QuestionPlan(resolved_question=resolved)
    fmt = re.search(r"\d+\s*줄|체크리스트|체크박스|표로", question)
    plan.response_format = fmt[0] if fmt else None
    if re.search(r"(?:시스템\s*프롬프트|데이터베이스\s*비밀번호).{0,20}(?:출력|알려|공개)|출처에\s*없어도.{0,40}답", question):
        plan.route, plan.clarification = "refused", "비공개 정보나 근거 없는 내용을 제공할 수 없습니다."
    elif ambiguous:
        plan.route, plan.missing_fields = "clarification", ["subject"]
        plan.clarification = "어느 부서나 시설의 전화번호가 필요하신가요?" if re.search(r"전화번호|연락처", question) else "어떤 신청이나 프로그램에 대해 물으시는 건가요?"
    elif re.search(r"기숙사|생활관", resolved) and re.search(r"자격|신청\s*대상|입실\s*대상", resolved):
        audiences = target_for(resolved).audiences
        if not set(audiences) & {"신입생", "편입생", "재학생", "대학원", "학부"}:
            plan.route, plan.missing_fields = "clarification", ["audience"]
            plan.clarification = "신입생·재학생·대학원생 중 어느 학생 유형의 기숙사 신청 자격이 궁금하신가요?"
    return plan


def finish_plan(plan: QuestionPlan, question: str, queries: list[str], payload: dict | None = None) -> QuestionPlan:
    if plan.route != "search":
        return plan
    if is_course_list(plan.resolved_question):
        # Exact catalogue lookups do not depend on a model's wording or omissions.
        queries = [plan.resolved_question]
        payload = None
    if payload and payload.get("route") == "clarification" and isinstance(payload.get("missing_fields"), list):
        missing = [f for f in payload["missing_fields"] if f in {"subject", "department", "audience", "year", "term", "section"}]
        known_target = target_for(plan.resolved_question)
        known_conditions = conditions_for(plan.resolved_question)
        known = {"subject": known_target.subject, "department": known_target.department, "audience": known_target.audiences,
                 "year": known_conditions.year, "term": known_conditions.term,
                 "section": re.search(r"\d+\s*분반", plan.resolved_question)}
        missing = [field for field in missing if not known[field]]
        if missing and isinstance(payload.get("clarification"), str) and payload["clarification"].strip():
            plan.route, plan.missing_fields, plan.clarification = "clarification", missing, payload["clarification"].strip()[:300]
            return plan
        if not queries:
            queries = [plan.resolved_question]
    if not queries:
        # A model cannot route a substantive university question to small talk.
        if TOPICS.search(question):
            queries = [plan.resolved_question]
        else:
            plan.route = "small_talk"
            return plan
    original = plan.resolved_question or question
    global_target = target_for(original)
    global_conditions = conditions_for(original)
    # Never adopt a department/student type invented by the planner.
    for query in queries[:3]:
        target = target_for(query)
        if target.department and target.department not in (global_target.department, None):
            if departments.normalize(target.department) not in departments.normalize(original):
                target.department, target.department_id = global_target.department, global_target.department_id
        independent = re.search(r"도서관|증명서|보건진료소|상담센터|학생증", query)
        if not target.department and not independent:
            target.department, target.department_id = global_target.department, global_target.department_id
        if target.subject and departments.normalize(target.subject) not in departments.normalize(original):
            target.subject = global_target.subject
        if not any(name in original for name in (target.school, *(alias for alias, full in SCHOOL_ALIASES.items() if full == target.school))):
            target.school = global_target.school
        target.audiences = [a for a in target.audiences if a in global_target.audiences] or global_target.audiences
        target.contexts = [c for c in target.contexts if c in global_target.contexts] or ([] if independent else global_target.contexts)
        target.grade, target.admission_year = global_target.grade, global_target.admission_year
        if target.instructor not in instructor_names(original):
            target.instructor = global_target.instructor
        target.level = global_target.level or target.level
        conditions = conditions_for(query)
        if (global_conditions.kind == "latest" and re.search(r"공지|게시글|게시물", query)) or (
            global_conditions.kind == "open" and (global_conditions.topic is None or global_conditions.topic in re.sub(r"\s+", "", query) or ("장학" in (global_conditions.topic or "") and "장학" in query))
        ):
            conditions = global_conditions.model_copy(deep=True)
            local_count = re.search(r"(?<!\d)(\d{1,2})\s*(?:개|건)", query)
            if local_count:
                conditions.limit = max(1, min(20, int(local_count[1])))
        query = re.sub(r"\d+\s*줄(?:로)?|체크리스트|체크박스|표로", "", query).strip()
        required = ["school"]
        if target.department: required.append("department")
        if target.audiences: required.append("audience")
        if target.contexts: required.append("program_context")
        if target.grade: required.append("grade")
        if target.admission_year: required.append("admission_year")
        if target.instructor: required.append("instructor")
        if conditions.kind == "courses": required.extend(("year", "term", "level"))
        if conditions.kind == "open": required.extend(("application_start", "application_end"))
        if conditions.kind == "latest": required.extend(("published_at", "order", "limit"))
        plan.tasks.append(SearchTask(text=query, purpose=purpose_for(query), target=target, conditions=conditions, required_conditions=required))
    return plan
