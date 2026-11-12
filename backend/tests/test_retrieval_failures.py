"""Channel failure isolation and API availability, without live DB/model calls."""

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.deps import get_state
from app.query_plan import QuestionPlan, SearchTask
from app.rate_limit import enforce_ask_rate_limit
from app.retrieval import _fetch_rpc_responses, search
from app.routers.ask import NO_INFO_ANSWER, SEARCH_UNAVAILABLE_MESSAGE, _retrieve, router
from app.schemas import Source
from app.search_status import SearchResults, SearchUnavailable

RPC = "match_pknu_main_documents"
CONTENT = ("교류수학 신청은 학생포털에서 진행합니다. 학생은 소속 학과에서 이수 가능한 개설 과목을 "
           "확인하고 성적증명서와 신청서를 제출해야 합니다. 교류 대학의 수업 일정과 학점 인정 기준을 "
           "확인한 뒤 담당 부서에서 승인 여부를 안내합니다. 등록금 납부와 수강신청 절차도 "
           "해당 학기 공지의 대상자 조건에 따라 확인해야 합니다.")


def hit(name="evidence", dataset="pknu_main", title="교류수학 안내"):
    return {"id": name, "source_id": name, "title": title, "content": CONTENT,
            "url": "https://www.pknu.ac.kr/" + name, "similarity": 0.9,
            "metadata": {"source_dataset": dataset, "source_kind": "page"}}


def ok(rows=()):
    return SimpleNamespace(data=list(rows)), 1.0, None


def failed():
    return None, 1.0, RuntimeError("private-db-address-and-credential")


def run_search(fetched, **overrides):
    options = dict(rpc_names=[RPC], embedding=[1.0], top_k=5, first_stage_k=5,
                   min_similarity=0.3, query_text="교류수학 신청 안내")
    options.update(overrides)
    with patch("app.retrieval._fetch_rpc_responses", return_value=fetched), patch(
        "app.retrieval._term_title_chunks", return_value=[]
    ), patch("app.retrieval._focus_department", side_effect=lambda client, embedding, rows, *args: rows):
        return search(None, **options)


def test_vector_failure_preserves_lexical_hits_and_rrf_score():
    result = run_search({RPC: failed(), RPC + "_lexical": ok([hit()])})
    assert len(result) == 1 and result[0]["id"] == "evidence"
    assert result[0]["_lexical_only"] is True
    assert 0.3 <= result[0]["similarity"] <= 0.75
    assert result.degraded
    assert [c["status"] for c in result.channels] == ["error", "ok"]
    diagnostics = json.dumps(result.diagnostics())
    assert "RuntimeError" in diagnostics
    assert "private-db-address-and-credential" not in diagnostics


def test_lexical_failure_preserves_vector_hits_and_real_similarity():
    result = run_search({RPC: ok([hit()]), RPC + "_lexical": failed()})
    assert len(result) == 1 and result[0]["similarity"] == 0.9
    assert not result[0].get("_lexical_only")
    assert result.degraded


def test_dataset_failure_does_not_discard_another_dataset():
    other = "match_pknu_notice_documents"
    result = run_search({RPC: failed(), RPC + "_lexical": failed(),
                         other: ok([hit("notice", "pknu_notice")]), other + "_lexical": ok()},
                        rpc_names=[RPC, other])
    assert [row["id"] for row in result] == ["notice"]
    assert sum(c["status"] == "error" for c in result.channels) == 2


def test_successful_empty_channels_are_an_ordinary_empty_result():
    result = run_search({RPC: ok(), RPC + "_lexical": ok()})
    assert result == [] and not result.degraded
    assert all(c["result_count"] == 0 for c in result.channels)


@pytest.mark.parametrize("vector,lexical", [(failed(), ok()), (ok(), failed()), (failed(), failed())])
def test_empty_with_any_failed_channel_is_not_evidence_of_absence(vector, lexical):
    with pytest.raises(SearchUnavailable):
        run_search({RPC: vector, RPC + "_lexical": lexical})


def test_unplanned_lexical_channel_is_not_misreported_as_a_failure():
    result = run_search({RPC: ok()}, query_text=None)
    assert result == [] and not result.degraded
    assert [c["channel"] for c in result.channels] == ["vector"]
    with pytest.raises(SearchUnavailable) as caught:
        run_search({RPC: failed()}, query_text=None)
    assert len(caught.value.channels) == 1


def test_no_configured_rpc_is_unavailable_instead_of_no_information():
    with pytest.raises(SearchUnavailable):
        run_search({}, rpc_names=[])


def test_filtered_partial_results_do_not_turn_into_a_no_information_response():
    with pytest.raises(SearchUnavailable):
        run_search({RPC: failed(), RPC + "_lexical": ok([hit()])}, scope_check=lambda row: False)


def test_healthy_dual_channel_hits_still_deduplicate():
    result = run_search({RPC: ok([hit()]), RPC + "_lexical": ok([hit()])})
    assert len(result) == 1 and not result.degraded
    assert result[0]["similarity"] == 0.9
    assert not result[0].get("_lexical_only")


@pytest.mark.parametrize("invalid", [None, {}, [42]])
def test_malformed_response_is_a_channel_failure_and_does_not_drop_its_companion(invalid):
    client = SimpleNamespace(rpc=lambda name, payload: SimpleNamespace(
        execute=lambda: SimpleNamespace(data=[] if name.endswith("_lexical") else invalid)))
    fetched = _fetch_rpc_responses(client, [RPC], {}, {RPC: 5}, "교류수학")
    assert isinstance(fetched[RPC][2], ValueError)
    assert fetched[RPC + "_lexical"][2] is None


