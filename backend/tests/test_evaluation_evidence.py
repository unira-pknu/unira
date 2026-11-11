"""Evaluation capture keeps source bodies and citation numbering intact."""

from types import SimpleNamespace
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.deps import get_state
from app.evaluation import evidence_snapshot
from app.query_plan import QuestionPlan, SearchTask
from app.rate_limit import enforce_ask_rate_limit
from app.routers.ask import router
from app.schemas import Source


def state():
    return SimpleNamespace(
        system_prompt="system", openai=None,
        settings=SimpleNamespace(
            openai_model="test", openai_temperature=0.1, openai_max_tokens=100,
            openai_timeout_seconds=5, max_chars_per_chunk=500, source_excerpt_max_chars=80,
            openai_api_key="secret-must-not-appear", supabase_service_role_key="secret-must-not-appear",
        ),
    )


def test_json_capture_preserves_full_bodies_and_leaves_ui_excerpts_short():
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_state] = state
    app.dependency_overrides[enforce_ask_rate_limit] = lambda: "test-user"
    client = TestClient(app)
    content = "졸업 소요학점은 140학점입니다. " + "상세 설명 " * 200 + "원문 마지막 문장"
    row = {"content": content, "source_id": "s1", "chunk_id": "c1", "metadata": {"department_id": "ce"}}
    source = Source(title="안내", uri="https://example.com", content=content, similarity=1)
    plan = QuestionPlan(tasks=[SearchTask(text="졸업 학점?")])
    with patch("app.routers.ask.plan_question", return_value=plan), patch(
        "app.routers.ask._retrieve", return_value=([row], [source])
    ), patch("app.routers.ask.generate_answer", return_value="140학점입니다[1]."):
        regular = client.post("/ask", json={"question": "졸업 학점?"}).json()
        captured = client.post("/ask", json={"question": "졸업 학점?", "include_evidence": True}).json()
    assert "evaluation_evidence" not in regular
    assert regular["sources"] == captured["sources"]
    assert len(captured["sources"][0]["content"]) <= 80
    evidence = captured["evaluation_evidence"]
    assert evidence["sources"][0]["content"] == content
    assert len(evidence["sources"][0]["shown_content"]) <= 500
    assert evidence["sources"][0]["index"] == 1
    assert evidence["sources"][0]["chunk_id"] == "c1"
    assert evidence["sources"][0]["metadata"]["department_id"] == "ce"
    assert "secret-must-not-appear" not in str(evidence)


def test_direct_answer_marker_does_not_shift_citation_numbers():
    rows = [{"_direct_answer": "최신 공지[1]."}, {"source_id": "s1", "content": "공지 원문"}]
    sources = [Source(title="공지", uri="", content="공지 원문", similarity=1)]
    evidence = evidence_snapshot(state(), QuestionPlan(), rows, sources)
    assert evidence["answer_mode"] == "direct"
    assert evidence["sources"][0]["index"] == 1
    assert evidence["sources"][0]["source_id"] == "s1"
    assert evidence["sources"][0]["shown_content"] is None


def test_comparison_coverage_is_kept_in_evaluation_capture():
    coverage = [{"question": "두 번째 전공의 필수과목", "status": "no_candidates", "candidate_count": 0}]
    rows = [{"content": "첫 번째 전공 자료", "metadata": {
        "request_coverage": coverage, "comparison_targets": ["첫 번째 전공", "두 번째 전공"],
    }}]
    sources = [Source(title="교육과정", uri="", content=rows[0]["content"], similarity=1)]
    captured = evidence_snapshot(state(), QuestionPlan(), rows, sources)
    assert captured["sources"][0]["metadata"]["request_coverage"] == coverage
    assert captured["sources"][0]["metadata"]["comparison_targets"] == ["첫 번째 전공", "두 번째 전공"]


def test_evidence_capture_rejects_streaming_explicitly():
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_state] = state
    app.dependency_overrides[enforce_ask_rate_limit] = lambda: "test-user"
    response = TestClient(app).post("/ask", json={"question": "졸업 학점?", "stream": True, "include_evidence": True})
    assert response.status_code == 400
