"""Keyword-search query: every word is AND-ed, so question-framing words must go."""

import unittest

from app.retrieval import _lexical_fallback_query, _lexical_query_text, _query_term_groups


def lexical(question):
    return _lexical_query_text(question, _query_term_groups(question))


class LexicalFallbackTests(unittest.TestCase):
    def test_graduation_credits_do_not_search_all_credit_bearing_documents(self):
        for question in ("졸업하려면 총 몇 학점?", "2024학년도 입학생 컴공 졸업학점 기준", "컴퓨터·인공지능공학부 졸업요건"):
            with self.subTest(question=question):
                query = lexical(question)
                self.assertIn("졸업소요학점", query)
                self.assertNotIn("학점", query.split(" OR "))
                self.assertNotIn("컴퓨터·인공지능공학부", query.split(" OR "))
                self.assertNotIn("졸업작품", query)

    def test_degree_deferral_does_not_expand_to_all_graduation_documents(self):
        for question in ("졸업유예 신청 기간", "학사학위취득유예 신청"):
            query = lexical(question)
            self.assertIn("학사학위취득유예", query)
            self.assertNotIn("졸업논문", query)
            self.assertNotIn("졸업작품", query)

    def test_department_contact_requires_both_the_department_and_office_words(self):
        query = lexical("컴퓨터·인공지능공학부 사무실 연락처")
        for branch in query.split(" OR "):
            self.assertEqual(4, branch.count('"'))
            self.assertTrue(any(name in branch for name in ("컴퓨터", "인공지능")))
        self.assertIn('"학부사무실"', query)

    def test_leave_procedure_uses_application_and_form_terms_without_bare_leave(self):
        query = lexical("휴학 신청 방법과 신청 마감일")
        self.assertIn("휴학신청", query)
        self.assertIn("휴학원", query)
        self.assertNotIn("휴학", query.split(" OR "))
        self.assertNotIn('"휴학 신청"', query)
        self.assertNotIn('"휴학 및 복학"', query)

    def test_shuttle_topic_does_not_require_campus_names_with_inflected_suffixes(self):
        query = lexical("대연캠퍼스에서 용당캠퍼스로 가는 셔틀버스 이용 방법")
        self.assertIn("셔틀버스", query)
        self.assertNotIn("대연캠퍼스", query)
        self.assertNotIn("용당캠퍼스", query)

    def test_explicit_regular_registration_does_not_search_other_stages(self):
        expected = '본수강신청 "2025학년도 1학기" OR 본수강신청 "2025 1학기"'
        self.assertEqual(lexical("2025학년도 1학기 학부 본수강신청 기간"), expected)
        self.assertEqual(lexical("2025년 1학기 본 수강 신청은 며칠까지였어?"), expected)
        self.assertIn('"2025학년도 2학기"', lexical("2025년 2학기 본수강신청 기간"))
        self.assertIn("수강신청", lexical("본수강신청 기간").split(" OR "))
        self.assertIn("수강신청", lexical("수강신청 기간").split(" OR "))

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
