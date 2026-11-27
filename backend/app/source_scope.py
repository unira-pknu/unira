"""Check a document's applicability independently of its retrieval score."""
from __future__ import annotations

import re
from datetime import date
from urllib.parse import urlparse

from . import department_focus as departments
from .document_focus import course_evaluation_app_mismatch, meal_date_mismatch
from .query_plan import AUDIENCES, PROGRAM_CONTEXTS, SCHOOL_ALIASES, SearchTask, department_catalog
from .query_transform import named_document_request


_EXCHANGE_TRACKS = ("혁신융합대학", "혁신공유대학", "디지털신기술", "공유대학")
_SEASONS = {"summer": r"하기|하계|여름", "winter": r"동기|동계|겨울"}
_FORM_LENGTH = re.compile(r"글자\s*수|몇\s*(?:글자|자)|글자\s*제한|분량|자\s*이내")
_PERSONAL_APPLICATION = re.compile(r"제가|저는|나는|나도|저도|(?:신청|지원)\s*할\s*수|(?:신청|지원)\s*가능")


def _dated_document_mismatch(title: str, task: SearchTask) -> str | None:
    text = task.text
    exchange = bool(re.search(r"학점\s*교류|교류\s*수학|교류\s*신청", text)
                    and re.search(r"학점\s*교류|교류\s*수학|교류\s*신청", title))
    if exchange:
        track = next((name for name in _EXCHANGE_TRACKS if name in title), None)
        if track and track not in text:
            return "different_exchange_track"
        seasons = {name for name, pattern in _SEASONS.items() if re.search(pattern, text)}
        title_seasons = {name for name, pattern in _SEASONS.items() if re.search(pattern, title)}
        if len(seasons) == 1 and len(title_seasons) == 1 and seasons != title_seasons:
            return "different_exchange_season"
    named_edition = bool(re.search(r"규정|세칙|지침", text) and re.search(r"규정|세칙|지침", title)
                         and re.search(r"개정|게시|20\d{2}\s*년\s*\d{1,2}\s*월", text))
    named_event = bool(re.search(r"졸업사정|공모전|채용상담회|교환학생.*모집", text)
                       and re.search(r"졸업사정|공모전|채용상담회|교환학생.*모집", title))
    if exchange or named_edition or named_event:
        question_years = set(re.findall(r"(?<!\d)(20\d{2})(?=\s*(?:년|학년도))", text))
        title_years = set(re.findall(r"(?<!\d)(20\d{2})(?!\d)", title))
        if len(question_years) == 1 and len(title_years) == 1 and question_years != title_years:
            return "different_document_year"
    return None


def source_department(row: dict) -> str | None:
    m = row.get("metadata") or {}
    identity = m.get("department_id")
    if identity:
        return str(identity)
    dataset = m.get("source_dataset") or m.get("ingestion_dataset")
    catalog = department_catalog()
    if any(e.get("dataset") == dataset for e in catalog):
        return str(dataset)
    url = row.get("url") or row.get("uri") or m.get("doc_url") or ""
    parsed = urlparse(str(url))
    for entry in catalog:
        host = urlparse(entry.get("base_url") or "").hostname
        prefix = entry.get("site_prefix")
        if (host and host not in {"www.pknu.ac.kr", "pknu.ac.kr", "icms.pknu.ac.kr"} and parsed.hostname == host) or (prefix and parsed.hostname == "icms.pknu.ac.kr" and parsed.path.startswith(f"/{prefix}/")):
            return entry.get("dataset")
    return None


