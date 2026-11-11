from __future__ import annotations

import unittest

from app.generation import build_user_message, load_system_prompt


class GenerationPromptTests(unittest.TestCase):
    def test_prompt_prefers_partial_grounded_answer_over_no_info(self) -> None:
        message = build_user_message(
            question="장학금 신청 방법을 알려줘",
            rows=[
                {
                    "content": "장학금은 학생포털에서 신청합니다.",
                    "metadata": {"doc_title": "장학금 안내"},
                    "similarity": 0.42,
                }
            ],
            max_chars_per_chunk=500,
        )

        self.assertIn("확인 가능한 범위까지 답하세요", message)
        self.assertIn("핵심에 답할 근거가 참고 자료에 전혀 없을 때만", message)

    def test_system_prompt_uses_no_info_only_when_no_core_evidence_exists(self) -> None:
        prompt = load_system_prompt()

        self.assertIn("관련된 내용이 하나라도 있으면", prompt)
        self.assertIn("핵심에 답할 근거가 참고 자료에 전혀 없을 때만", prompt)

    def test_comparison_and_missing_subquery_are_explicit_in_generation_context(self) -> None:
        message = build_user_message(
            question="두 전공의 전공필수가 같은가요?",
            rows=[{"content": "인공지능전공 교육과정 일부", "metadata": {
                "comparison_targets": ["인공지능전공", "컴퓨터공학전공"],
                "request_coverage": [
                    {"question": "인공지능전공 전공필수", "status": "candidates_selected"},
                    {"question": "컴퓨터공학전공 전공필수", "status": "no_candidates"},
                ],
            }}],
            max_chars_per_chunk=500,
        )
        self.assertIn("인공지능전공", message)
        self.assertIn("컴퓨터공학전공", message)
        self.assertIn("한쪽 근거만 있으면", message)
        self.assertIn('"status": "no_candidates"', message)
        self.assertIn("자료 자체가 존재하지 않는다고 단정하지 마세요", message)


if __name__ == "__main__":
    unittest.main()
