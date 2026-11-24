"""Real answer failures: department credit generalization and expired periods."""
from datetime import date
from types import SimpleNamespace

import pytest

from app.answer_scope import scope_problems
from app import generation

TODAY = date(2026, 11, 22)
ROWS = [{"content": "데이터정보과학부 졸업소요학점 132학점", "metadata": {
    "department_id": "bigdata", "department_name": "데이터정보과학부"}}]


def client_with_answers(first, revised):
    calls = []
    def create(**kwargs):
        calls.append(kwargs)
        if kwargs.get("stream"):
            return iter([SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=first))])])
        content = first if len(calls) == 1 else revised
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))], usage=None)
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))), calls


@pytest.mark.parametrize("answer", [
    "졸업하려면 총 132학점 이상을 이수해야 합니다[1].\n학과별로 다를 수 있습니다.",
    "졸업하려면 총 132학점 이상을 이수해야 합니다. 이 학점은 학과 기준입니다[1].\n학과별로 다를 수 있습니다.",
])
def test_other_departments_credits_are_not_a_universal_total(answer):
    assert scope_problems(answer, ROWS, "졸업하려면 몇 학점인가요?", today=TODAY)


@pytest.mark.parametrize("answer,question,rows", [
    ("데이터정보과학부의 예시는 132학점입니다[1].", "졸업학점?", ROWS),
    ("132학점입니다[1].", "데이터정보과학부 졸업학점?", ROWS),
    ("132학점입니다[1].", "졸업학점?", [{"content": "공통규정 졸업 132학점"}]),
    ("장학금 신청은 12학점 이수자입니다[1].", "장학 신청요건?", ROWS),
])
def test_qualified_examples_named_departments_and_common_sources_are_preserved(answer, question, rows):
    assert not scope_problems(answer, rows, question, today=TODAY)


@pytest.mark.parametrize("period", ["2026년 1월 21일(수)부터 1월 27일(화)까지", "2026. 1. 21. ~ 1. 27."])
def test_expired_application_range_cannot_be_presented_as_available(period):
    assert scope_problems(f"졸업유예 신청은 {period} 가능합니다[1].", [], "졸업유예 언제 신청하나요?", today=TODAY)


def test_expired_period_is_not_a_current_deadline_even_with_a_generic_latest_notice_warning():
    answer = "졸업유예 신청 기간은 2026년 1월 21일부터 1월 27일까지입니다. 최신 공지를 확인하세요."
    assert scope_problems(answer, [], "졸업유예 언제 신청하나요?", today=TODAY)


@pytest.mark.parametrize("answer", [
    "2026년 1월 21일부터 1월 27일까지였으며 이미 종료된 과거 신청 기간입니다[1].",
    "신청은 2026년 11월 20일부터 11월 30일까지 가능합니다[1].",
    "신청은 2026년 12월 20일부터 1월 30일까지 가능합니다[1].",  # crosses years
    "신청은 2026년 2월 30일부터 3월 3일까지 가능합니다[1].",  # invalid date is not guessed
    "신청은 1월 21일부터 1월 27일까지 가능합니다[1].",  # unknown year
    "2026년 1월 21일부터 1월 27일까지인 신청 기간이 지나 현재 신청은 불가능합니다[1].",
])
def test_historical_ongoing_future_or_ambiguous_ranges_are_not_reclassified(answer):
    assert not scope_problems(answer, [], "신청 기간", today=TODAY)


@pytest.mark.parametrize("body", ["2025.04.21. 개정 졸업 소요 132학점", "2025학년도 이후 입학생 졸업 소요 132학점", "전공별 졸업학점 132학점"])
def test_posting_or_amendment_year_cannot_verify_a_requested_admission_cohort(body):
    assert scope_problems("총 132학점입니다[1].", [{"content": body}], "2024학년도 입학생 컴공 졸업학점", today=TODAY)


