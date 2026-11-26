"""Faculty profiles stay scoped to the named person and department."""

from types import SimpleNamespace
from unittest.mock import MagicMock, call

from app.faculty import profile_chunks, profile_homepage
from app.generation import _source_content
from app.query_plan import SearchTask, Target
from app.routers.ask import _search_one


def _profile_source(source_id, person_number):
    return {"id": source_id, "title": "홍길동 명예교수", "source_type": "json",
            "url": f"https://physics.pknu.ac.kr/physics/303?action=view&no={person_number}",
            "metadata": {"department_id": "physics", "ingestion_dataset": "physics", "category": "교수진"}}


def _client(sources, chunks=()):
    client = MagicMock()
    queries = {"rag_sources": MagicMock(), "rag_chunks": MagicMock()}
    client.table.side_effect = queries.__getitem__
    for table, rows in (("rag_sources", sources), ("rag_chunks", chunks)):
        query = queries[table]
        for op in ("select", "eq", "contains", "order", "limit"):
            getattr(query, op).return_value = query
        query.execute.return_value = SimpleNamespace(data=list(rows))
    return client, queries


def test_named_degree_question_uses_the_exact_active_profile_as_evidence():
    source = _profile_source("profile", 17)
    client, queries = _client([source], [{"id": "profile-chunk", "source_id": "profile", "chunk_index": 0,
                                         "chunk_id": 1, "content": "홍길동 명예교수 학력: 부산대학교 이학박사"}])
    state = SimpleNamespace(supabase=client, settings=SimpleNamespace(rag_top_k=10), embedder=MagicMock())
    text = "물리학과 홍길동 명예교수가 박사학위를 받은 대학을 알려줘"
    task = SearchTask(text=text, target=Target(department_id="physics", instructor="홍길동"))

    rows = _search_one(state, text, task, capture_trace=True)

    assert len(rows) == 1
    assert rows[0]["url"] == source["url"] and rows[0]["id"] == "profile-chunk"
    assert "부산대학교" in rows[0]["content"]
    assert rows.trace["stages"][0]["stage"] == "faculty_profile_selected"
    assert queries["rag_sources"].eq.call_args_list == [call("status", "active"), call("category", "교수진"),
                                                       call("subcategory", "홍길동")]
    queries["rag_sources"].contains.assert_called_once_with("metadata", {"ingestion_dataset": "physics"})
    queries["rag_chunks"].eq.assert_called_once_with("source_id", "profile")
    state.embedder.encode_query.assert_not_called()


def test_professors_with_the_same_name_and_page_path_keep_separate_identities():
    client, queries = _client([_profile_source("first", 17), _profile_source("second", 18)])
    assert profile_chunks(client, department="physics", name="홍길동", question="홍길동 교수 학력", top_k=5) == []
    queries["rag_chunks"].execute.assert_not_called()


def test_exact_faculty_profile_preserves_short_unlabelled_research_field():
    source = _profile_source("profile", 17)
    body = "홍길동 교수\n등록된 내용이 없습니다.\n응집물질물리학\n" + "주요 논문 2022 초전도 연구. " * 30
    client, _ = _client([source], [{"id": "chunk", "content": body, "chunk_index": 0}])
    question = "홍길동 교수의 연구 분야는 무엇인가요?"
    rows = profile_chunks(client, department="physics", name="홍길동", question=question, top_k=5)
    assert _source_content(rows[0], 300, question) == body


def test_named_profile_homepage_requires_a_label_and_university_host():
    row = _profile_source("profile", 17)
    question = "홍길동 명예교수의 연구실 홈페이지 주소는?"
    assert profile_homepage(row, question, "홈페이지: https://lab.pknu.ac.kr 약력") == "https://lab.pknu.ac.kr"
    assert profile_homepage(row, question, "홈페이지: https://pknu.ac.kr.example.org") is None
    assert profile_homepage(row, question, "연구자료 https://lab.pknu.ac.kr") is None
    assert profile_homepage(row, "김영희 교수의 홈페이지 주소", "홈페이지: https://lab.pknu.ac.kr") is None
