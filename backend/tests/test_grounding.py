"""grounding: figures an answer states that its sources don't contain."""

import unittest
from types import SimpleNamespace

from app import generation
from app.grounding import miscited_numbers, unsupported_numbers

SOURCES = [
    "졸업하려면 몇 학점?",
    "직전 2개 학기 성적평점평균 3.5 이상. 선발 인원 2배수 이하인 경우 재공고. 장학금액: 2,000,000원",
]


class UnsupportedNumbersTests(unittest.TestCase):
    def test_invented_credit_total_is_flagged(self):
        self.assertEqual(["130학점"], unsupported_numbers("졸업하려면 **총 130학점 이상**을 이수해야 합니다[1].", SOURCES))

    def test_figures_written_differently_from_the_source_pass(self):
        answer = "**3.50 이상**이어야 하고, 2배수 이하면 재공고하며 장학금은 2000000원입니다[1][2]."
        self.assertEqual([], unsupported_numbers(answer, SOURCES))

    def test_list_markers_citations_and_dates_are_not_claims(self):
        self.assertEqual([], unsupported_numbers("1. 신청: 2026년 10월 27일까지 [3]\n2. 문의", SOURCES))


class MiscitedNumbersTests(unittest.TestCase):
    SOURCES = ["교직과목 50학점 이상 이수", "졸업 소요학점: 컴퓨터공학 140학점"]

    def test_figure_borrowed_from_another_source_is_flagged(self):
        self.assertEqual(["50학점"], miscited_numbers("졸업하려면 50학점이 필요합니다[2].", "졸업 학점?", self.SOURCES))

    def test_figure_in_the_cited_source_passes(self):
        self.assertEqual([], miscited_numbers("졸업 학점은 140학점입니다[2].\n교직은 50학점[1].", "q", self.SOURCES))

    def test_uncited_lines_and_figures_in_no_source_are_left_to_the_other_check(self):
        self.assertEqual([], miscited_numbers("총 50학점입니다.\n총 130학점입니다[1].", "q", self.SOURCES))


class FakeClient:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **kwargs):
        self.calls.append(kwargs["messages"])
        message = SimpleNamespace(content=self.answers.pop(0))
        return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=None)


def _answer(client):
    return generation.generate_answer(
        openai_client=client, model="m", system_prompt="s", question="졸업하려면 몇 학점?",
        rows=[{"content": SOURCES[1]}], max_chars_per_chunk=1000, timeout=5,
    )


class GenerateAnswerRetryTests(unittest.TestCase):
    def test_answer_with_an_invented_figure_is_rewritten_once(self):
        client = FakeClient("총 **130학점**입니다.", "졸업 학점은 학과별로 다릅니다.")
        self.assertEqual("졸업 학점은 학과별로 다릅니다.", _answer(client))
        self.assertEqual(2, len(client.calls))
        self.assertIn("130학점", client.calls[1][-1]["content"])

    def test_answer_citing_the_wrong_source_is_rewritten_once(self):
        rows = [{"content": "교직과목 50학점 이상"}, {"content": "졸업 소요학점 140학점"}]
        client = FakeClient("졸업에는 50학점이 필요합니다[2].", "졸업에는 140학점이 필요합니다[2].")
        answer = generation.generate_answer(
            openai_client=client, model="m", system_prompt="s", question="졸업 학점?",
            rows=rows, max_chars_per_chunk=1000, timeout=5,
        )
        self.assertEqual("졸업에는 140학점이 필요합니다[2].", answer)
        self.assertIn("다른 자료에 있습니다", client.calls[1][-1]["content"])

    def test_grounded_answer_is_returned_as_is(self):
        client = FakeClient("**3.50 이상**입니다[1].")
        self.assertEqual("**3.50 이상**입니다[1].", _answer(client))
        self.assertEqual(1, len(client.calls))


if __name__ == "__main__":
    unittest.main()
