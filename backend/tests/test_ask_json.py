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

    def test_answer_and_sources_are_json_unless_stream_is_asked_for(self):
        source = Source(title="Guide", uri="https://example.com/guide",
                        content="Graduation requirements", similarity=0.9)
        for extra in ({}, {"stream": False}):
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
            response = self.client.post("/ask", json={"question": "Unknown"})
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


class SmallTalkTests(unittest.TestCase):
    def setUp(self):
        app = FastAPI()
        app.include_router(router)
        settings = SimpleNamespace(openai_model="test", openai_timeout_seconds=10)
        app.dependency_overrides[get_state] = lambda: SimpleNamespace(settings=settings, openai=None)
        app.dependency_overrides[enforce_ask_rate_limit] = lambda: "test-user"
        self.client = TestClient(app)

    def test_greeting_is_answered_without_searching(self):
        with patch("app.routers.ask.plan_search_queries", return_value=[]), patch(
            "app.routers.ask.generate_small_talk", return_value="안녕하세요! 학사 도우미 UNIRA예요."
        ), patch("app.routers.ask._retrieve") as retrieve:
            body = self.client.post("/ask", json={"question": "안녕"}).json()
        retrieve.assert_not_called()
        self.assertEqual({"answer": "안녕하세요! 학사 도우미 UNIRA예요.", "sources": [], "answerable": True}, body)


def _planner(content):
    message = SimpleNamespace(content=content)
    create = lambda **_: SimpleNamespace(choices=[SimpleNamespace(message=message)])  # noqa: E731
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


class PlanSearchQueriesTests(unittest.TestCase):
    def plan(self, content):
        from app.query_rewrite import plan_search_queries

        return plan_search_queries(openai_client=_planner(content), model="m", question="q", chat_history=[])

    def test_empty_queries_mean_small_talk(self):
        self.assertEqual([], self.plan('{"queries": []}'))

    def test_unusable_output_still_searches_the_question(self):
        for content in ["not json", '{"other": 1}', '{"queries": "q"}']:
            with self.subTest(content=content):
                self.assertEqual(["q"], self.plan(content))


def _events(response):
    import json

    return [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]


class AskStreamTests(unittest.TestCase):
    def setUp(self):
        app = FastAPI()
        app.include_router(router)
        settings = SimpleNamespace(
            openai_model="test", max_chars_per_chunk=500, openai_timeout_seconds=10,
            openai_temperature=0.1, openai_max_tokens=100, source_excerpt_max_chars=300,
        )
        app.dependency_overrides[get_state] = lambda: SimpleNamespace(settings=settings, openai=None, system_prompt="s")
        app.dependency_overrides[enforce_ask_rate_limit] = lambda: "test-user"
        self.client = TestClient(app)

    def post(self, **patches):
        source = Source(title="Guide", uri="", content="본문", similarity=1)
        defaults = {
            "plan_search_queries": ["q1", "q2"],
            "_retrieve": ([{"content": "본문"}], [source]),
            "stream_answer": iter([("token", "졸업은 "), ("token", "130학점[1]."), ("final", "졸업은 130학점[1].")]),
        }
        defaults.update(patches)
        with patch("app.routers.ask.plan_search_queries", return_value=defaults["plan_search_queries"]), patch(
            "app.routers.ask._retrieve", return_value=defaults["_retrieve"]
        ), patch("app.routers.ask.stream_answer", return_value=defaults["stream_answer"]), patch(
            "app.routers.ask.generate_small_talk", return_value="안녕하세요!"
        ):
            response = self.client.post("/ask", json={"question": "졸업 학점?", "stream": True})
        self.assertTrue(response.headers["content-type"].startswith("text/event-stream"))
        return _events(response)

    def test_steps_then_answer_pieces_then_done(self):
        events = self.post()
        self.assertEqual(
            [("status", "planning"), ("status", "searching"), ("status", "writing"), ("token", None), ("token", None), ("done", None)],
            [(e["type"], e.get("stage")) for e in events],
        )
        self.assertEqual(2, events[1]["count"])
        self.assertEqual("졸업은 130학점[1].", events[-1]["answer"])
        self.assertEqual(["Guide"], [s["title"] for s in events[-1]["sources"]])
        self.assertTrue(events[-1]["answerable"])

    def test_failed_number_check_announces_and_replaces(self):
        events = self.post(stream_answer=iter([
            ("token", "150만원"), ("revising", ""), ("replace", "금액은 자료에 없습니다."), ("final", "금액은 자료에 없습니다."),
        ]))
        self.assertEqual(["status", "status", "status", "token", "status", "replace", "done"], [e["type"] for e in events])
        self.assertEqual("revising", events[4]["stage"])
        self.assertEqual("금액은 자료에 없습니다.", events[-1]["answer"])

    def test_small_talk_skips_search(self):
        events = self.post(plan_search_queries=[])
        self.assertEqual([("status", "planning"), ("done", None)], [(e["type"], e.get("stage")) for e in events])
        self.assertEqual({"answer": "안녕하세요!", "sources": [], "answerable": True}, {k: events[-1][k] for k in ("answer", "sources", "answerable")})

    def test_no_sources_is_a_no_info_done(self):
        events = self.post(_retrieve=([], []))
        self.assertEqual({"type": "done", "answer": "관련 정보를 찾을 수 없습니다.", "sources": [], "answerable": False}, events[-1])

    def test_generation_failure_mid_stream_is_an_error_event(self):
        def broken():
            yield "token", "졸업은"
            raise RuntimeError("openai down")

        events = self.post(stream_answer=broken())
        self.assertEqual({"type": "error", "detail": "generation failed"}, events[-1])
