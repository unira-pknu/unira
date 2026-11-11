"""Keyword-search query: every word is AND-ed, so question-framing words must go."""

import unittest

from app.query_transform import transform_query
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


class DepartmentScopeTests(unittest.TestCase):
    # 2026-11-11: "컴퓨터·인공지능공학부" alone matched 21,077 chunks and timed out;
    # AND-ed with the topic it is 313 chunks.
    NAMES = ("컴공", "컴퓨터·인공지능공학부", "컴퓨터인공지능공학부")

    def test_department_is_and_ed_with_the_topic(self):
        expected = " OR ".join(f"{name} 사무실" for name in self.NAMES)
        self.assertEqual(lexical("컴퓨터공학과 사무실이 어디야?"), expected)
        self.assertEqual(lexical("컴퓨터·인공지능공학부 사무실 위치"), expected)

    def test_honorific_and_list_words_go_next_to_the_department(self):
        self.assertEqual(lexical("컴퓨터공학과 교수님 목록"), " OR ".join(f"{name} 교수" for name in self.NAMES))

    def test_department_is_and_ed_with_each_topic_variant(self):
        query = lexical("컴공 졸업요건")
        self.assertIn("컴공 졸업요건", query)
        self.assertIn("컴퓨터·인공지능공학부 학위수여", query)
        self.assertNotRegex(query, r"(^| OR )(컴공|컴퓨터·인공지능공학부|컴퓨터인공지능공학부)( OR |$)")

    def test_department_alone_skips_the_keyword_search(self):
        self.assertEqual(lexical("컴공"), "")
        self.assertEqual(lexical("컴공 위치"), "")

    def test_list_words_stay_without_a_department(self):
        # Bare "교수" matches 112k chunks; keep the narrower AND.
        self.assertEqual(lexical("교수님 목록"), "교수님 목록")


class PortalTermTests(unittest.TestCase):
    def test_portal_searches_the_system_name_and_address(self):
        # 2026-11-11: "부경대 포털 주소" matched neither "포털시스템" nor the URL token.
        query = lexical("부경대학교 포털 주소")
        self.assertIn("포털시스템", query)
        self.assertIn("portal.pknu.ac.kr", query)
        self.assertNotRegex(query, r"(^| OR )포털( OR |$)")


class CreditExchangeTermTests(unittest.TestCase):
    def test_credit_exchange_is_not_a_credits_question(self):
        # 2026-11-10: "학점교류" searched "학점 OR 소요학점 OR 이수학점" and timed out.
        self.assertNotIn("학점", [term for term, _ in _query_term_groups("충북대학교 학점교류 신청 방법")])
        self.assertEqual(lexical("충북대학교 학점교류 신청 방법"), "충북대학교 학점교류 신청")

    def test_a_real_credits_question_about_exchange_still_counts(self):
        self.assertIn("학점", [term for term, _ in _query_term_groups("학점교류 학점 인정 몇 학점까지")])


class DormitoryTermTests(unittest.TestCase):
    def test_dormitory_searches_the_school_word(self):
        # 2026-11-06: "기숙사" alone ranked exchange notices about other universities' dorms first.
        query = transform_query("기숙사 신청은 어떻게 해?")
        self.assertIn("학생생활관", query)
        self.assertEqual(lexical(query), "학생생활관")
        self.assertIn("생활관", [term for term, _ in _query_term_groups(query)])


if __name__ == "__main__":
    unittest.main()
