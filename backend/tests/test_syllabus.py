"""Exact offering lookup, ambiguity handling and inside-document evidence."""
import unittest
from types import SimpleNamespace

from app.syllabus import CourseRequest, evidence_window, extract_request, search_syllabus


class Query:
    def __init__(self, rows, calls):
        self.rows = list(rows)
        self.calls = calls

    def select(self, *args, **kwargs):
        return self

    def eq(self, key, value):
        self.rows = [r for r in self.rows if (str(r.get("metadata", {}).get(key.split("->>")[1], "")) if "->>" in key else r.get(key)) == value]
        return self

    def in_(self, key, values):
        self.rows = [r for r in self.rows if r.get(key) in values]
        return self

    def ilike(self, key, pattern):
        self.calls.append(pattern)
        words = pattern.strip("%").split("%")
        self.rows = [r for r in self.rows if all(w.casefold() in r[key].casefold() for w in words)]
        return self

    def like(self, key, pattern):
        words = pattern.strip("%").split("%")
        self.rows = [r for r in self.rows if all(w in str(r.get("metadata", {}).get(key.split("->>")[-1], "")) for w in words)]
        return self

    def order(self, key):
        self.rows.sort(key=lambda r: r.get(key, 0))
        return self

    def limit(self, size):
        self.rows = self.rows[:size]
        return self

    def execute(self):
        return SimpleNamespace(data=self.rows)


class DB:
    def __init__(self, sections=("101",), terms=("2",)):
        self.sources, self.chunks, self.calls = [], [], []
        for term in terms:
            for section in sections:
                for variant in ("kor", "eng"):
                    identity = term + section + variant
                    self.sources.append({"id": identity, "status": "active",
                        "title": f"103932_{section}_운영체제_{variant}_abcdef1234.pdf",
                        "file_path": f"files/pknu_syllabus/2026/{term}/source.pdf", "url": None,
                        "metadata": {"department": "컴퓨터공학전공", "staff_name": "홍길동"}})
                    for i, text in enumerate(("교과목 개요", "평가방법\n중간고사: 35% | 기말고사: 40%", "주교재: 운영체제 입문")):
                        self.chunks.append({"id": identity + str(i), "source_id": identity,
                                            "chunk_index": i, "chunk_id": i + 1, "content": text})

    def table(self, name):
        return Query(self.sources if name.endswith("sources") else self.chunks, self.calls)


def instructor_db(extra_courses=0):
    db = DB()
    db.sources = []
    db.chunks = []
    offerings = [(2026, "2", "학부", "102961", "104", "소프트웨어공학", "유승호"),
                 (2026, "2", "학부", "109700", "130", "캡스톤디자인Ⅱ", "유승호"),
                 (2026, "2", "학부", "112034", "102", "오픈소스소프트웨어", "유승호"),
                 (2026, "2", "학부", "112034", "103", "오픈소스소프트웨어", "유승호"),
                 (2026, "2", "학부", "114798", "101", "엣지컴퓨팅", "유승호"),
                 (2026, "1", "학부", "103932", "104", "운영체제", "유승호"),
                 (2026, "2", "산업대학원", "C00637", "001", "운영체제특론", "유승호"),
                 (2026, "2", "학부", "102997", "101", "수리학및실험", "오정은")]
    offerings += [(2026, "2", "학부", f"X{index}", "101", f"추가과목{index}", "유승호") for index in range(extra_courses)]
    for index, (year, term, level, code, section, name, staff) in enumerate(offerings):
        for variant in ("kor", "eng"):
            db.sources.append({"id": f"{index}:{variant}", "status": "active", "title": name,
                               "metadata": {"year": year, "term": term, "level": level, "course_no": code,
                                            "section": section, "course_name": name, "staff_name": staff,
                                            "department": "컴퓨터공학전공", "variant": variant}})
    return db


