"""Keyword-search query: every word is AND-ed, so question-framing words must go."""

import unittest

from app.retrieval import _lexical_fallback_query, _lexical_query_text, _query_term_groups


def lexical(question):
    return _lexical_query_text(question, _query_term_groups(question))


class LexicalFallbackTests(unittest.TestCase):
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
