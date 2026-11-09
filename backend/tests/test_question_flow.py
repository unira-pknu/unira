"""JSON and SSE must apply the same plan before retrieval/generation."""
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.deps import get_state
from app.notice_listing import ListingResult
from app.query_plan import finish_plan, rule_plan
from app.rate_limit import enforce_ask_rate_limit
from app.routers.ask import router


def client():
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_state] = lambda: SimpleNamespace(
        settings=SimpleNamespace(openai_model="test", rag_top_k=10), openai=None, supabase=None)
    app.dependency_overrides[enforce_ask_rate_limit] = lambda: "test-user"
    return TestClient(app)


def body(response, stream):
    assert response.status_code == 200
    if stream:
        events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
        assert events[-1]["type"] == "done"
        return events[-1]
    return response.json()


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("question", ["거기 전화번호는?", "신청 언제까지야?", "기숙사 입실 신청 자격과 절차를 알려주세요."])
def test_ambiguity_skips_search_and_generation_for_both_protocols(stream, question):
    with patch("app.routers.ask._retrieve") as search, patch("app.routers.ask.generate_small_talk") as talk:
        result = body(client().post("/ask", json={"question": question, "stream": stream}), stream)
    assert not result["sources"] and not result["answerable"]
    assert result["answer"].endswith("?")
    search.assert_not_called()
    talk.assert_not_called()


@pytest.mark.parametrize("stream", [False, True])
def test_sorted_listing_keeps_citations_in_both_protocols(stream):
    q = "컴공 최신 공지 3개"
    plan = finish_plan(rule_plan(q), q, [q])
    row = {"id": "1", "source_id": "1", "title": "공지", "url": "https://ce.pknu.ac.kr/ce/1814?no=1", "content": "게시일 2026-11-07", "similarity": 1.0}
    with patch("app.routers.ask.plan_question", return_value=plan), patch(
        "app.routers.ask.list_notices", return_value=ListingResult([row], "최신 공지입니다 [1].")
    ) as listing, patch("app.routers.ask.generate_answer") as generation:
        result = body(client().post("/ask", json={"question": q, "stream": stream}), stream)
    assert result["answer"] == "최신 공지입니다 [1]."
    assert result["sources"][0]["uri"] == row["url"]
    assert listing.call_args.args[1].target.department_id == "ce"
    generation.assert_not_called()


def test_scope_is_applied_before_reranking_even_to_added_candidates():
    from app.retrieval import search
    good, bad = {"id": "good"}, {"id": "bad"}
    with patch("app.retrieval._fetch_rpc_responses", return_value={}), patch(
        "app.retrieval._term_title_chunks", return_value=[bad, good]
    ), patch("app.retrieval._focus_department", side_effect=lambda *args: args[2]), patch(
        "app.retrieval._rerank_candidates", return_value=[good]
    ) as rerank:
        result = search(None, rpc_names=[], embedding=[1.0], top_k=1, first_stage_k=2, min_similarity=0.3,
                        query_text="query", scope_check=lambda row: row["id"] != "bad")
    assert result == [good]
    assert rerank.call_args.kwargs["candidates"] == [good]