def test_worker_client_failure_stays_in_its_own_channel_result():
    client = SimpleNamespace(rpc=lambda name, payload: SimpleNamespace(execute=lambda: SimpleNamespace(data=[])))
    with patch("app.retrieval._worker_client", side_effect=[RuntimeError("client unavailable"), client]):
        fetched = _fetch_rpc_responses(client, [RPC], {}, {RPC: 5}, "교류수학", max_workers=2)
    assert sum(error is not None for response, elapsed, error in fetched.values()) == 1
    assert sum(response is not None for response, elapsed, error in fetched.values()) == 1


def api_state():
    return SimpleNamespace(openai=None, supabase=None, system_prompt="system", settings=SimpleNamespace(
        openai_model="test", rag_top_k=5, rag_context_neighbor_sources=0, max_chars_per_chunk=1000,
        source_excerpt_max_chars=320, openai_timeout_seconds=5, openai_temperature=0.1, openai_max_tokens=100))


def client():
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_state] = api_state
    app.dependency_overrides[enforce_ask_rate_limit] = lambda: "test-user"
    return TestClient(app)


def channels(error=True):
    return [{"rpc_name": RPC, "channel": "vector", "status": "error" if error else "ok",
             "result_count": None if error else 0, "error_type": "RuntimeError" if error else None}]


def stream_events(response):
    return [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]


@pytest.mark.parametrize("stream", [False, True])
def test_api_distinguishes_unavailable_from_healthy_empty_search(stream):
    plan = QuestionPlan(tasks=[SearchTask(text="휴학 신청 안내")])
    for outage in (True, False):
        found = SearchResults(channels=channels(outage), failed_queries=["휴학 신청 안내"] if outage else [])
        with patch("app.routers.ask.plan_question", return_value=plan), patch(
            "app.routers.ask._search_all", return_value=[found]
        ), patch("app.routers.ask.generate_answer") as generate:
            response = client().post("/ask", json={"question": "휴학 신청 안내", "stream": stream})
        generate.assert_not_called()
        if outage:
            if stream:
                events = stream_events(response)
                assert events[-1]["type"] == "error" and events[-1]["status_code"] == 503
                assert not any(event["type"] == "done" for event in events)
            else:
                assert response.status_code == 503
                assert response.json()["detail"] == SEARCH_UNAVAILABLE_MESSAGE
        else:
            body = stream_events(response)[-1] if stream else response.json()
            assert response.status_code == 200
            assert body["answer"] == NO_INFO_ANSWER and body["answerable"] is False
            assert body["retrieval_status"] == "complete"


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("answer", ["학생포털에서 신청합니다[1].", NO_INFO_ANSWER])
def test_partial_api_results_keep_evidence_but_cannot_assert_absence(stream, answer):
    plan = QuestionPlan(tasks=[SearchTask(text="교류수학 신청 안내")])
    rows = SearchResults([hit()], channels=channels())
    sources = [Source(title="안내", uri="https://www.pknu.ac.kr/evidence", content=CONTENT, similarity=0.9)]
    with patch("app.routers.ask.plan_question", return_value=plan), patch(
        "app.routers.ask._retrieve", return_value=(rows, sources)
    ), patch("app.routers.ask.generate_answer", return_value=answer), patch(
        "app.routers.ask.stream_answer", return_value=iter([("token", answer), ("final", answer)])
    ):
        response = client().post("/ask", json={"question": "교류수학 신청 안내", "stream": stream,
                                              "include_evidence": not stream})
    if answer == NO_INFO_ANSWER:
        if stream:
            assert stream_events(response)[-1]["status_code"] == 503
            assert not any(e["type"] == "done" for e in stream_events(response))
        else:
            assert response.status_code == 503
    else:
        body = stream_events(response)[-1] if stream else response.json()
        assert body["retrieval_status"] == "partial" and body["answer"] == answer
        assert len(body["sources"]) == 1
        if not stream:
            assert body["evaluation_evidence"]["retrieval"]["channels"] == channels()


def test_failed_subquery_preserves_other_subquery_and_marks_unknown_request():
    plan = QuestionPlan(tasks=[SearchTask(text="휴학 신청 안내"), SearchTask(text="장학 신청 안내")])

    def find(state, query, task):
        if "휴학" in query:
            raise SearchUnavailable(channels())
        # A 장학 document: an exchange-program title would be filtered out of a
        # general 장학 request by source_scope before the merge is observed.
        return SearchResults([hit(title="장학 신청 안내")], channels=channels(False))

    with patch("app.routers.ask._search_one", side_effect=find):
        rows, sources = _retrieve(state=api_state(), user_id="test", question="휴학과 장학 신청 안내",
                                  chat_history=[], sub_questions=plan.queries, plan=plan)
    assert len(rows) == len(sources) == 1 and rows.degraded
    assert rows[0]["metadata"]["retrieval_incomplete"] is True
    assert "휴학" in rows[0]["metadata"]["unresolved_request"]
    assert "검색 장애" in rows[0]["metadata"]["unresolved_request"]
    coverage = rows[0]["metadata"]["request_coverage"]
    assert [entry["status"] for entry in coverage] == ["search_unavailable", "candidates_selected"]
