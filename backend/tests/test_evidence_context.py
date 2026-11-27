"""Evidence survives late passages and table boundaries without live services."""

import hashlib
from types import SimpleNamespace
from unittest.mock import patch

from app import generation
from app.document_focus import menu_titles
from app.evidence_context import select_evidence, supplement_neighbors
from app.evaluation import evidence_snapshot
from app.generation import _format_source_block, _number_problems, _source_content, build_user_message
from app.query_plan import QuestionPlan, SearchTask
from app.schemas import Source


def test_specific_public_menu_titles_are_scoped_to_the_question():
    assert menu_titles("부산역에서 대연캠퍼스 갈 때 지하철 몇 번 출구야?") == ["대연캠퍼스"]
    assert menu_titles("대학원 수료 상태인데 ISIC 발급돼요?") == ["국제학생증발급"]
    assert "연혁" in menu_titles("응용수학과 입학정원", department=True)
    assert menu_titles("오늘 날씨 어때요?") == []


def test_course_evaluation_menu_lookup_matches_navigation_and_respects_dated_notices():
    assert menu_titles("폰으로 강의평가 하려면 어디로 들어가요?") == ["강의평가"]
    assert menu_titles("휴대폰에서 수업 평가 어디서 해요?") == ["강의평가"]
    assert menu_titles("2020학년도 2학기 강의평가 앱 메뉴가 어디였어요?") == []
    assert menu_titles("모바일 강의평가 마감은 언제예요?") == []
    assert menu_titles("수강신청 앱에서 강의평가 하는 방법이 뭐였어?") == []


def test_flattened_meal_table_uses_the_named_cafeteria_and_keeps_the_day_header():
    content = ("본문: 대연캠퍼스 라일락과 다래락 안내 "
               "라일락 구분 / Monday / Tuesday / 운영정보 9월 12일 / 9월 13일 중식 / 휴일 / 미역국 불고기 "
               "다래락 구분 / Monday / Tuesday / 라면")
    shown = select_evidence(content, question="9월 13일 미래관 라일락 점심 메뉴", target_chars=60,
                            metadata={"doc_title": "교내 식당 주간 식단표"})
    assert "9월 13일" in shown and "미역국 불고기" in shown
    assert "다래락" not in shown


def test_flattened_regulation_keeps_requested_article_with_its_text():
    content = "제1조(목적) " + "행정 일반 안내입니다. " * 75 + "제2조(정의) 학생설계전공은 학생이 교육과정을 구성합니다."
    shown = select_evidence(content, question="제2조 학생설계전공의 정의", target_chars=70)
    assert "제2조(정의) 학생설계전공은 학생이 교육과정을 구성합니다." in shown


def test_requested_article_keeps_definition_instead_of_later_mention():
    content = ("제1조(목적) 교내 용어 지침이다. "
               "제2조(정의) ‘장표’란 그래프와 표로 구성된 화면이다. "
               "제5조(관리) 관리자는 전자 장표 관리를 담당한다.")
    shown = select_evidence(content, question="용어 관리 지침 제2조에서 장표란 무엇인가요?", target_chars=20,
                            metadata={"doc_title": "용어 관리 지침"})
    assert shown == "제2조(정의) ‘장표’란 그래프와 표로 구성된 화면이다."


def test_named_faculty_relative_rule_keeps_course_guidance_article():
    content = ("제1조(목적) 수강 관리 지침이다. "
               "제3조(수강신청 지도 등) 필수 교과목인 경우에도 타 분반을 선택하도록 지도한다. "
               "제4조(사전신고) 분반이 없는 경우 사전 신고해야 한다.")
    shown = select_evidence(content, question="교수·자녀 수강 공정성 지침에서 부모 강의가 필수과목이면?",
                            target_chars=20, metadata={"doc_title": "교수-자녀 간 강의수강 및 성적평가 공정성 제고 관리 지침"})
    assert shown == "제3조(수강신청 지도 등) 필수 교과목인 경우에도 타 분반을 선택하도록 지도한다."


def test_history_passage_only_keeps_the_event_for_the_requested_date():
    content = ("2025 2025. 9. 1. 이준혁 교수가 전공주임 취임 "
               "2023 2023. 9. 1. 박지영 교수가 전공주임 취임 "
               "2022 2022. 3. 1. 학부 신설")
    shown = select_evidence(content, question="2023년 9월 전공주임은 누구였나요?", target_chars=24,
                            metadata={"doc_title": "연혁"})
    assert shown == "2023. 9. 1. 박지영 교수가 전공주임 취임"


