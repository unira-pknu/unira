"""Resolve a course offering before choosing syllabus evidence inside it."""
from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Any

from .freshness import current_term, query_term, today_kst
from .course_identity import INSTRUCTOR, instructor_name, course_level, is_course_list, resolve_course_question

SEARCH_URL = "https://irumi.pknu.ac.kr/nxui/launch.html?screenid=Desktop_screen_quick&menuId=U020913"
SOURCE_LIMIT = 200
PURPOSES = {
    "assessment": ("평가", "중간고사", "기말고사", "배점", "비율"),
    "textbook": ("교재", "주교재", "부교재", "참고자료", "textbook"),
    "instructor": ("담당교수", "담당 교수", "교수님", "강사"),
    "prerequisites": ("선수과목", "선수 과목", "병수과목", "prerequisite"),
    "weekly": ("주차", "주별", "주간", "weekly"),
    "project": ("프로젝트", "project"),
    "language": ("강의언어", "강의 언어", "영어", "English"),
}
TRIGGER = re.compile(r"강의\s*계획서|선수\s*과목|주차별|주별\s*강의|교재|평가\s*(?:비율|방법)|담당\s*교수|(?:수업|강의).{0,15}(?:교수님|팀프로젝트|영어)", re.I)
LEGACY_TITLE = re.compile(r"^(?P<course_no>[A-Za-z0-9]+)_(?P<section>\d+)_(?P<course_name>.+)_(?P<variant>kor|eng|tech)_[a-f0-9]+\.pdf$")


@dataclass(frozen=True)
class CourseRequest:
    course: str = ""
    year: int | None = None
    term: str | None = None
    section: str | None = None
    course_no: str | None = None
    department: str | None = None
    instructor: str | None = None
    purposes: tuple[str, ...] = ()
    level: str | None = None
    list_courses: bool = False
    names_only: bool = False
    all_terms: bool = False
    all_levels: bool = False
    limit: int | None = None


@dataclass
class SyllabusResult:
    rows: list[dict]
    answer: str | None = None
    answerable: bool = True


def compact(value: str) -> str:
    return re.sub(r"\s+", "", value).casefold()