class SyllabusTests(unittest.TestCase):
    def test_extracts_identity_without_question_purpose_words(self):
        request = extract_request("2026학년도 2학기 운영체제 101분반 평가 비율이 궁금해요.")
        self.assertEqual((request.course, request.year, request.term, request.section), ("운영체제", 2026, "2", "101"))
        self.assertEqual(request.purposes, ("assessment",))
        self.assertIsNone(extract_request("등록금 납부는 어디서 하나요?"))
        self.assertEqual(extract_request("인공지능개론에서 사용하는 교재를 알려주세요.").course, "인공지능개론")
        pair = extract_request("2026-2 운영체제 103932-101 평가 비율")
        self.assertEqual((pair.course, pair.course_no, pair.section, pair.term), ("운영체제", "103932", "101", "2"))
        self.assertEqual(extract_request("2026-2학기 운영체제 학수번호 103932-101 평가 비율").course, "운영체제")
        from app.freshness import today_kst
        self.assertEqual(extract_request("작년 운영체제 교재").year, today_kst().year - 1)

    def test_followup_reuses_course_but_overrides_section(self):
        request = extract_request("2학기 102분반이요", [{"role": "user", "content": "운영체제 평가 비율 알려줘"}])
        self.assertEqual((request.course, request.term, request.section), ("운영체제", "2", "102"))

    def test_exact_course_only_lookup_and_purpose_first_chunk(self):
        db = DB()
        result = search_syllabus(db, CourseRequest("운영체제", 2026, "2", "101", purposes=("assessment",)))
        self.assertIsNone(result.answer)
        self.assertEqual(db.calls, ["%운영체제%"])
        self.assertIn("중간고사: 35%", result.rows[0]["content"])
        self.assertTrue(all(r["source_id"] == "2101kor" for r in result.rows))
        self.assertIn("launch.html", result.rows[0]["url"])

    def test_never_answers_across_ambiguous_sections_or_terms(self):
        result = search_syllabus(DB(sections=("101", "102"), terms=("1", "2")), CourseRequest("운영체제"))
        self.assertFalse(result.rows)
        self.assertIn("101분반", result.answer)
        self.assertIn("102분반", result.answer)
        self.assertIn("1학기", result.answer)
        self.assertIn("2학기", result.answer)

    def test_similar_course_is_not_substituted(self):
        db = DB()
        db.sources[0]["title"] = "103932_101_운영체제개론_kor_abcdef1234.pdf"
        result = search_syllabus(db, CourseRequest("운영체제개론아님"))
        self.assertFalse(result.rows)
        self.assertFalse(result.answerable)

    def test_window_keeps_evidence_after_generic_cutoff(self):
        text = "개요 " * 500 + "\n평가비율 | 중간고사: 35% | 기말고사: 40%"
        self.assertIn("기말고사: 40%", evidence_window(text, ("assessment",)))

    def test_professor_first_list_with_korean_particle(self):
        for q in ("유승호 교수님이 진행하는 강의명을 알려줘", "유승호 교수님이 수업하는 강의 목록을 알려줘"):
            request = extract_request(q)
            self.assertEqual(request.instructor, "유승호")
            self.assertTrue(request.list_courses)
            self.assertFalse(request.course)

    def test_exact_instructor_term_level_excludes_wrong_teacher_and_merges_variants_sections(self):
        request = extract_request("2026학년도 2학기 학부 유승호 교수님이 진행하는 강의명을 알려줘")
        result = search_syllabus(instructor_db(), request, top_k=1)
        self.assertNotIn("수리학", result.answer)
        self.assertNotIn("운영체제특론", result.answer)
        self.assertNotIn("운영체제 [", result.answer)
        self.assertEqual(result.answer.count("오픈소스소프트웨어"), 1)
        self.assertEqual(len(result.rows[0]["metadata"]["catalogue_offerings"]), 4)
        self.assertEqual(len(result.rows[0]["metadata"]["source_ids"]), 5)
        self.assertIn("[1]", result.answer)

    def test_list_enumerates_more_than_rag_top_k(self):
        request = extract_request("2026-2학기 학부 유승호 교수님 강의명 알려줘")
        result = search_syllabus(instructor_db(extra_courses=12), request, top_k=2)
        self.assertEqual(len(result.rows[0]["metadata"]["catalogue_offerings"]), 16)

    def test_missing_term_and_level_shows_scoped_choices(self):
        result = search_syllabus(instructor_db(), extract_request("유승호 교수님이 진행하는 강의명을 알려줘"))
        self.assertFalse(result.rows)
        self.assertIn("어느 학기·과정", result.answer)
        self.assertIn("2026학년도 1학기 · 학부", result.answer)
        self.assertIn("2026학년도 2학기 · 산업대학원", result.answer)

    def test_followup_uses_user_professor_not_previous_wrong_answer(self):
        history = [{"role": "user", "content": "유승호 교수님이 진행하는 강의명을 알려줘"},
                   {"role": "assistant", "content": "수리학및실험입니다."},
                   {"role": "user", "content": "2026학년도 2학기 학부만"}]
        request = extract_request("전체 강의명만 알려줘", history)
        self.assertEqual((request.instructor, request.year, request.term, request.level), ("유승호", 2026, "2", "학부"))
        self.assertNotIn("수리학", search_syllabus(instructor_db(), request).answer)

    def test_graduate_level_is_separate_from_undergraduate(self):
        request = extract_request("2026학년도 2학기 대학원 유승호 교수님 강의명 알려줘")
        result = search_syllabus(instructor_db(), request)
        self.assertIn("운영체제특론", result.answer)
        self.assertNotIn("오픈소스소프트웨어", result.answer)

    def test_unavailable_professor_is_not_substituted(self):
        result = search_syllabus(instructor_db(), extract_request("2026-2학기 학부 홍길동 교수님 강의명 알려줘"))
        self.assertFalse(result.rows)
        self.assertFalse(result.answerable)

    def test_new_topic_breaks_professor_followup_context(self):
        history = [{"role": "user", "content": "유승호 교수님 강의명 알려줘"}, {"role": "user", "content": "기숙사 신청 방법 알려줘"}]
        self.assertIsNone(extract_request("전체 강의명만 알려줘", history))

    def test_explicit_all_terms_and_levels_override_previous_selection(self):
        history = [{"role": "user", "content": "2026-2학기 학부 유승호 교수님 강의명 알려줘"}]
        request = extract_request("전체 학기 모든 과정 강의명만 알려줘", history)
        self.assertTrue(request.all_terms and request.all_levels)
        self.assertIsNone(request.year)
        self.assertIsNone(request.term)
        self.assertIsNone(request.level)
        result = search_syllabus(instructor_db(), request)
        self.assertIn("2026학년도 1학기", result.answer)
        self.assertIn("산업대학원", result.answer)

    def test_new_level_overrides_old_level(self):
        history = [{"role": "user", "content": "2026-2학기 대학원 유승호 교수님 강의명 알려줘"}]
        request = extract_request("학부만 알려줘", history)
        self.assertEqual(request.level, "학부")
        self.assertNotIn("운영체제특론", search_syllabus(instructor_db(), request).answer)

    def test_json_and_stream_use_exact_metadata_without_models_or_general_search(self):
        import json
        from unittest.mock import patch
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from app.deps import get_state
        from app.rate_limit import enforce_ask_rate_limit
        from app.routers.ask import router
        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[get_state] = lambda: SimpleNamespace(
            settings=SimpleNamespace(openai_model="test", rag_top_k=1), openai=None, supabase=instructor_db())
        app.dependency_overrides[enforce_ask_rate_limit] = lambda: "test-user"
        client = TestClient(app)
        for stream in (False, True):
            with patch("app.routers.ask._search_one") as generic, patch("app.routers.ask.generate_answer") as generation:
                response = client.post("/ask", json={"question": "2026-2학기 학부 유승호 교수님 강의명 알려줘", "stream": stream})
            self.assertEqual(response.status_code, 200)
            result = ([json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")][-1]
                      if stream else response.json())
            self.assertIn("소프트웨어공학", result["answer"])
            self.assertNotIn("수리학", result["answer"])
            self.assertEqual(len(result["sources"]), 1)
            self.assertIn("irumi.pknu.ac.kr", result["sources"][0]["uri"])
            generic.assert_not_called()
            generation.assert_not_called()


class ClassroomQuestionTests(unittest.TestCase):
    # 2026-11-11: "<교수> 교수님의 <과목> 강의실" named no syllabus word and went to
    # vector search, which answered "관련 정보를 찾을 수 없습니다".
    def test_classroom_and_time_questions_are_soft_syllabus_requests(self):
        request = extract_request("홍길동 교수님의 운영체제 강의실을 알려줘")
        self.assertEqual((request.course, request.instructor, request.soft), ("운영체제", "홍길동", True))
        self.assertIn("classroom", request.purposes)
        self.assertEqual(extract_request("운영체제 수업 시간 알려줘").course, "운영체제")
        self.assertFalse(extract_request("운영체제 강의계획서 알려줘").soft)

    def test_sections_of_one_term_are_listed_together(self):
        result = search_syllabus(DB(sections=("101", "102")), extract_request("운영체제 강의실 어디야"))
        self.assertIsNone(result.answer)
        self.assertEqual(len(result.rows), 1)
        self.assertIn("[101분반 · 홍길동]", result.rows[0]["content"])
        self.assertIn("[102분반 · 홍길동]", result.rows[0]["content"])
        self.assertIn("교과목 개요", result.rows[0]["content"])

    def test_without_a_term_the_latest_term_is_listed(self):
        result = search_syllabus(DB(sections=("101", "102"), terms=("1", "summer", "2")), CourseRequest("운영체제", purposes=("classroom",)))
        self.assertEqual(len(result.rows), 1)
        self.assertIn("2026년 2학기", result.rows[0]["title"])

    def test_other_questions_across_sections_still_ask_which_one(self):
        result = search_syllabus(DB(sections=("101", "102")), CourseRequest("운영체제", purposes=("textbook",)))
        self.assertFalse(result.rows)
        self.assertIn("102분반", result.answer)

    def test_unknown_course_falls_back_to_normal_search(self):
        result = search_syllabus(DB(), extract_request("공용 강의실 대여 방법"))
        self.assertFalse(result.rows)
        self.assertFalse(result.answerable)
