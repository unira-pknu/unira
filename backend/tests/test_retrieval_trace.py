"""Evaluation traces preserve ranking behavior, identity and pipeline boundaries."""

import json
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.deps import get_state
from app.query_plan import QuestionPlan, SearchTask, Target
from app.rate_limit import enforce_ask_rate_limit
from app.retrieval import search
from app.retrieval_trace import RetrievalTrace, candidate_key, source_identity
from app.routers.ask import _row_to_source, router
from app.search_status import SearchResults, SearchUnavailable

RPC = "match_pknu_main_documents"
BODY = ("교류수학 신청은 학생포털에서 진행합니다. 학생은 소속 학과에서 이수 가능한 개설 과목을 "
        "확인하고 성적증명서와 신청서를 제출해야 합니다. 교류 대학의 수업 일정과 학점 인정 기준을 "
        "확인한 뒤 담당 부서에서 승인 여부를 안내합니다. 등록금 납부와 수강신청 절차도 "
        "해당 학기 공지의 대상자 조건에 따라 확인해야 합니다.")


def row(name="A", index=0):
    return {"id": "db-" + name, "source_id": "source-" + name, "chunk_id": index, "chunk_index": index,
            "title": "교류수학 안내", "content": name + " " + BODY, "url": "https://www.pknu.ac.kr/" + name,
            "similarity": 0.9 if name == "A" else 0.8,
            "embedding": [123456789], "metadata": {"rpc_name": RPC, "source_dataset": "pknu_main",
                "ingestion_dataset": "ce", "department_id": "ce", "api_key": "secret-must-not-appear"}}


class ReverseRanker:
    def score(self, question, rows):
        return [0.1 if r["id"] == "db-A" else 0.9 for r in rows]


def run_search(*, capture_trace=True, reranker=None, scope_check=None, rows=None, expanded=(), **kwargs):
    rows = rows if rows is not None else [row(), row("B", 1)]
    fetched = {RPC: (SimpleNamespace(data=rows), 1.0, None),
               RPC + "_lexical": (SimpleNamespace(data=[]), 1.0, None)}
    options = dict(rpc_names=[RPC], embedding=[1.0], top_k=1, first_stage_k=2, min_similarity=0.3,
                   query_text="교류수학 신청 안내", reranker=reranker, capture_trace=capture_trace,
                   scope_check=scope_check)
    options.update(kwargs)
    with patch("app.retrieval._fetch_rpc_responses", return_value=fetched), patch(
        "app.retrieval._term_title_chunks", return_value=list(expanded)
    ), patch("app.retrieval._focus_department", side_effect=lambda client, embedding, rows, *args: rows):
        return search(None, **options)


def stage(trace, name):
    return next(s for s in trace["stages"] if s["stage"] == name)


def test_source_ids_preserve_zero_and_separate_sequence_from_database_row_id():
    source = _row_to_source(row())
    assert source.source_id == "source-A" and source.chunk_id == 0
    assert source.chunk_row_id == "db-A" and source.chunk_index == 0
    assert source.source_dataset == "pknu_main" and source.ingestion_dataset == "ce"
    assert source.department_id == "ce" and source.rpc_name == RPC
    missing = source_identity({"id": "db-only", "metadata": {"validated_target": {"department_id": "ce"}}})
    assert missing["chunk_id"] is None and missing["source_id"] is None
    assert missing["chunk_row_id"] == "db-only" and missing["department_id"] is None


def test_catalogue_source_ids_are_not_mislabelled_as_chunk_database_ids():
    notice = source_identity({"id": "notice-id", "source_id": "notice-id", "_source_row": True})
    assert notice["source_id"] == "notice-id" and notice["chunk_row_id"] is None
    aggregate = source_identity({"id": "synthetic-catalogue", "_synthetic_source": True,
                                 "metadata": {"source_ids": ["real-source-one", "real-source-two"]}})
    assert aggregate["source_id"] is None and aggregate["chunk_row_id"] is None
    assert aggregate["source_ids"] == ["real-source-one", "real-source-two"]


def test_trace_is_opt_in_and_does_not_change_selection():
    traced = run_search(reranker=ReverseRanker())
    with patch("app.retrieval.RetrievalTrace") as recorder:
        ordinary = run_search(capture_trace=False, reranker=ReverseRanker())
    recorder.assert_not_called()
    assert ordinary.trace is None
    assert [r["id"] for r in traced] == [r["id"] for r in ordinary] == ["db-B"]
    assert traced[0]["rerank_final_score"] == ordinary[0]["rerank_final_score"]


def test_trace_separates_raw_candidates_filtering_rerank_and_top_k():
    result = run_search(reranker=ReverseRanker())
    trace = result.trace
    assert stage(trace, "vector_returned")["count"] == 2
    assert [c["chunk_row_id"] for c in stage(trace, "rerank_input")["candidates"]] == ["db-A", "db-B"]
    assert [c["chunk_row_id"] for c in stage(trace, "rerank_ordered")["candidates"]] == ["db-B", "db-A"]
    assert [c["chunk_row_id"] for c in stage(trace, "task_selected")["candidates"]] == ["db-B"]
    assert trace["reranker"]["status"] == "applied"
    assert trace["parameters"]["candidate_counts"][RPC] == 2
    serialized = json.dumps(trace)
    assert "123456789" not in serialized and "secret-must-not-appear" not in serialized
    assert BODY not in serialized