def extract_request(question: str, history: list[dict] | None = None) -> CourseRequest | None:
    history = history or []
    question = resolve_course_question(question, history)
    listing = is_course_list(question)
    previous = next((extract_request(turn["content"]) for turn in reversed(history)
                     if turn.get("role") == "user" and TRIGGER.search(turn.get("content", ""))), None)
    if not listing and not TRIGGER.search(question) and not (previous and re.search(r"학기|분반|그\s*(?:수업|과목)|교수|교재|평가", question)):
        return None
    purposes = tuple(key for key, words in PURPOSES.items() if any(word in question for word in words))
    year_match = re.search(r"(?<!\d)(20\d{2})(?:\s*(?:년|학년도))?", question)
    term_match = re.search(r"([12])\s*학기|하계|여름\s*계절|동계|겨울\s*계절", question)
    term = None
    if term_match:
        term = term_match.group(1) or ("summer" if re.search(r"하계|여름", term_match[0]) else "winter")
    target = current_term() if re.search(r"이번\s*학기", question) else query_term(question)
    if target is not None and re.search(r"이번\s*학기|지난\s*학기|다음\s*학기|작년|재작년", question):
        # freshness encodes regular semesters as year * 2 + (semester - 1).
        year, semester = divmod(target, 2)
        term = str(semester + 1)
    else:
        year = int(year_match[1]) if year_match else None
        if not year_match and re.search(r"작년|재작년", question):
            year = today_kst().year - (2 if "재작년" in question else 1)
    short_term = re.search(r"20\d{2}\s*[-/]\s*([12])(?!\d)(?:\s*학기)?", question)
    if not term and short_term:
        term = short_term[1]
    section = re.search(r"(?<!\d)(\d{1,3})\s*분반|분반\s*[:：]?\s*(\d{1,3})", question)
    code = re.search(r"학수번호\s*[:：]?\s*([A-Za-z0-9]+)", question)
    pair = re.search(r"\b([A-Za-z]?\d{5,6})[-–](\d{1,3})\b", question)
    department = re.search(r"([가-힣·A-Za-z]+(?:학과|학부|전공))", question)
    instructor = INSTRUCTOR.search(question)
    # Do not mistake the predicate "담당 교수" for a professor's name.
    staff = instructor_name(question)
    level = course_level(question)
    if listing:
        all_term_match = re.search(r"(?:전체|모든)\s*학기|학기\s*상관없", question)
        explicit_term = re.search(r"[12]\s*학기|20\d{2}\s*[-/]\s*[12]|(?:이번|지난|다음)\s*학기", question)
        all_terms = bool(all_term_match and (not explicit_term or all_term_match.start() < explicit_term.start()))
        if all_terms:
            term = None
            if not year_match or year_match.start() > all_term_match.start():
                year = None
        count = re.search(r"(?<!\d)(\d{1,2})\s*(?:개|건)", question)
        return CourseRequest(year=year, term=term, instructor=staff, level=level, list_courses=True,
                             names_only=bool(re.search(r"강의명|과목명|강좌명|이름만", question)),
                             all_terms=all_terms,
                             all_levels=level is None and bool(re.search(r"(?:전체|모든)\s*과정|학부.{0,5}대학원.{0,5}(?:모두|전체)|과정\s*상관없", question)),
                             limit=max(1, min(20, int(count[1]))) if count else None)
    cleaned = question
    for match in (pair, short_term, year_match, term_match, section, code, department):
        if match:
            cleaned = cleaned.replace(match[0], " ")
    if instructor and staff:
        cleaned = re.sub(re.escape(instructor[0]) + r"(?:께서|이|가|의)?\s*(?:진행하는|진행하시는|담당하는|담당하시는|가르치는)?", " ", cleaned)
    if level:
        cleaned = cleaned.replace(level, " ")
    cleaned = re.sub(r"이번\s*학기|지난\s*학기|다음\s*학기|작년|재작년|학년도|학수번호|분반|제\s*$", " ", cleaned).strip(" -·,")
    cleaned = re.sub(r"^(?:그럼|그러면)\s*", "", cleaned)
    cleaned = re.split(r"에서\s*(?:사용|쓰|활용)", cleaned, maxsplit=1)[0]
    # The noun phrase before the request purpose is the course identity;
    # purpose words never enter the document lookup's AND conditions.
    cleaned = re.split(r"강의\s*계획서|수업|강의는|강의의|평가|교재|선수\s*과목|담당\s*교수|주차|주별|팀프로젝트", cleaned, maxsplit=1)[0]
    cleaned = re.sub(r"(?:에서|에\s*대해|의|은|는|을|를)\s*$", "", cleaned.strip()).strip(" \t.,?!'\"“”")
    cleaned = re.sub(r"\s+(?:과목|어떤|무슨)\s*$", "", cleaned).strip()
    if cleaned in {"", "그", "그 과목", "그 수업", "같은 과목이라도", "알려줘", "알려주세요", "이요", "요", "입니다"}:
        cleaned = ""
    request = CourseRequest(cleaned, year, term,
                            (section[1] or section[2]) if section else (pair[2] if pair else None),
                            code[1] if code else (pair[1] if pair else None), department[1] if department else None, staff, purposes, level)
    if previous and not request.course and not request.course_no:
        return replace(request, course=previous.course, course_no=previous.course_no,
                       year=request.year if request.year is not None else previous.year,
                       term=request.term or previous.term, section=request.section or previous.section,
                       department=request.department or previous.department,
                       instructor=request.instructor or previous.instructor,
                       level=request.level or previous.level,
                       purposes=request.purposes or previous.purposes)
    return request


def offering(source: dict) -> dict:
    metadata = dict(source.get("metadata") or {})
    match = LEGACY_TITLE.match(str(source.get("title") or ""))
    if match:
        for key, value in match.groupdict().items():
            if not metadata.get(key):
                metadata[key] = value
    path_match = re.search(r"/(20\d{2})/(1|2|summer|winter)/", str(source.get("file_path") or metadata.get("source_path") or "").replace("\\", "/"))
    if path_match:
        metadata.setdefault("year", int(path_match[1]))
        metadata.setdefault("term", path_match[2])
    return {**source, "metadata": metadata}


