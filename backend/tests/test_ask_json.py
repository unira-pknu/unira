"""HTTP contract regression tests without live DB or model requests."""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.deps import get_state
from app.rate_limit import enforce_ask_rate_limit
from app.routers.ask import _search_all, router
from app.schemas import Source


class AskJsonTests(unittest.TestCase):
    def setUp(self):
        app = FastAPI()
        app.include_router(router)
        settings = SimpleNamespace(
            openai_model="test", max_chars_per_chunk=500,
            openai_timeout_seconds=10, openai_temperature=0.1,
            openai_max_tokens=100, source_excerpt_max_chars=300,
        )
        app.dependency_overrides[get_state] = lambda: SimpleNamespace(
            settings=settings, openai=None, system_prompt="test",
        )
        app.dependency_overrides[enforce_ask_rate_limit] = lambda: "test-user"
        self.client = TestClient(app)

    def test_answer_and_sources_are_json_even_for_legacy_stream_requests(self):
        source = Source(title="Guide", uri="https://example.com/guide",
                        content="Graduation requirements", similarity=0.9)
        for extra in ({}, {"stream": False}, {"stream": True}):
            with self.subTest(extra=extra), patch(
                "app.routers.ask._retrieve", return_value=([{}], [source])
            ), patch("app.routers.ask.generate_answer", return_value="Answer [1]."):
                response = self.client.post("/ask", json={"question": "Requirements?", **extra})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.headers["content-type"], "application/json")
                self.assertEqual(response.json()["answer"], "Answer [1].")
                self.assertEqual(response.json()["sources"][0]["title"], "Guide")

    def test_no_sources_is_json(self):
        with patch("app.routers.ask._retrieve", return_value=([], [])):
            response = self.client.post("/ask", json={"question": "Unknown", "stream": True})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["sources"], [])
        self.assertTrue(response.json()["answer"])

    def test_answerable_flag(self):
        source = Source(title="Guide", uri="", content="text", similarity=1)
        cases = [
            ("졸업학점은 130학점입니다 [1].", True),
            ("관련 정보를 찾을 수 없습니다.", False),
            (" \"관련 정보를 찾을 수 없습니다\" ", False),
            ("", False),
            ("신청 기간은 9월입니다 [1]. 제출 서류는 관련 정보를 찾을 수 없습니다.", True),
        ]
        for answer, expected in cases:
            with self.subTest(answer=answer), patch(
                "app.routers.ask._retrieve", return_value=([{}], [source])
            ), patch("app.routers.ask.generate_answer", return_value=answer):
                response = self.client.post("/ask", json={"question": "Question"})
                self.assertEqual(response.json()["answerable"], expected)
        with patch("app.routers.ask._retrieve", return_value=([], [])):
            self.assertFalse(self.client.post("/ask", json={"question": "Q"}).json()["answerable"])

    def test_generation_failure_is_http_error_not_sse_event(self):
        source = Source(title="Guide", uri="", content="text", similarity=1)
        with patch("app.routers.ask._retrieve", return_value=([{}], [source])), patch(
            "app.routers.ask.generate_answer", side_effect=RuntimeError("test")
        ):
            response = self.client.post("/ask", json={"question": "Question"})
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json(), {"detail": "generation failed"})


class SearchAllTests(unittest.TestCase):
    def test_sub_queries_run_together_and_keep_their_order(self):
        import threading

        # 세 검색이 모두 동시에 들어와야 통과하는 문 — 차례로 돌면 1초 뒤 BrokenBarrierError
        gate = threading.Barrier(3, timeout=1)

        def search_one(_state, query):
            gate.wait()
            return [{"id": query}]

        with patch("app.routers.ask._search_one", side_effect=search_one):
            rows = _search_all(None, ["a", "b", "c"])
        self.assertEqual([[{"id": "a"}], [{"id": "b"}], [{"id": "c"}]], rows)

    def test_single_query_skips_the_thread_pool(self):
        with patch("app.routers.ask._search_one", return_value=[{"id": 1}]) as search_one:
            self.assertEqual([[{"id": 1}]], _search_all(None, ["q"]))
        search_one.assert_called_once()
