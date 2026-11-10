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
        self.assertEqual(["50학점"], miscited_numbers("졸업하려면 50학점이 필요합니다[2].", self.SOURCES))

    def test_figure_in_the_cited_source_passes(self):
        self.assertEqual([], miscited_numbers("졸업 학점은 140학점입니다[2].\n교직은 50학점[1].", self.SOURCES))

    def test_uncited_lines_and_figures_in_no_source_are_left_to_the_other_check(self):
        self.assertEqual([], miscited_numbers("총 50학점입니다.\n총 130학점입니다[1].", self.SOURCES))


class NumberEvidenceTests(unittest.TestCase):
    def test_only_the_source_body_is_evidence(self):
        rows = [{"content": "졸업 요건은 학과별로 다릅니다.", "metadata": {"doc_title": "130학점 안내"}}]
        self.assertTrue(generation._number_problems("총 130학점입니다[1].", rows, 1000))
        self.assertTrue(generation._number_problems("총 130학점입니다.", [], 1000))

    def test_validation_uses_the_same_content_window_as_the_prompt(self):
        cases = [
            ({"content": "가" * 600 + " 총 130학점"}, True),
            ({"content": "가" * 600 + " 총 130학점", "_syllabus": True}, False),
            ({"content": "가" * 4100 + " 총 130학점", "_syllabus": True}, True),
        ]
        for row, should_fail in cases:
            with self.subTest(syllabus=row.get("_syllabus"), content_length=len(row["content"])):
                prompt = generation.build_user_message("졸업 요건?", [row], 500)
                self.assertEqual(not should_fail, "130학점" in prompt)
                self.assertEqual(should_fail, bool(generation._number_problems("총 130학점입니다[1].", [row], 500)))


class FakeClient:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **kwargs):
        self.calls.append(kwargs["messages"])
        message = SimpleNamespace(content=self.answers.pop(0))
        return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=None)


def _answer(client, **overrides):
    options = dict(
        openai_client=client, model="m", system_prompt="s", question="졸업하려면 몇 학점?",
        rows=[{"content": SOURCES[1]}], max_chars_per_chunk=1000, timeout=5,
    )
    options.update(overrides)
    return generation.generate_answer(**options)


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

    def test_unsupported_question_or_history_figure_is_not_evidence(self):
        contexts = [
            {"question": "졸업하려면 130학점이면 되나요?"},
            {"chat_history": [{"role": "assistant", "content": "총 130학점입니다."}]},
        ]
        for context in contexts:
            with self.subTest(context=context):
                client = FakeClient("총 130학점입니다[1].", "졸업 학점은 자료에서 확인되지 않습니다.")
                answer = _answer(client, **context)
                self.assertEqual("졸업 학점은 자료에서 확인되지 않습니다.", answer)
                self.assertEqual(2, len(client.calls))
                self.assertIn("130학점", client.calls[1][-1]["content"])

    def test_question_figure_does_not_excuse_the_wrong_citation(self):
        client = FakeClient("졸업에는 50학점이 필요합니다[2].", "졸업에는 140학점이 필요합니다[2].")
        answer = _answer(
            client, question="졸업하려면 50학점이면 되나요?",
            rows=[{"content": "교직과목 50학점 이상"}, {"content": "졸업 소요학점 140학점"}],
        )
        self.assertEqual("졸업에는 140학점이 필요합니다[2].", answer)
        self.assertEqual(2, len(client.calls))
        self.assertIn("다른 자료에 있습니다", client.calls[1][-1]["content"])

    def test_question_figure_also_supported_by_the_source_passes(self):
        client = FakeClient("졸업에는 140학점이 필요합니다[1].")
        answer = _answer(client, question="140학점이면 졸업하나요?", rows=[{"content": "졸업 소요학점 140학점"}])
        self.assertEqual("졸업에는 140학점이 필요합니다[1].", answer)
        self.assertEqual(1, len(client.calls))

    def test_revision_with_repeated_or_new_unsupported_figure_falls_back(self):
        for revision in ("총 130학점입니다[1].", "총 150학점입니다[1]."):
            with self.subTest(revision=revision):
                client = FakeClient("총 130학점입니다[1].", revision)
                self.assertEqual("관련 정보를 찾을 수 없습니다.", _answer(client, question="130학점이면 되나요?"))
                self.assertEqual(2, len(client.calls))

    def test_revision_with_a_new_miscitation_falls_back(self):
        client = FakeClient("총 130학점입니다[2].", "총 50학점입니다[2].")
        answer = _answer(
            client, question="졸업하려면 50학점인가요?",
            rows=[{"content": "교직과목 50학점 이상"}, {"content": "졸업 소요학점 140학점"}],
        )
        self.assertEqual("관련 정보를 찾을 수 없습니다.", answer)
        self.assertEqual(2, len(client.calls))