def matches(source: dict, request: CourseRequest) -> bool:
    m = source["metadata"]
    if request.course and compact(str(m.get("course_name") or "")) != compact(request.course):
        return False
    for key, expected in (("year", request.year), ("term", request.term), ("course_no", request.course_no)):
        if expected is not None and str(m.get(key)) != str(expected):
            return False
    if request.section is not None and str(m.get("section", "")).lstrip("0") != request.section.lstrip("0"):
        return False
    if request.level and not level_matches(str(m.get("level") or ""), request.level):
        return False
    return True


def level_matches(value: str, expected: str) -> bool:
    return value.endswith("대학원") if expected == "대학원" else value == expected


def search_instructor_courses(client: Any, request: CourseRequest) -> SyllabusResult:
    """Enumerate source metadata exactly; no vector search or generated joins."""
    if not request.instructor:
        return SyllabusResult([], "어느 교수님의 강의 목록이 궁금하신가요?", False)
    query = client.table("pknu_syllabus_sources").select("id,title,url,parent_url,file_path,metadata").eq("status", "active")
    query = query.eq("metadata->>staff_name", request.instructor)
    for key, value in (("year", request.year), ("term", request.term)):
        if value is not None:
            query = query.eq("metadata->>" + key, str(value))
    if request.level and not request.all_levels:
        query = query.like("metadata->>level", "%대학원") if request.level == "대학원" else query.eq("metadata->>level", request.level)
    raw = query.order("id").limit(SOURCE_LIMIT + 1).execute().data or []
    if len(raw) > SOURCE_LIMIT:
        return SyllabusResult([], "강의계획서 후보가 많습니다. 학년도·학기·과정을 지정해 주세요.", False)
    sources = [offering(item) for item in raw]
    sources = [s for s in sources if compact(str(s["metadata"].get("staff_name", ""))) == compact(request.instructor)
               and all(s["metadata"].get(k) not in (None, "") for k in ("course_name", "course_no", "section", "year", "term", "level"))
               and matches(s, replace(request, level=None if request.all_levels else request.level))]
    if not sources:
        return SyllabusResult([], f"{request.instructor} 교수님의 해당 학기·과정에 일치하는 강의계획서를 찾지 못했습니다. 수집된 자료 범위의 결과입니다.\n\n[공식 강의계획서 조회 화면]({SEARCH_URL})", False)
    scopes = sorted({(str(s["metadata"]["year"]), str(s["metadata"]["term"]), str(s["metadata"]["level"])) for s in sources})
    ambiguous_term = not request.all_terms and len({(y, t) for y, t, _ in scopes}) > 1
    ambiguous_level = not request.all_levels and len({level for _, _, level in scopes}) > 1
    if ambiguous_term or ambiguous_level:
        choices = "\n".join(f"- {y}학년도 {t}학기 · {level}" for y, t, level in scopes[:12])
        return SyllabusResult([], f"{request.instructor} 교수님의 어느 학기·과정 강의 목록이 필요하신가요?\n\n{choices}\n\n예: 2026학년도 2학기 학부. 전체 학기·모든 과정으로도 조회할 수 있습니다.", False)
    # Language/report variants represent the same offering. Sections represent
    # separate offerings, but the requested course-name list groups them.
    offerings: dict[tuple, list[dict]] = {}
    for source in sources:
        m = source["metadata"]
        key = tuple(str(m[k]) for k in ("year", "term", "level", "course_no", "section"))
        offerings.setdefault(key, []).append(source)
    courses: dict[tuple, list[dict]] = {}
    for key, variants in offerings.items():
        selected = min(variants, key=lambda s: {"kor": 0, "tech": 1, "eng": 2}.get(s["metadata"].get("variant"), 3))
        courses.setdefault(key[:-1], []).append(selected)
    lines, evidence, source_ids = [], [], []
    current_scope = None
    ordered = sorted(courses.items(), key=lambda item: (item[0][:3], str(item[1][0]["metadata"]["course_name"])))
    for key, sections in ordered[:request.limit]:
        m = sections[0]["metadata"]
        scope = key[:3]
        if scope != current_scope:
            lines.append(f"\n{scope[0]}학년도 {scope[1]}학기 · {scope[2]}")
            current_scope = scope
        section_names = sorted({str(s["metadata"]["section"]) for s in sections})
        detail = "" if request.names_only else f" — 학수번호 {m['course_no']}, {', '.join(section_names)}분반"
        lines.append(f"- {m['course_name']}{detail} [1]")
        evidence.append({"year": m["year"], "term": m["term"], "level": m["level"], "course_name": m["course_name"],
                         "course_no": m["course_no"], "sections": section_names, "staff_name": request.instructor})
        source_ids.extend(str(s["id"]) for s in sections)
    # The citation describes the official catalogue, rather than pretending a
    # single PDF supplies the metadata of all the instructor's courses.
    title = "공식 강의계획서 조회 — 교수별 강의 목록"
    content = "강의계획서 문서 메타데이터에서 담당교수·학기·과정을 일치시켜 조회한 목록입니다.\n" + "\n".join(
        f"{e['year']}-{e['term']} {e['level']} | {e['course_name']} | {e['staff_name']} | {e['course_no']} | 분반 {', '.join(e['sections'])}" for e in evidence)
    metadata = {"doc_title": title, "doc_url": SEARCH_URL, "source_link_kind": "syllabus_search_page", "source_dataset": "pknu_syllabus",
                "staff_name": request.instructor, "level": request.level or (scopes[0][2] if len(scopes) == 1 else ""),
                "catalogue_offerings": evidence, "source_ids": source_ids, "rpc_name": "match_pknu_syllabus_documents"}
    row = {"id": "syllabus-catalogue:" + request.instructor, "title": title, "url": SEARCH_URL, "content": content,
           "metadata": metadata, "similarity": 1.0, "_syllabus": True}
    return SyllabusResult([row], f"수집된 강의계획서에서 확인한 {request.instructor} 교수님의 강의 목록입니다.\n" + "\n".join(lines))