def test_tuition_passage_preserves_the_requested_cohort_in_the_same_row():
    prefix = "정규 등록금: 2026학년도 2학기 / 학부 / 공학계열"
    earlier = prefix + "(미래융합학부) / 학생구분 신입생. 입학금 0원, 수업료 1,000원, 총액 1,000원."
    requested = prefix + " / 학생구분 신입생. 입학금 0원, 수업료 2,000원, 총액 2,000원. 공식 대조 상태: matched."
    later = prefix + " / 학생구분 재학생. 입학금 0원, 수업료 3,000원, 총액 3,000원."
    metadata = {"doc_title": "2026학년도 2학기 등록금 조회 자료"}
    question = "2026학년도 2학기 학부 공학계열 신입생 등록금 총액은 얼마야?"
    shown = select_evidence(" ".join((earlier, requested, later)), question=question, target_chars=20, metadata=metadata)
    assert shown == requested
    assert "3,000원" not in select_evidence(later, question=question, target_chars=20, metadata=metadata)


def test_late_application_passage_keeps_exception_and_required_documents():
    content = ("학교 소개와 일반 안내입니다.\n" * 100
               + "## 복학 신청\n복학 신청은 학사행정정보시스템에서 진행합니다.\n"
               + "다만 군 복무 중인 학생은 전역 예정 증빙이 필요합니다.\n"
               + "제출 서류: 전역증 사본과 복학원.\n"
               + "기타 시설 이용 안내입니다.\n" * 40)
    shown = select_evidence(content, question="복학 신청 방법과 제출 서류?", target_chars=200)
    assert "학사행정정보시스템" in shown
    assert "군 복무 중인 학생" in shown
    assert "전역증 사본과 복학원" in shown
    assert "앞부분 생략" in shown
    assert len(shown) <= 4000


def test_copied_document_title_cannot_displace_the_late_answer():
    content = ("문서제목: 전공필수 과목 확인 방법\n분류: 학사안내\n파일명: notice.json\n본문: "
               + "시설 이용 안내입니다.\n" * 100
               + "전공필수 과목은 학점관리시스템에서 확인합니다.\n"
               + "다만 적용 교육과정은 입학연도와 전공에 따라 다릅니다.\n"
               + "시설 이용 안내입니다.\n" * 100)
    shown = select_evidence(content, question="전공필수 과목 확인 방법", target_chars=120)
    assert "학점관리시스템에서 확인" in shown
    assert "입학연도와 전공" in shown
    assert "파일명: notice.json" not in shown
    assert len(shown) <= 4000


def test_table_selection_repeats_title_columns_and_complete_target_row():
    content = ("일반 안내입니다.\n" * 100 + "## 휴학 유형별 제출 서류\n\n"
               + "| 유형 | 제출 서류 | 인정 기간 |\n| --- | --- | --- |\n"
               + "| 일반휴학 | 휴학원 | 1년 |\n"
               + "| 군입대휴학 | 입영통지서 사본 | 복무 기간 |\n"
               + "| 질병휴학 | 진단서 | 6개월 |\n")
    shown = select_evidence(content, question="군입대휴학 제출 서류?", target_chars=150)
    assert "## 휴학 유형별 제출 서류" in shown
    assert "| 유형 | 제출 서류 | 인정 기간 |" in shown
    assert "| 군입대휴학 | 입영통지서 사본 | 복무 기간 |" in shown


def test_oversized_table_row_is_omitted_whole_instead_of_cutting_a_cell():
    content = "| 대상 | 제출 서류 |\n| --- | --- |\n| 군입대 | " + "증빙설명" * 1500 + "끝값 |"
    shown = select_evidence(content, question="군입대 제출 서류", target_chars=150)
    assert "원문 확인" in shown
    assert "증빙설명" not in shown
    assert len(shown) <= 4000


def test_spreadsheet_keeps_row_column_binding_past_target():
    body = ("시트: 개설강좌 | 원본 행: 17\n교과목명[A]: 자료구조\n담당교수[B]: 김교수\n"
            + "운영 안내 " * 150 + "\n평가 비율[D]: 기말고사 40%")
    table = {"kind": "spreadsheet", "sheet": "개설강좌", "rows": [{"source_row": 17}]}
    shown = select_evidence(body, question="자료구조 평가 비율?", target_chars=100,
                            metadata={"structured_table": table})
    assert shown == body
    assert "교과목명[A]: 자료구조" in shown and "기말고사 40%" in shown