def test_initial_scope_rejection_remains_visible_in_raw_candidate_trace():
    result = run_search(scope_check=lambda r: r["id"] != "db-A")
    assert stage(result.trace, "vector_returned")["count"] == 2
    assert stage(result.trace, "channel_merged")["count"] == 1
    rejected = result.trace["decisions"]
    assert rejected[0]["chunk_row_id"] == "db-A" and rejected[0]["reason"] == "applicability"


def test_source_cap_is_recorded_separately_from_reranking():
    a, b = row(), row("B", 1)
    for item in (a, b):
        item["source_id"] = "one-document"
        item["metadata"]["source_kind"] = "attachment"
    # A title expansion can add another chunk after the initial pool cap.
    result = run_search(rows=[a], expanded=[b], top_k=2, max_chunks_per_url=1)
    assert stage(result.trace, "rerank_input")["count"] == 2
    assert stage(result.trace, "after_source_caps")["count"] == 1
    assert any(d["reason"] == "per_source_cap" for d in result.trace["decisions"])


def test_reranker_failure_reports_fallback_without_exception_details():
    ranker = SimpleNamespace(score=lambda *args: (_ for _ in ()).throw(RuntimeError("secret-must-not-appear")))
    result = run_search(reranker=ranker)
    assert result.trace["reranker"] == {"status": "fallback", "error_type": "RuntimeError"}
    assert result[0]["id"] == "db-A"
    assert "secret-must-not-appear" not in json.dumps(result.trace)


def test_trace_limit_marks_incomplete_and_preserves_real_counts():
    trace = RetrievalTrace(max_records=1)
    trace.snapshot("raw", [row(), row("B", 1)])
    exported = trace.export()
    assert exported["complete"] is False
    assert exported["stages"][0]["count"] == 2 and exported["stages"][0]["recorded_count"] == 1
    assert candidate_key(row()) != candidate_key({**row(), "metadata": {"rpc_name": "match_rule_documents"}})


def api_client():
    state = SimpleNamespace(supabase=None, openai=None, system_prompt="system", settings=SimpleNamespace(
        rag_top_k=5, rag_context_neighbor_sources=0, max_chars_per_chunk=1000, source_excerpt_max_chars=120,
        openai_model="test", openai_timeout_seconds=5, openai_temperature=0.1, openai_max_tokens=100))
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_state] = lambda: state
    app.dependency_overrides[enforce_ask_rate_limit] = lambda: "test-user"
    return TestClient(app)


def exchange_task():
    # The real planner marks "교류수학" questions with the program context; without
    # it source_scope rightly drops 교류수학 documents from a general request.
    return SearchTask(text="교류수학 신청 안내", target=Target(contexts=["교류프로그램"]))


def test_api_evaluation_connects_trace_identity_prompt_and_answer_citations():
    plan = QuestionPlan(tasks=[exchange_task()])
    requested = []

    def find(state, query, task, *, capture_trace=False):
        requested.append(capture_trace)
        return run_search(capture_trace=capture_trace)

    with patch("app.routers.ask.plan_question", return_value=plan), patch(
        "app.routers.ask._search_one", side_effect=find
    ), patch("app.routers.ask.generate_answer", return_value="학생포털에서 신청합니다[1]."):
        regular = api_client().post("/ask", json={"question": "교류수학 신청 안내"}).json()
        captured = api_client().post("/ask", json={"question": "교류수학 신청 안내", "include_evidence": True}).json()
    assert requested == [False, True]
    assert "evaluation_evidence" not in regular and regular["sources"] == captured["sources"]
    assert captured["sources"][0]["chunk_id"] == 0
    evidence = captured["evaluation_evidence"]
    assert evidence["sources"][0]["key"] == candidate_key(row())
    assert evidence["sources"][0]["cited_in_answer"] is True
    assert evidence["generation"]["cited_source_indices"] == [1]
    assert stage(evidence["retrieval"]["trace"]["request"], "generation_sources")["count"] == 1
    assert evidence["retrieval"]["trace"]["tasks"][0]["trace"]["complete"] is True


def test_evaluation_retains_trace_on_search_unavailable_without_turning_it_into_success():
    plan = QuestionPlan(tasks=[exchange_task()])

    def unavailable(state, query, task, *, capture_trace=False):
        trace = RetrievalTrace()
        trace.snapshot("vector_returned", [], rpc_name=RPC, status="error")
        raise SearchUnavailable([{"rpc_name": RPC, "channel": "vector", "status": "error"}], trace=trace.export())

    with patch("app.routers.ask.plan_question", return_value=plan), patch(
        "app.routers.ask._search_one", side_effect=unavailable
    ):
        response = api_client().post("/ask", json={"question": "교류수학 신청 안내", "include_evidence": True})
    assert response.status_code == 503
    evidence = response.json()["evaluation_evidence"]
    assert evidence["retrieval"]["status"] == "partial"
    assert evidence["retrieval"]["trace"]["tasks"][0]["trace"]["stages"][0]["status"] == "error"
