"""Department questions reach the department's own guide pages."""

from unittest.mock import patch

from app.generation import _source_department
from app.query_plan import department_identity
from app.retrieval import _department_guide_chunks


class FakeQuery:
    def __init__(self, calls, rows):
        self.calls, self.rows = calls, rows

    def __getattr__(self, name):
        def record(*args, **kwargs):
            self.calls.append((name, args))
            return self
        return record

    @property
    def not_(self):
        self.calls.append(("not", ()))
        return self

    def execute(self):
        return type("Response", (), {"data": self.rows})()


class FakeClient:
    def __init__(self, rows):
        self.calls, self.rows = [], rows

    def table(self, name):
        self.calls.append(("table", (name,)))
        return FakeQuery(self.calls, self.rows)


def guide(question, rag_filter, rows=({"id": "src-faculty", "url": "https://ce.pknu.ac.kr/ce/1805"},)):
    client = FakeClient(list(rows))
    chunk = {"id": "c1", "url": "https://ce.pknu.ac.kr/ce/1805", "title": "교수진"}
    with patch("app.retrieval._fetch_notice_chunks", return_value=([], [])) as fetch, \
            patch("app.retrieval._notice_chunk_rows", return_value=[chunk]):
        extra = _department_guide_chunks(client, [0.0], [], question, [], 0.3, 0.15, 0.1, rag_filter)
    return client.calls, fetch, extra


def test_colloquial_department_names_resolve_to_the_registry():
    # 2026-11-11: "컴퓨터공학과" resolved to nothing, so other departments' 교수진 pages ranked first.
    assert department_identity("컴퓨터공학과 교수님 목록")[1] == "ce"
    assert department_identity("전자공학과 건물이 어디야?")[1] == "ee"
    assert department_identity("기계공학과 사무실")[1] == "me"
    assert department_identity("화학과 교수님")[1] == "chem"
    assert department_identity("과사무실 어디야") == (None, None)


def test_department_named_without_its_ending_or_by_a_short_name():
    # 2026-11-27 paraphrased evaluation: these found no department and searched unscoped.
    assert department_identity("통계데이터사이언스 팩스 번호 알려줘")[1] == "stat"
    assert department_identity("해양공학 이인철 교수님 실험실 이름")[1] == "oceaneng"
    assert department_identity("패션디자인 정현숙 교수님 방 어디야?")[1] == "fashion"
    assert department_identity("정외과 과사 어느 건물 몇 호야?")[1] == "politics"
    assert department_identity("영문과 황보현진 교수님")[1] == "english"
    assert department_identity("환경지질 이민희 교수님 방")[1] == "earth"


def test_short_forms_count_only_as_whole_words():
    assert department_identity("전자공학개론 강의실") == (None, None)  # a course
    assert department_identity("영문 과제 제출 기한") == (None, None)  # not 영문과
    assert department_identity("2026년 1학기 통계학 105832-103 강의실") == (None, None)  # course, stem too short


def test_faculty_question_adds_the_department_faculty_page():
    calls, fetch, extra = guide("컴퓨터·인공지능공학부 교수님 목록", {"ingestion_dataset": "ce"})
    assert ("contains", ("metadata", {"ingestion_dataset": "ce"})) in calls
    assert ("not", ()) in calls and ("like", ("url", "%?%")) in calls
    assert any(name == "or_" and "title.ilike.*교수*" in args[0] for name, args in calls)
    assert fetch.call_args.args[3] == ["src-faculty"]
    assert [row["title"] for row in extra] == ["교수진"]


def test_office_question_looks_for_the_location_page():
    calls, _, _ = guide("컴퓨터공학과 사무실이 어디야?", {"ingestion_dataset": "ce"})
    assert any(name == "or_" and "title.ilike.*오시는*" in args[0] for name, args in calls)


def test_needs_a_department_and_a_guide_topic():
    for question, rag_filter in (("교수님 목록", {}), ("컴공 장학금 신청", {"ingestion_dataset": "ce"})):
        calls, fetch, extra = guide(question, rag_filter)
        assert calls == [] and not fetch.called and extra == []


def test_candidates_already_present_are_not_added_twice():
    client = FakeClient([{"id": "src-faculty", "url": "https://ce.pknu.ac.kr/ce/1805"}])
    chunk = {"id": "c1", "url": "https://ce.pknu.ac.kr/ce/1805", "title": "교수진", "chunk_index": 0}
    with patch("app.retrieval._fetch_notice_chunks", return_value=([], [])), \
            patch("app.retrieval._notice_chunk_rows", return_value=[dict(chunk)]):
        extra = _department_guide_chunks(client, [0.0], [dict(chunk)], "컴공 교수님", [], 0.3, 0.15, 0.1,
                                         {"ingestion_dataset": "ce"})
    assert extra == []


def test_source_block_names_the_department():
    # The 교수진 page never names its department; without this the model often refused.
    metadata = {"department_name": "컴퓨터·인공지능공학부", "department_majors": ["컴퓨터공학전공, 인공지능전공"]}
    assert _source_department(metadata) == "컴퓨터·인공지능공학부 (컴퓨터공학전공, 인공지능전공)"
    assert _source_department({"department_name": "화학과", "department_majors": []}) == "화학과"
    assert _source_department({}) == ""