def test_short_submission_checklist_keeps_middle_documents_and_qualifying_notes():
    body = ("제출서류 일람표\n지원서와 지원이유서를 제출합니다. " + "작성 안내입니다. " * 25
            + "\n추천서: 소속 대학의 서명과 직인이 필요합니다.\n성적증명서: 영문으로 제출합니다."
            + "\n외국어능력증명서: 영어 및 일본어 성적 항목이 표기되어 있습니다."
            + "\n재학증명서 및 졸업예정증명서: 영문 원본을 확인합니다.")
    shown = select_evidence(body, question="신청 대상과 제출 서류", target_chars=100)
    assert "추천서: 소속 대학의 서명과 직인" in shown
    assert "성적증명서: 영문" in shown and "졸업예정증명서" in shown
    assert shown == body


def test_reranker_sees_the_late_boarding_passage_instead_of_a_long_document_prefix():
    from app.reranking import _row_text
    row = {"title": "대학생활 안내", "content": "시설 소개입니다.\n" * 200
           + "## 셔틀버스 이용\n출발 기준점은 공학1관 앞과 용당 셔틀버스 정류장입니다. 주말·공휴일에는 운행하지 않습니다."}
    excerpt = _row_text(row, max_chars=500, question="셔틀버스 출발 기준점과 주말 운행")
    assert "공학1관 앞" in excerpt and "주말·공휴일" in excerpt
    assert len(excerpt) <= 500


def test_shuttle_spelling_selects_same_evidence_without_rewriting_source():
    content = "일반 안내입니다.\n" * 40 + "셔틀 버스 안내\n셔틀 버스는 공학1관 앞에서 출발합니다."
    a = select_evidence(content, question="셔틀버스 출발", target_chars=90)
    b = select_evidence(content, question="셔틀 버스 출발", target_chars=90)
    assert a == b
    assert "셔틀 버스는 공학1관 앞에서 출발합니다." in a


def test_faculty_card_rejoins_overlapping_chunks_and_excludes_other_people():
    body = ("다른 교수 명단과 소개 " * 80 + "상세보기 "
            "홍길동 명예교수 이메일 : gildong@example.test 전화번호 : -- 연구분야 : 건축구조 상세보기 "
            "김철수 교수 이메일 : chulsoo@example.test 연구분야 : 도시설계 상세보기")
    boundary = body.index(".test", body.index("홍길동"))
    first = "문서제목: 교수진\n본문: " + body[:boundary]
    second = "문서제목: 교수진\n본문: " + body[boundary - 60:]
    row = {"content": first + "\n" + second, "metadata": {"doc_title": "교수진 및 교직원"},
           "_context_chunks": [{"content": first}, {"content": second}]}

    shown = _source_content(row, 80, "홍길동 명예교수 연구분야를 알려줘")

    assert "홍길동 명예교수" in shown and "건축구조" in shown
    assert "gildong@example.test" in shown
    assert "김철수" not in shown and "도시설계" not in shown


def test_faculty_research_skips_a_truncated_entry_created_by_repeated_overlap():
    overlap = ("건축설계, 주거건축, 한국전통건축문화 상세보기 "
               "홍길동 명예교수 이메일 : gildong@example.")
    first = "문서제목: 교수진\n본문: " + "이전 항목 " * 30 + overlap
    second = "문서제목: 교수진\n본문: " + overlap + " " + overlap + "test 전화번호 : -- 연구분야 : 건축구조 상세보기"
    row = {"content": first + "\n" + second, "metadata": {"doc_title": "교수진"},
           "_context_chunks": [{"content": first}, {"content": second}]}

    shown = _source_content(row, 80, "홍길동 교수 연구 분야가 뭐예요?")

    assert "연구분야 : 건축구조" in shown
    assert "gildong@example.test" in shown
    assert "건축설계" not in shown


def test_zero_budget_and_unbroken_ocr_have_bounded_outputs():
    body = "학교안내" * 2000 + " 복학 제출서류는 전역증 사본입니다."
    assert select_evidence(body, question="복학", target_chars=0) == ""
    shown = select_evidence(body, question="복학 제출서류", target_chars=160)
    assert "전역증 사본" in shown
    assert len(shown) <= 4000


def row(position=4, source="doc", rpc="match_rule_documents", content="복학 신청 안내", **metadata):
    return {"id": f"{source}-{position}", "source_id": source, "chunk_id": position + 1,
            "chunk_index": position, "content": content, "similarity": 0.9,
            "metadata": {"rpc_name": rpc, **metadata}}


