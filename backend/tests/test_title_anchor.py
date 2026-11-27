"""Title-anchor selection: rare title words pick this term's notice."""

import unittest

from app import title_anchor
from app.retrieval import _LEXICAL_PARTICLE_SUFFIXES, _LEXICAL_STOPWORDS


def terms(question):
    return title_anchor.anchor_terms(question, _LEXICAL_STOPWORDS, _LEXICAL_PARTICLE_SUFFIXES)


def rows(title, url, count=1):
    return [{"title": title, "url": url}] * count


class AnchorTermsTests(unittest.TestCase):
    def test_spoken_and_admin_words_are_not_anchors(self):
        self.assertEqual(terms("정효택 장학금은 신청자가 적으면 어떻게 돼?"), ["정효택", "장학금"])
        self.assertEqual(terms("우리 학부 외국인 유학생 멘토를 하면 장학금 얼마 받아?"),
                         ["외국인", "유학생", "멘토", "장학금"])

    def test_ascii_names_kept_and_bare_years_dropped(self):
        self.assertEqual(terms("CES 2027 견학 신청서"), ["CES", "견학"])


class RankNoticesTests(unittest.TestCase):
    def test_yearless_monthly_notice_beats_other_months_and_other_years(self):
        phrase = "모의토익"
        term_rows = {phrase: [
            {"title": "6월 모의 토익시험 고사장 안내", "url": "https://x/target", "published_at": "2023-06-29"},
            {"title": "8월 모의토익 접수 안내", "url": "https://x/aug", "published_at": "2023-08-03"},
            {"title": "2026년 6월 모의토익 안내", "url": "https://x/later", "published_at": "2026-06-01"},
        ]}
        self.assertEqual(title_anchor.notice_title_patterns("6월 모의 토익 고사장"),
                         {phrase: "%모의%토익%"})
        self.assertEqual(title_anchor.rank_notices(term_rows, title_year=2023, event_month=(2023, 6),
                                                   phrase_terms=frozenset({phrase}))[0], "https://x/target")

    def test_explicit_publication_year_does_not_get_replaced_by_the_admission_year(self):
        notices = {"해양대학": [
            {"title": "해양대학 2027년 입학생 모집", "url": "https://x/new", "published_at": "2026-11-01"},
            {"title": "해양대학 2026년 입학생 모집", "url": "https://x/requested", "published_at": "2025-11-05"},
            {"title": "해양대학 2026년 입학생 모집", "url": "https://x/requested"},
        ]}
        self.assertEqual(title_anchor.rank_notices(notices, published_year=2025), ["https://x/requested"])

    def test_newest_term_wins_among_equal_matches(self):
        term_rows = {"정효택": (
            rows("2025-2학기 [발전기금]정효택 장학생 모집 안내", "https://ce.pknu.ac.kr/ce/1814?action=view&no=9970000")
            + rows("2026-2학기 [발전기금]정효택 장학생 모집 안내", "https://ce.pknu.ac.kr/ce/1814?action=view&no=9995302")
            + rows("2026-1학기 [발전기금]정효택 장학생 모집 안내", "https://ce.pknu.ac.kr/ce/1814?action=view&no=9986758")
        )}
        self.assertEqual(title_anchor.rank_notices(term_rows)[0],
                         "https://ce.pknu.ac.kr/ce/1814?action=view&no=9995302")

    def test_common_terms_are_ignored(self):
        many = [{"title": f"장학금 안내 {i}", "url": f"https://x/{i}"} for i in range(title_anchor.MAX_TERM_DF + 1)]
        self.assertEqual(title_anchor.rank_notices({"장학금": many}), [])

    def test_more_rare_terms_beat_one(self):
        term_rows = {
            "CES": rows("CES 2026 참관", "https://x/1") + rows("CES 2027 기술견학", "https://x/2"),
            "기술견학": rows("CES 2027 기술견학", "https://x/2"),
        }
        self.assertEqual(title_anchor.rank_notices(term_rows)[0], "https://x/2")

    def test_posting_date_dates_a_yearless_title(self):
        # 2026-10-24: the 2026-10 notice lost to a 2026-05 "2027년 ..." one
        # (bare title year read as 2027) before posting dates counted.
        target = "https://ce.pknu.ac.kr/ce/1814?action=view&no=9995378"
        term_rows = {"지역인재": [
            {"title": "★ 2027년 지역인재 7급 공무원 선발시험 준비를 위한 동아리 모집 안내 ★",
             "url": "https://www.pknu.ac.kr/main/163?action=view&no=724926", "date": "2026-05-26"},
            {"title": "2026년 지역인재7급 수습직원 선발시험 설명회 개최 안내",
             "url": "https://ce.pknu.ac.kr/ce/1814?action=view&no=9973092"},
            {"title": "전국 지역인재 7급 수습직원 선발시험 향후 변경사항 안내", "url": target,
             "published_at": "2026-10-06T00:00:00+09:00"},
            # The same notice's attachment row has no date; the body's counts.
            {"title": "전국 지역인재 7급 수습직원 선발시험 향후 변경사항 안내", "url": target},
        ]}
        self.assertEqual(title_anchor.rank_notices(term_rows)[0], target)


class ChunkPickTests(unittest.TestCase):
    def test_chunk_terms_keep_admin_words_but_drop_title_words(self):
        words = title_anchor.chunk_terms(
            "CES 2027 해외 기술견학 프로그램 지원 자격은 무엇인가요",
            "[모집] 해외SW기술견학 INNO-TRACK CES 2027 프로그램(~10/27)",
            _LEXICAL_STOPWORDS, _LEXICAL_PARTICLE_SUFFIXES,
        )
        self.assertIn("자격", words)
        self.assertNotIn("CES", words)
        self.assertNotIn("프로그램", words)

    def test_keyword_hits_skip_header_and_match_stems(self):
        chunk = "문서제목: 선발 안내\n분류: 공지\n본문: 재학생 6명을 선발합니다."
        self.assertEqual(title_anchor.keyword_hits(chunk, ["선발되", "인원"]), 1)
        self.assertEqual(title_anchor.keyword_hits("문서제목: 인원 안내\n본문: 없음", ["인원"]), 0)


class VectorHelpersTests(unittest.TestCase):
    def test_parse_vector_text_and_cosine(self):
        self.assertEqual(title_anchor.parse_vector("[1,0]"), [1.0, 0.0])
        self.assertIsNone(title_anchor.parse_vector("garbage"))
        self.assertAlmostEqual(title_anchor.cosine([1.0, 0.0], [1.0, 0.0]), 1.0)


if __name__ == "__main__":
    unittest.main()
