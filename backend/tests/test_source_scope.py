from app.query_plan import SearchTask, target_for, conditions_for, purpose_for
from app.source_scope import checked_rows, scope_reason


def task(question):
    return SearchTask(text=question, target=target_for(question), conditions=conditions_for(question), purpose=purpose_for(question))


def test_pknu_host_does_not_make_jeju_exchange_rules_general():
    row = {"title": "2023학년도 타 대학(제주대학교) 교류 수학 안내", "url": "https://www.pknu.ac.kr/main/95?no=1", "content": "공결은 제주대학교 교육혁신과로 신청"}
    assert scope_reason(row, task("공결 신청 방법")) is not None
    assert scope_reason(row, task("제주대학교 교류수학 공결 신청 방법")) is None


def test_international_freshman_dormitory_notice_is_not_general():
    row = {"title": "2026학년도 외국인 특별전형 신·편입학 합격자 안내", "content": "기숙사 신청과 결핵증명서"}
    assert scope_reason(row, task("기숙사 신청 자격")) == "restricted_audience"
    assert scope_reason(row, task("외국인 신입생 기숙사 신청 자격")) is None


def test_one_major_selection_rule_cannot_be_generalized():
    row = {"title": "전과 세부 선발기준", "content": "위성정보융합공학전공 전입 선발 직전 2개 학기 2.50 이상"}
    assert scope_reason(row, task("전과 신청 자격")) == "department_specific_rule"
    common = {"title": "전과에 관한 규정", "content": "1개 학기 이상 이수한 학생이 신청할 수 있다"}
    assert scope_reason(common, task("전과 신청 자격")) is None


def test_department_must_match_metadata_even_if_title_mentions_computer():
    row = {"title": "컴퓨터공학전공 관련 수강신청", "metadata": {"department_id": "humanbio"}}
    assert scope_reason(row, task("컴공 최신 공지 3개")) == "different_department"


def test_catalog_can_list_restricted_notices_without_claiming_eligibility():
    row = {"title": "외국인 대학원생 장학 안내", "metadata": {"department_id": "ce"}}
    assert scope_reason(row, task("컴공 최신 공지 3개")) is None
    assert scope_reason(row, task("컴공 장학금 신청 자격")) == "restricted_audience"


def test_emergency_exception_requires_explicit_context():
    row = {"title": "외국인 학생 휴학", "content": "감염병 확산에 따라 국내 입국이 어려운 경우 증빙 없이 신청"}
    assert scope_reason(row, task("외국인 학생 휴학 방법")) == "conditional_exception"
    assert scope_reason(row, task("감염병으로 입국이 어려운 외국인 학생 휴학 방법")) is None


def test_compatible_sources_keep_scope_and_condition_evidence():
    rows = checked_rows([{"title": "휴복학 안내", "content": "공통 절차"}], task("휴학 신청 방법"))
    assert rows[0]["metadata"]["validated_target"]["school"] == "부경대학교"
    assert "신청자격" in rows[0]["metadata"]["applicability_note"]


def test_completion_credits_do_not_establish_application_eligibility():
    row = {"title": "다전공 안내", "content": "이수조건 부전공 21학점 복수전공 36학점"}
    assert scope_reason(row, task("복수전공 신청 자격")) == "completion_not_application_eligibility"
    row["content"] += " 신청자격 1개 학기 이상 이수한 학생"
    assert scope_reason(row, task("복수전공 신청 자격")) is None


def test_explicit_grade_and_cohort_mismatch():
    row = {"title": "복수전공 안내", "metadata": {"applicable_grades": [3, 4]}}
    assert scope_reason(row, task("26학번 2학년 복수전공 신청 자격")) == "different_grade"


def test_internship_lodging_is_not_the_university_dormitory():
    row = {"title": "한국해양과학기술원 하계 현장실습 운영 안내", "content": "기숙사는 근무지 40km 이내 거주자 이용 불가"}
    assert scope_reason(row, task("기숙사 신청 자격")) == "different_program_context"
    assert scope_reason(row, task("한국해양과학기술원 현장실습 기숙사 신청 자격")) is None


def test_syllabus_with_generic_reason_word_is_not_absence_policy():
    row = {"title": "사유와표현", "metadata": {"source_dataset": "pknu_syllabus"}, "content": "과목 개요"}
    assert scope_reason(row, task("공결로 인정되는 사유")) == "different_document_purpose"


def test_individual_syllabus_is_not_a_major_curriculum_comparison():
    row = {"title": "2026-1 컴퓨터공학개론", "metadata": {"source_dataset": "pknu_syllabus"}, "content": "강의계획서"}
    assert scope_reason(row, task("컴퓨터공학전공 전공필수 과목")) == "different_document_purpose"
    assert scope_reason(row, task("컴퓨터공학개론 강의계획서 평가 방법")) is None


def test_sibling_majors_with_the_same_dataset_are_still_distinct():
    row = {"title": "컴퓨터공학전공 교과과정", "metadata": {"department_id": "ce"}, "content": "전공필수 목록"}
    assert scope_reason(row, task("인공지능전공 전공필수 과목")) == "different_major"
    assert scope_reason(row, task("컴퓨터공학전공 전공필수 과목")) is None


def test_infection_exception_does_not_replace_general_absence_reasons():
    row = {"title": "출석인정 신청 안내", "content": "감염 증상은 진단서 제출, 백신 접종 차수별 1회만 가능"}
    assert scope_reason(row, task("공결 신청 방법")) == "conditional_exception"


def test_non_degree_language_course_rules_are_not_general_student_rules():
    row = {"title": "국립부경대학교 한국어연수과정 운영 지침", "content": "질병 1개월 미만 결석"}
    assert scope_reason(row, task("공결 인정 사유")) == "different_program_context"


def test_future_amendment_is_not_current_policy():
    from datetime import datetime
    from app.query_plan import KST
    row = {"title": "학사운영 규정(개정 2027.07.01.)"}
    request = task("공결 인정 사유")
    request.conditions.as_of = datetime(2026, 11, 7, tzinfo=KST)
    assert scope_reason(row, request) == "future_amendment"
    request.conditions.kind = "latest"
    assert scope_reason(row, request) is None  # An announcement can describe a future amendment.


def test_admission_table_section_does_not_apply_to_enrolled_students():
    row = {"title": "학생생활관 입실생 선발지침", "content": "수시전형 합격자 정시전형 합격자 편입생 합격자 신청기간"}
    assert scope_reason(row, task("재학생 기숙사 신청 자격")) == "restricted_student_section"


def test_spreadsheet_row_instructor_and_term_are_checked():
    row = {"title": "개설 교과목", "metadata": {"structured_table": {"kind": "spreadsheet", "rows": [
        {"fields": {"staff_name": "오정은", "course_name": "수리학및실험", "year": "2026", "term": "2"}}]}}}
    request = task("2026년 2학기 유승호 교수님 소프트웨어공학 강의실")
    assert scope_reason(row, request) == "different_instructor"
    row["metadata"]["structured_table"]["rows"][0]["fields"]["staff_name"] = "유승호"
    request.conditions.term = "1"
    assert scope_reason(row, request) == "different_course_term"


def test_old_unstructured_spreadsheet_cannot_establish_professor_course_relation():
    row = {"title": "개설 교과목", "content": "유승호 수리학및실험", "metadata": {"source_ext": ".xlsx"}}
    assert scope_reason(row, task("유승호 교수님 소프트웨어공학 강의실")) == "unverified_spreadsheet_instructor"