class FakeClient:
    def __init__(self, chunks=(), fail=False):
        self.chunks, self.fail, self.calls = list(chunks), fail, []

    def table(self, table):
        client = self

        class Query:
            def __init__(self):
                self.filters = {"table": table}

            def select(self, columns):
                self.filters["columns"] = columns
                return self

            def eq(self, key, value):
                self.filters[key] = value
                return self

            def in_(self, key, values):
                self.filters[key] = list(values)
                return self

            def limit(self, value):
                self.filters["limit"] = value
                return self

            def execute(self):
                client.calls.append(self.filters)
                if client.fail:
                    raise RuntimeError("lookup unavailable")
                # Deliberately return foreign/non-adjacent rows too: helper
                # must not trust a malformed response to satisfy its filters.
                return SimpleNamespace(data=client.chunks)

        return Query()


def test_adjacent_chunks_keep_order_identity_and_original_inputs():
    parent = row(content="복학 신청은 포털에서 합니다.")
    before = row(3, content="## 복학 신청 안내")
    after = row(5, content="다만 전역 예정자는 증빙서류를 제출합니다.")
    client = FakeClient([after, before, row(5, source="other"), row(8)])
    enriched = supplement_neighbors(client, [parent])[0]
    assert enriched["content"] == "\n".join(r["content"] for r in [before, parent, after])
    assert [c["chunk_index"] for c in enriched["_context_chunks"]] == [3, 4, 5]
    assert enriched["id"] == parent["id"] and enriched["similarity"] == parent["similarity"]
    assert parent["content"] == "복학 신청은 포털에서 합니다."
    assert client.calls[0]["source_id"] == "doc"
    assert client.calls[0]["chunk_index"] == [3, 5]
    assert client.calls[0]["table"] == "rule_chunks"
    assert "embedding" not in client.calls[0]["columns"]


def test_scope_rejection_and_lookup_failure_leave_original_evidence():
    parent = row()
    client = FakeClient([row(5, content="대학원생 전용 안내")])
    assert supplement_neighbors(client, [parent], scope_check=lambda p, r: False) == [parent]
    assert supplement_neighbors(FakeClient(fail=True), [parent]) == [parent]


def test_different_spreadsheet_rows_never_share_one_citation():
    def table(n):
        return {"kind": "spreadsheet", "sheet": "강좌", "rows": [{"source_row": n}]}
    parent = row(structured_table=table(10))
    same = row(3, content="같은 강좌의 다음 열", structured_table=table(10))
    other = row(5, content="다른 교수의 강좌", structured_table=table(11))
    enriched = supplement_neighbors(FakeClient([same, other]), [parent])[0]
    assert "같은 강좌의 다음 열" in enriched["content"]
    assert "다른 교수의 강좌" not in enriched["content"]
    # An unlabelled neighbor must not inherit the parent's row number.
    assert supplement_neighbors(FakeClient([row(5)]), [parent]) == [parent]


def test_lookup_budget_disable_and_missing_identity():
    client = FakeClient()
    assert supplement_neighbors(client, [row()], max_sources=0) == [row()]
    supplement_neighbors(client, [row(source=str(i)) for i in range(6)], max_sources=2)
    assert len(client.calls) == 2
    client.calls.clear()
    supplement_neighbors(client, [{"url": "same-page", "content": "정보"}, {**row(), "_syllabus": True}])
    assert not client.calls


def test_prompt_number_check_and_snapshot_share_the_selected_passage():
    content = (("지난 제도 안내 " * 30) + "130학점\n" + "시설 안내입니다.\n" * 300
               + "## 복학 신청 조건\n복학 신청은 18학점 이내입니다.\n"
               + "다만 특정 유형은 12학점까지입니다.\n" + "다른 안내입니다.\n" * 100)
    question = "복학 신청 학점 조건?"
    rows = [row(content=content)]
    shown = _source_content(rows[0], 120, question)
    prompt = build_user_message(question, rows, 120)
    assert shown in prompt and "18학점" in shown and "12학점" in shown
    assert "130학점" in content and "130학점" not in shown
    assert not _number_problems("18학점 이내입니다[1].", rows, 120, question=question)
    assert _number_problems("130학점입니다[1].", rows, 120, question=question)
    assert _number_problems("150학점입니다[1].", rows, 120, question=question + " 150학점?")
    state = SimpleNamespace(system_prompt="system", settings=SimpleNamespace(max_chars_per_chunk=120))
    plan = QuestionPlan(tasks=[SearchTask(text=question)])
    sources = [Source(title="복학", uri="", content=content, similarity=0.9)]
    capture = evidence_snapshot(state, plan, rows, sources, question=question)
    assert capture["sources"][0]["shown_content"] == shown
    assert capture["prompt_source_block"] == _format_source_block(rows, 120, question)
    assert capture["sources"][0]["content"] == content


