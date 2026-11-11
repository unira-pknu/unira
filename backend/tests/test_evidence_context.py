"""Evidence survives late passages and table boundaries without live services."""

import hashlib
from types import SimpleNamespace
from unittest.mock import patch

from app import generation
from app.evidence_context import select_evidence, supplement_neighbors
from app.evaluation import evidence_snapshot
from app.generation import _format_source_block, _number_problems, _source_content, build_user_message
from app.query_plan import QuestionPlan, SearchTask
from app.schemas import Source


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