def scope_reason(row: dict, task: SearchTask) -> str | None:
    """None means compatible, not proof that every statement is supported."""
    m = row.get("metadata") or {}
    title = str(m.get("doc_title") or row.get("title") or "")
    content = str(row.get("content") or "")
    document_lookup = named_document_request(task.text, title)
    reviewed_form_length = bool(
        m.get("reviewed_campus_source") and m.get("attachment_name") and task.purpose == "general"
        and re.search(r"자기\s*소개서|자소서", task.text) and _FORM_LENGTH.search(task.text)
        and not _PERSONAL_APPLICATION.search(task.text)
    )
    target = task.target
    table = m.get("structured_table") or {}
    table_rows = table.get("rows") or []
    fields = table_rows[0].get("fields", {}) if table.get("kind") == "spreadsheet" and len(table_rows) == 1 else {}
    staff = str(fields.get("staff_name") or m.get("staff_name") or "")
    spreadsheet = table.get("kind") == "spreadsheet" or str(m.get("source_ext") or "").lower() in {".xlsx", ".xls"}
    if target.instructor and spreadsheet and re.search(r"강의|수업|과목|강좌", task.text) and not fields.get("staff_name"):
        return "unverified_spreadsheet_instructor"
    if target.instructor and ((staff and staff != target.instructor) or (task.conditions.kind == "courses" and staff != target.instructor)):
        return "different_instructor"
    if target.level and (fields.get("level") or m.get("level")):
        level = str(fields.get("level") or m["level"])
        if not (level.endswith("대학원") if target.level == "대학원" else level == target.level):
            return "different_course_level"
    if fields:
        for key, expected in (("year", task.conditions.year), ("term", task.conditions.term)):
            if expected is not None and fields.get(key) and str(fields[key]) != str(expected):
                return "different_course_term"
    amendment = re.search(r"(?:개정|시행)\s*\(?\s*(20\d{2})\s*[.-]\s*(\d{1,2})\s*[.-]\s*(\d{1,2})", title)
    if amendment and task.conditions.kind == "search":
        try:
            if date(*(int(x) for x in amendment.groups())) > task.conditions.as_of.date():
                return "future_amendment"
        except ValueError:
            pass
    if task.conditions.kind == "search":
        if meal_date_mismatch(task.text, title, str(m.get("published_at") or m.get("date") or "")):
            return "different_meal_week"
        if course_evaluation_app_mismatch(task.text, f"{title}\n{content}"):
            return "different_course_evaluation_app"
        if mismatch := _dated_document_mismatch(title, task):
            return mismatch
        for context, pattern in PROGRAM_CONTEXTS.items():
            if not document_lookup and context not in target.contexts and re.search(pattern, title):
                return "different_program_context"
        syllabus = row.get("_syllabus") or m.get("source_dataset") == "pknu_syllabus" or m.get("rpc_name") == "match_pknu_syllabus_documents"
        if (syllabus and not row.get("_syllabus")
                and re.search(r"전공\s*(?:필수|공통)|교육과정|교과과정|이수표", task.text)
                and not re.search(r"강의계획서|담당\s*교수|학수번호|분반|평가", task.text)):
            return "different_document_purpose"
        if syllabus and not row.get("_syllabus") and not re.search(r"강의|수업|과목|교수|학수번호|분반", task.text):
            return "different_document_purpose"
        infection_exception = re.search(r"백신|감염\s*증상|코로나", content)
        common_absence_rules = re.search(r"경조사|병역|공적인?\s*의무|직계|사망|대회", content)
        if infection_exception and not common_absence_rules and "감염병" not in target.contexts:
            if re.search(r"공결|출석인정", task.text):
                return "conditional_exception"
    school = str(m.get("applicable_school") or m.get("university") or m.get("school") or "")
    school = SCHOOL_ALIASES.get(school, school).removeprefix("국립")
    exchange = re.search(r"타\s*대학\s*\(([^)]+)\)", title)
    if exchange:
        school = SCHOOL_ALIASES.get(exchange[1], exchange[1]).removeprefix("국립")
    if school and school != target.school and not document_lookup:
        if not (exchange and re.search(r"교류|교환|타\s*대학", task.text)):
            return "different_school"
    # Publishing a document on pknu.ac.kr does not make another school's rules applicable.
    identity = source_department(row)
    if target.department_id and identity and identity != target.department_id:
        return "different_department"
    # Several majors share one dataset (e.g. ce). A dataset match alone must
    # not make a sibling major's explicit curriculum heading applicable.
    if target.department:
        wanted_major = departments.normalize(target.department)
        heading = departments.normalize(title + " " + str(m.get("subcategory") or ""))
        for entry in department_catalog():
            majors = [departments.normalize(name.strip()) for value in entry.get("majors") or []
                      for name in str(value).split(",") if name.strip()]
            if wanted_major in majors and any(name in heading for name in majors) and wanted_major not in heading:
                return "different_major"
    if task.conditions.kind == "latest" and target.department_id and identity != target.department_id:
        return "department_unverified"
    applicable = m.get("applicable_department")
    if applicable:
        variants = departments.focus_variants(target.department or "")
        if not variants or not departments.mentions(str(applicable), variants):
            return "different_department"
    # A latest-notice catalogue describes documents, not the user's eligibility.
    catalogue = task.conditions.kind == "latest"
    restricted = str(m.get("applicable_audience") or m.get("audience") or "")
    heading = f"{title} {m.get('subcategory') or ''} {restricted}"
    if "학부" in target.audiences and "대학원" in heading:
        return "different_level"
    if task.conditions.kind == "search" and re.search(r"(?:수시|정시)전형\s*합격자|편입생\s*합격자", content) and "재학생" not in content:
        if not set(target.audiences) & {"신입생", "편입생"}:
            return "restricted_student_section"
    for audience in ("외국인", "대학원", "신입생", "편입생"):
        if any(w in heading.lower() for w in AUDIENCES[audience]):
            if not catalogue and not document_lookup and not reviewed_form_length and audience not in target.audiences:
                # '신·편입생' is one combined population; either explicit type suffices.
                if audience in {"신입생", "편입생"} and set(target.audiences) & {"신입생", "편입생"}:
                    continue
                return "restricted_audience"
    grades = m.get("applicable_grades")
    if target.grade and isinstance(grades, list) and str(target.grade) not in {str(g) for g in grades}:
        return "different_grade"
    cohorts = m.get("applicable_admission_years")
    if target.admission_year and isinstance(cohorts, list) and str(target.admission_year) not in {str(y) for y in cohorts}:
        return "different_admission_year"
    if task.purpose == "eligibility" and re.search(r"신청|지원", task.text):
        if re.search(r"이수\s*조건", content) and not re.search(r"(?:신청|지원)\s*(?:자격|대상|가능)|신청할\s*수", content):
            return "completion_not_application_eligibility"
    # Per-major selection rules must not become university-wide requirements.
    named = [name for name in departments.DEPARTMENT_NAMES if departments.normalize(name) in departments.normalize(content)]
    # Historical/renamed majors may no longer appear in today's registry.
    named += [name for name in re.findall(r"[가-힣·]{3,}(?:학과|학부|전공)", content)
              if name not in {"복수전공", "부전공", "주전공", "다전공", "해당학과", "소속학과", "해당전공"}]
    selection_table = bool(re.search(r"세부\s*선발|전입\s*선발|전출\s*선발|학과별\s*기준", title + " " + content))
    if selection_table and named:
        variants = departments.focus_variants(target.department or "")
        if not variants:
            return "department_specific_rule"
        if not departments.mentions(content, variants):
            return "different_department"
    if re.search(r"감염병\s*확산|코로나.{0,15}(?:확산|입국)", content) and re.search(r"입국이?\s*어려|입국.{0,10}불가", content):
        if not re.search(r"감염병|코로나|입국", task.text):
            return "conditional_exception"
    return None


def checked_rows(rows: list[dict], task: SearchTask) -> list[dict]:
    kept = []
    for row in rows:
        if scope_reason(row, task):
            continue
        m = dict(row.get("metadata") or {})
        m["validated_target"] = task.target.model_dump()
        m["query_conditions"] = task.conditions.model_dump(mode="json")
        m["request_purpose"] = task.purpose
        m["request_query"] = task.text
        # This label travels with the evidence into generation; URL alone is not scope.
        m["applicability_note"] = "공통 규정과 특정 대상의 예외를 구분하고, 신청자격을 이수조건으로 대체하지 마세요. 학번·학년·개인 자격을 확인하지 못했으면 해당 학생에게 적용된다고 확정하지 마세요."
        if named_document_request(task.text, str(m.get("doc_title") or row.get("title") or "")):
            m["request_document_lookup"] = True
            m["applicability_note"] += " 특정 기관의 공고 조회입니다. 공고의 모집 대상·조건을 설명하되 사용자가 그 자격을 충족한다고 확정하지 마세요."
        kept.append({**row, "metadata": m})
    return kept