def test_snapshot_records_each_supplementary_chunk_and_its_hash():
    rows = supplement_neighbors(FakeClient([row(5, content="제출서류: 전역증 사본")]), [row()])
    state = SimpleNamespace(system_prompt="system", settings=SimpleNamespace(max_chars_per_chunk=200))
    sources = [Source(title="복학", uri="", content=rows[0]["content"], similarity=0.9)]
    capture = evidence_snapshot(state, QuestionPlan(), rows, sources, question="복학 제출서류")
    chunks = capture["sources"][0]["context_chunks"]
    assert [c["chunk_index"] for c in chunks] == [4, 5]
    assert all(c["content_sha256"] == hashlib.sha256(c["content"].encode()).hexdigest() for c in chunks)
    assert "제출서류: 전역증 사본" in capture["sources"][0]["shown_content"]


def test_header_in_previous_chunk_stays_bound_to_selected_table_row():
    header = row(3, content="## 휴학 제출서류\n유형 | 제출 서류 | 인정 기간\n--- | --- | ---\n\n")
    parent = row(4, content="\n일반휴학 | 휴학원 | 1년\n군입대휴학 | 입영통지서 | 복무기간")
    enriched = supplement_neighbors(FakeClient([header]), [parent])[0]
    shown = _source_content(enriched, 100, "군입대휴학 제출 서류")
    assert "## 휴학 제출서류" in shown
    assert "유형 | 제출 서류 | 인정 기간" in shown
    assert "군입대휴학 | 입영통지서 | 복무기간" in shown


def test_labelled_tab_table_preserves_empty_cells_and_column_order():
    content = "시설 안내입니다.\n" * 100 + "휴학 제출서류\n유형\t제출 서류\t비고\n군입대휴학\t입영통지서\t\n일반휴학\t휴학원\t확인필요"
    shown = select_evidence(content, question="군입대휴학 제출 서류", target_chars=80)
    assert "유형\t제출 서류\t비고\n군입대휴학\t입영통지서\t" in shown


def test_real_retrieval_scope_is_reapplied_to_supplementary_evidence():
    from app.routers.ask import _retrieve

    parent = row(content="복학 신청 안내")
    before = row(3, content="복학 제출 서류: 복학원")
    foreign = row(5, content="다른 대학의 복학 신청 절차", applicable_school="부산대학교")
    state = SimpleNamespace(supabase=FakeClient([before, foreign]),
                            settings=SimpleNamespace(rag_top_k=10, rag_context_neighbor_sources=4))
    plan = QuestionPlan(tasks=[SearchTask(text="복학 신청 절차")])
    with patch("app.routers.ask._search_all", return_value=[[parent]]):
        rows, sources = _retrieve(state=state, user_id="test", question="복학 신청 절차", chat_history=[],
                                  sub_questions=plan.queries, plan=plan)
    assert len(rows) == len(sources) == 1
    assert "복학 제출 서류: 복학원" in rows[0]["content"]
    assert "다른 대학의 복학" not in rows[0]["content"]


def test_json_generation_and_streaming_validate_the_late_selected_window():
    content = "시설 안내입니다.\n" * 300 + "복학 신청은 18학점 이내입니다.\n" + "시설 안내입니다.\n" * 100
    options = dict(openai_client=None, model="test", system_prompt="system", question="복학 신청 학점 조건?",
                   rows=[row(content=content)], max_chars_per_chunk=100, timeout=5)
    with patch("app.generation._complete", return_value="18학점 이내입니다[1].") as complete, patch(
        "app.generation._stream_complete", return_value=iter(["18학점 이내입니다[1]."])
    ):
        assert generation.generate_answer(**options) == "18학점 이내입니다[1]."
        events = list(generation.stream_answer(**options))
    assert events[-1] == ("final", "18학점 이내입니다[1].")
    assert not any(kind == "revising" for kind, _ in events)
    assert complete.call_count == 1
    assert "18학점" in complete.call_args.args[2][-1]["content"]