def label(source: dict) -> str:
    m = source["metadata"]
    term = {"summer": "하계", "winter": "동계"}.get(str(m.get("term")), f"{m.get('term', '?')}학기")
    return (f"{m.get('year', '?')}년 {term} · {m.get('course_name', '?')} · "
            f"학수번호 {m.get('course_no', '?')} · {m.get('section', '?')}분반 · "
            f"{m.get('department') or '학과 미확인'} · {m.get('staff_name') or '교수 미확인'}")


def evidence_window(content: str, purposes: tuple[str, ...], limit: int = 3500) -> str:
    words = [word for purpose in purposes for word in PURPOSES[purpose]]
    positions = [content.find(word) for word in words if word in content]
    start = max(0, min(positions) - 120) if positions else 0
    # Prefer complete lines when table rows have been extracted structurally.
    start = content.rfind("\n", 0, start) + 1 if "\n" in content[:start] else start
    return content[start:start + limit]


def search_syllabus(client: Any, request: CourseRequest, *, top_k: int = 8) -> SyllabusResult:
    if request.list_courses:
        return search_instructor_courses(client, request)
    if not request.course and not request.course_no:
        return SyllabusResult([], f"어떤 과목의 강의계획서인가요? 과목명과 학기·분반을 알려주세요.\n\n[공식 강의계획서 조회 화면]({SEARCH_URL})")
    # Only course identity enters this query. No full-text AND of question words.
    query = client.table("pknu_syllabus_sources").select("id,title,url,parent_url,file_path,metadata").eq("status", "active")
    needle = request.course_no or request.course
    escaped = needle.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")
    # Whitespace may differ in titles; ordered name parts are safe ILIKE terms.
    pattern = "%" + "%".join(escaped.split()) + "%"
    raw = query.ilike("title", pattern).order("id").limit(SOURCE_LIMIT + 1).execute().data or []
    candidates = [source for item in raw if matches(source := offering(item), request)]
    if len(raw) > SOURCE_LIMIT:
        return SyllabusResult([], "후보가 많습니다. 과목명과 학수번호·학기·분반을 더 구체적으로 알려주세요.")
    if not candidates:
        return SyllabusResult([], f"요청한 과목·학기·분반에 일치하는 강의계획서를 찾지 못했습니다. 과목명이나 학수번호를 확인해 주세요.\n\n[공식 강의계획서 조회 화면]({SEARCH_URL})", False)
    # First chunks supplement old rows that predate receipt metadata repair.
    headers = client.table("pknu_syllabus_chunks").select("source_id,content").in_("source_id", [s["id"] for s in candidates]).eq("chunk_index", 0).limit(SOURCE_LIMIT + 1).execute().data or []
    header_by_id = {row["source_id"]: row["content"] for row in headers}
    for source in candidates:
        m = source["metadata"]
        header = header_by_id.get(source["id"], "")
        for key, pattern in (("staff_name", r"담당교수\s+(.{1,40}?)(?=\s+(?:연구실|\(인\)|강의시간))"),
                             ("department", r"개설학\(부\)과\s+(.{1,60}?)(?=\s+개설학년)")):
            match = re.search(pattern, header)
            if not m.get(key) and match:
                m[key] = match[1].strip()
    if request.department:
        candidates = [s for s in candidates if compact(str(s["metadata"].get("department", ""))) == compact(request.department)]
    if request.instructor:
        candidates = [s for s in candidates if compact(str(s["metadata"].get("staff_name", ""))) == compact(request.instructor)]
    groups: dict[tuple, list[dict]] = {}
    for source in candidates:
        m = source["metadata"]
        key = tuple(str(m.get(k, "")) for k in ("year", "term", "course_no", "section"))
        groups.setdefault(key, []).append(source)
    if len(groups) != 1:
        choices = sorted((min(items, key=lambda s: {"kor": 0, "tech": 1, "eng": 2}.get(s["metadata"].get("variant"), 3))
                          for items in groups.values()), key=label)
        text = "학기·분반에 따라 내용이 다릅니다. 아래에서 원하는 강좌의 학기·학수번호·분반을 알려주세요."
        if not choices:
            text = "해당 학과·교수 조건에 일치하는 강좌를 찾지 못했습니다. 조건을 확인해 주세요."
        text += "\n\n" + "\n".join(f"- {label(s)}" for s in choices[:12])
        if len(choices) > 12:
            text += f"\n- 외 {len(choices) - 12}개 강좌. 학기를 먼저 지정해 주세요."
        return SyllabusResult([], text + f"\n\n[공식 강의계획서 조회 화면]({SEARCH_URL})")
    variants = next(iter(groups.values()))
    # Prefer the normal Korean report. A translated/engineering variant must
    # never crowd out another offering or silently substitute its conditions.
    variants.sort(key=lambda s: {"kor": 0, "tech": 1, "eng": 2}.get(s["metadata"].get("variant"), 3))
    selected = variants[0]
    chunks = client.table("pknu_syllabus_chunks").select("id,source_id,chunk_id,chunk_index,content,metadata").eq("source_id", selected["id"]).order("chunk_index").limit(201).execute().data or []
    if len(chunks) > 200:
        return SyllabusResult([], "강의계획서가 커서 전체 내용을 확인하지 못했습니다. 조회 화면에서 원문을 확인해 주세요.", False)
    words = [word for purpose in request.purposes for word in PURPOSES[purpose]]
    chunks.sort(key=lambda row: sum(word in row["content"] for word in words), reverse=True)
    rows = []
    count = 32 if "weekly" in request.purposes else max(1, min(top_k, 8))
    for chunk in chunks[:count]:
        m = {**(chunk.get("metadata") or {}), **selected["metadata"]}
        m.update(doc_title=label(selected), doc_url=selected.get("url") or selected.get("parent_url") or SEARCH_URL,
                 source_link_kind="syllabus_search_page", rpc_name="match_pknu_syllabus_documents")
        rows.append({**chunk, "title": label(selected), "url": m["doc_url"], "metadata": m,
                     "similarity": 1.0, "_syllabus": True,
                     "content": evidence_window(chunk["content"], request.purposes)})
    return SyllabusResult(rows)