class StreamAnswerTests(unittest.TestCase):
    def client(self, streamed, retry=None):
        calls = []

        def create(**kwargs):
            calls.append(kwargs)
            if kwargs.get("stream"):
                return iter(
                    SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=piece))]) for piece in streamed
                )
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=retry))], usage=None)

        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))), calls

    def run_stream(self, client, **overrides):
        options = dict(
            openai_client=client, model="m", system_prompt="s", question="졸업하려면 몇 학점?",
            rows=[{"content": SOURCES[1]}], max_chars_per_chunk=1000, timeout=5,
        )
        options.update(overrides)
        return list(generation.stream_answer(**options))

    def test_grounded_answer_streams_and_ends_with_final(self):
        client, calls = self.client(["**3.50 ", "이상**입니다[1]."])
        self.assertEqual(
            [("token", "**3.50 "), ("token", "이상**입니다[1]."), ("final", "**3.50 이상**입니다[1].")],
            self.run_stream(client),
        )
        self.assertEqual(1, len(calls))

    def test_invented_figure_is_rewritten_after_streaming(self):
        client, calls = self.client(["총 **130", "학점**입니다."], retry="졸업 학점은 학과별로 다릅니다.")
        events = self.run_stream(client)
        self.assertEqual(["token", "token", "revising", "replace", "final"], [kind for kind, _ in events])
        self.assertEqual("졸업 학점은 학과별로 다릅니다.", events[-1][1])
        self.assertIn("130학점", calls[1]["messages"][-1]["content"])

    def test_question_figure_is_checked_after_streaming(self):
        client, calls = self.client(["총 130학점입니다[1]."], retry="졸업 학점은 자료에서 확인되지 않습니다.")
        events = self.run_stream(client, question="졸업하려면 130학점이면 되나요?")
        self.assertEqual(["token", "revising", "replace", "final"], [kind for kind, _ in events])
        self.assertEqual(("replace", "졸업 학점은 자료에서 확인되지 않습니다."), events[-2])
        self.assertEqual(("final", events[-2][1]), events[-1])
        self.assertEqual(2, len(calls))

    def test_question_figure_does_not_excuse_streamed_miscitation(self):
        client, calls = self.client(["졸업에는 50학점이 필요합니다[2]."], retry="졸업에는 140학점이 필요합니다[2].")
        events = self.run_stream(
            client, question="졸업하려면 50학점이면 되나요?",
            rows=[{"content": "교직과목 50학점 이상"}, {"content": "졸업 소요학점 140학점"}],
        )
        self.assertEqual(["token", "revising", "replace", "final"], [kind for kind, _ in events])
        self.assertEqual(("final", "졸업에는 140학점이 필요합니다[2]."), events[-1])
        self.assertIn("다른 자료에 있습니다", calls[1]["messages"][-1]["content"])

    def test_failed_revision_is_replaced_with_fallback_before_final(self):
        rows = [{"content": "교직과목 50학점 이상"}, {"content": "졸업 소요학점 140학점"}]
        for revision in ("총 130학점입니다[2].", "총 150학점입니다[2].", "총 50학점입니다[2]."):
            with self.subTest(revision=revision):
                client, calls = self.client(["총 130학점입니다[2]."], retry=revision)
                events = self.run_stream(client, question="졸업하려면 130학점인가요?", rows=rows)
                self.assertEqual(
                    [("token", "총 130학점입니다[2]."), ("revising", ""),
                     ("replace", "관련 정보를 찾을 수 없습니다."), ("final", "관련 정보를 찾을 수 없습니다.")],
                    events,
                )
                self.assertEqual(2, len(calls))


if __name__ == "__main__":
    unittest.main()