@pytest.mark.parametrize("body", ["2024학년도 입학생 졸업 소요 132학점", "24학번 졸업 소요 132학점", "2022학년도 이후 입학생 졸업 소요 132학점", "2020~2025학년도 입학생 졸업 소요 132학점"])
def test_explicit_cohort_or_covering_range_is_preserved(body):
    assert not scope_problems("총 132학점입니다[1].", [{"content": body}], "2024학년도 입학생 컴공 졸업학점", today=TODAY)


def test_cohort_from_one_guide_cannot_validate_credits_in_another_documents_table():
    rows = [{"content": "교양교과목: 22학번 이후 입학연도별 교육과정 적용"},
            {"content": "2025학년도 이후 입학생 컴퓨터공학전공 졸업 소요 132학점"}]
    assert scope_problems("2024학년도 입학생 졸업학점은 132학점입니다[1][2].", rows,
                          "2024학년도 입학생 컴공 졸업학점", today=TODAY)


def test_generalized_credits_are_repaired_in_the_existing_bounded_pass():
    client, calls = client_with_answers("졸업에는 총 132학점이 필요합니다[1].", "학과별 기준이 다릅니다. 데이터정보과학부는 132학점입니다[1]. 소속 전공과 입학연도를 알려주세요.")
    answer = generation.generate_answer(openai_client=client, model="m", system_prompt="s",
        question="졸업학점 기준", rows=ROWS, max_chars_per_chunk=500, timeout=5)
    assert "데이터정보과학부" in answer and len(calls) == 2
    assert "전체 학생 기준" in calls[1]["messages"][-1]["content"]


def test_stream_replaces_a_generalized_credit_answer_with_the_validated_revision():
    revision = "데이터정보과학부의 예시는 132학점입니다[1]. 소속 학과를 알려주세요."
    client, calls = client_with_answers("졸업에는 총 132학점이 필요합니다[1].", revision)
    events = list(generation.stream_answer(openai_client=client, model="m", system_prompt="s",
        question="졸업학점 기준", rows=ROWS, max_chars_per_chunk=500, timeout=5))
    assert ("replace", revision) in events and events[-1] == ("final", revision)
    assert len(calls) == 2


def test_repeated_cohort_error_returns_the_missing_condition_without_inventing_credits():
    bad = "2024학년도 입학생 졸업학점은 132학점입니다[1]."
    client, calls = client_with_answers(bad, bad)
    answer = generation.generate_answer(openai_client=client, model="m", system_prompt="s",
        question="2024학년도 입학생 컴공 졸업학점", rows=ROWS, max_chars_per_chunk=500, timeout=5)
    assert "2024학년도 입학생" in answer and "확정할 수 없습니다" in answer
    assert "132학점" not in answer and len(calls) == 2


def test_cohort_repair_with_a_new_invented_total_still_explains_the_unresolved_cohort():
    client, calls = client_with_answers("졸업에는 총 132학점이 필요합니다[1].", "졸업에는 총 150학점이 필요합니다[1].")
    answer = generation.generate_answer(openai_client=client, model="m", system_prompt="s",
        question="2024학년도 입학생 컴공 졸업학점", rows=ROWS, max_chars_per_chunk=500, timeout=5)
    assert "2024학년도 입학생" in answer and "확정할 수 없습니다" in answer
    assert "150학점" not in answer and answer != "관련 정보를 찾을 수 없습니다."
    assert len(calls) == 2


def test_past_period_is_repaired_once_using_the_same_clock_as_the_prompt(monkeypatch):
    monkeypatch.setattr(generation, "today_kst", lambda: TODAY)
    client, calls = client_with_answers("신청은 2026년 1월 21일부터 1월 27일까지 가능합니다[1].",
                                      "2026년 1월 21일부터 1월 27일까지였으며 이미 종료된 과거 신청 기간입니다[1].")
    answer = generation.generate_answer(openai_client=client, model="m", system_prompt="s",
        question="신청 기간", rows=[{"content": "신청기간: 2026. 1. 21. ~ 1. 27."}],
        max_chars_per_chunk=500, timeout=5)
    assert "종료된 과거" in answer and len(calls) == 2
