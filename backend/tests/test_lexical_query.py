"""Keyword-search query: every word is AND-ed, so question-framing words must go."""

import unittest

from app.retrieval import _lexical_fallback_query, _lexical_query_text, _query_term_groups


def lexical(question):
    return _lexical_query_text(question, _query_term_groups(question))


class LexicalFallbackTests(unittest.TestCase):
    def test_named_exchange_document_uses_literal_institution_and_academic_year(self):
        question = "<2026학년도 1학기 타 대학 교류 수학 안내(연세대학교)> / 1. [연세대학교] 2026학년도 1학기 국내교환학생(타대생)을 위한 학사안내 내용을 알려줘"
        self.assertEqual(lexical(question), "연세대학교 2026학년도")

    def test_academic_year_suffix_is_not_removed_as_a_particle(self):
        self.assertIn("2026학년도", _lexical_fallback_query("2026학년도 강의편람 내용"))

    def test_notice_label_before_academic_year_does_not_strip_its_suffix(self):
        for prefix in ("[필독]", "[공지]"):
            with self.subTest(prefix=prefix):
                query = _lexical_fallback_query(prefix + "2026학년도 강의편람 내용")
                self.assertIn("2026학년도", query)

    def test_multiple_institutions_are_not_arbitrarily_reduced_to_one(self):
        query = _lexical_fallback_query("2026학년도 연세대학교 고려대학교 교류 안내")
        self.assertIn("연세대학교", query)
        self.assertIn("고려대학교", query)

    def test_question_words_are_dropped_from_the_topic(self):
        # 2026-11-03: "학사경고 기준" matched 0 chunks; "학사경고" alone found the guideline.
        self.assertEqual(lexical("학사경고 기준"), "학사경고")
        self.assertEqual(lexical("학사경고 받는 기준이랑 받으면 어떻게 돼?"), "학사경고")
        self.assertEqual(lexical("학생증 재발급 방법"), "학생증 재발급")

    def test_question_words_stay_when_nothing_specific_is_left(self):
        # A bare common word would scan most of the table; keep the AND narrower.
        self.assertEqual(lexical("기준"), "기준")
        self.assertEqual(_lexical_fallback_query("졸업 기준"), "졸업 기준")


if __name__ == "__main__":
    unittest.main()
