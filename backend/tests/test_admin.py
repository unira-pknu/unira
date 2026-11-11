"""Admin dashboard: aggregation logic and the admin allowlist gate (no live DB)."""

import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock

from fastapi import Depends, FastAPI, Request
from fastapi.testclient import TestClient

from app.deps import get_state, require_admin, require_user
from app.routers.admin import build_dashboard, normalize_question, router as admin_router
from app.feedback_training import build_training_candidate, question_key


def feedback(id_, rating, chat, answer_at, *, reasons=None, comment=None, answer="답변"):
    return {
        "id": id_, "rating": rating, "reasons": reasons or [], "comment": comment,
        "created_at": f"2026-10-02T10:{id_[-1]}0:00+00:00",
        "chat_messages": {"content": answer, "chat_id": chat, "created_at": answer_at},
    }


class BuildDashboardTests(unittest.TestCase):
    def test_summary_thumbs_comments_and_question_pairing(self):
        rows = [
            feedback("f2", "down", "c1", "2026-10-02T09:05:00", reasons=["정보가 부정확해요"], comment="학점이 틀려요"),
            feedback("f1", "up", "c1", "2026-10-02T09:01:00"),
        ]
        user_messages = {"c1": [
            {"chat_id": "c1", "content": "졸업학점?", "created_at": "2026-10-02T09:00:00"},
            {"chat_id": "c1", "content": "전공필수는?", "created_at": "2026-10-02T09:04:00"},
        ]}
        result = build_dashboard(
            feedback_rows=rows, user_messages_by_chat=user_messages, recent_questions=[],
            total_feedback=4, up_count=3, total_questions=12,
            now=datetime(2026, 10, 2, tzinfo=timezone.utc),
        )
        summary = result["summary"]
        self.assertEqual((4, 3, 1, 0.75, 12), (summary["totalFeedback"], summary["upCount"],
                                               summary["downCount"], summary["satisfactionRate"],
                                               summary["totalQuestions"]))
        thumbs = result["feedback"]["thumbs"]
        # each answer is paired with the latest user question before it in the same chat
        self.assertEqual(["전공필수는?", "졸업학점?"], [t["question"] for t in thumbs])
        self.assertEqual(["down", "up"], [t["kind"] for t in thumbs])
        self.assertEqual(["정보가 부정확해요"], thumbs[0]["reasons"])
        self.assertEqual([{"id": "f2", "comment": "학점이 틀려요", "relatedQuestion": "전공필수는?",
                           "createdAt": rows[0]["created_at"]}], result["feedback"]["comments"])

    def test_no_feedback_has_no_satisfaction_rate(self):
        result = build_dashboard(feedback_rows=[], user_messages_by_chat={}, recent_questions=[],
                                 total_feedback=0, up_count=0, total_questions=0)
        self.assertIsNone(result["summary"]["satisfactionRate"])
        self.assertEqual([], result["feedback"]["thumbs"])

    def test_missing_question_is_labelled(self):
        result = build_dashboard(feedback_rows=[feedback("f1", "up", "c9", "2026-10-02T09:00:00")],
                                 user_messages_by_chat={}, recent_questions=[],
                                 total_feedback=1, up_count=1, total_questions=0)
        self.assertEqual("(질문을 찾을 수 없음)", result["feedback"]["thumbs"][0]["question"])

    def test_top_questions_group_trivial_variants(self):
        questions = ["졸업학점 알려줘", "졸업학점  알려줘?", "졸업학점 알려줘!", "휴학 신청", "", "  "]
        top = build_dashboard(feedback_rows=[], user_messages_by_chat={}, recent_questions=questions,
                              total_feedback=0, up_count=0, total_questions=6)
        self.assertEqual([("졸업학점 알려줘", 3), ("휴학 신청", 1)],
                         [(q["question"], q["count"]) for q in top["topQuestions"]])
        self.assertEqual(4, top["summary"]["questionSampleSize"])
        self.assertEqual(normalize_question("A  b?!"), "a b")


class RequireAdminTests(unittest.TestCase):
    def client(self, admin_emails, caller_email):
        app = FastAPI()

        @app.get("/secret")
        def secret(user_id: str = Depends(require_admin)):
            return {"user": user_id}

        def fake_user(request: Request) -> str:
            request.state.user_email = caller_email
            return "user-1"

        app.dependency_overrides[require_user] = fake_user
        app.dependency_overrides[get_state] = lambda: SimpleNamespace(
            settings=SimpleNamespace(admin_emails=admin_emails))
        return TestClient(app)

    def test_allowlisted_admin_passes_case_insensitively(self):
        response = self.client(["Admin@PKNU.ac.kr"], "admin@pknu.ac.kr").get("/secret")
        self.assertEqual(200, response.status_code)

    def test_other_user_is_forbidden(self):
        response = self.client(["admin@pknu.ac.kr"], "student@pknu.ac.kr").get("/secret")
        self.assertEqual(403, response.status_code)

    def test_empty_allowlist_denies_everyone(self):
        response = self.client([], "admin@pknu.ac.kr").get("/secret")
        self.assertEqual(403, response.status_code)


class AdminMeTests(unittest.TestCase):
    def client(self, admin_emails, caller_email):
        app = FastAPI()
        app.include_router(admin_router)

        def fake_user(request: Request) -> str:
            request.state.user_email = caller_email
            return "user-1"

        app.dependency_overrides[require_user] = fake_user
        app.dependency_overrides[get_state] = lambda: SimpleNamespace(
            settings=SimpleNamespace(admin_emails=admin_emails))
        return TestClient(app)

    def test_reports_admin_and_non_admin(self):
        self.assertEqual({"isAdmin": True},
                         self.client(["Admin@PKNU.ac.kr"], "admin@pknu.ac.kr").get("/admin/me").json())
        self.assertEqual({"isAdmin": False},
                         self.client(["admin@pknu.ac.kr"], "student@pknu.ac.kr").get("/admin/me").json())

    def test_requires_login(self):
        app = FastAPI()
        app.include_router(admin_router)
        app.dependency_overrides[get_state] = lambda: SimpleNamespace(
            settings=SimpleNamespace(admin_emails=["admin@pknu.ac.kr"]))
        self.assertEqual(401, TestClient(app).get("/admin/me").status_code)


class TrainingCandidateTests(unittest.TestCase):
    def row(self):
        return {
            "id": "00000000-0000-0000-0000-000000000001", "rating": "down",
            "updated_at": "2026-11-11T10:00:00+00:00", "reasons": ["부정확해요"],
            "comment": "조건이 달라요", "user_id": "must-not-be-exported",
            "chat_messages": {"role": "assistant", "chat_id": "private-chat", "content": "기존 답변",
                              "created_at": "2026-11-11T09:03:00+00:00", "sources": [{"url": "https://example.edu/rule"}]},
        }

    def questions(self):
        return [{"content": "그럼 전공필수는?", "created_at": "2026-11-11T09:02:00+00:00"},
                {"content": "졸업학점?", "created_at": "2026-11-11T09:00:00+00:00"}]

    def test_draft_has_no_positive_label_and_omits_user_and_chat_ids(self):
        draft = build_training_candidate(self.row(), self.questions())
        self.assertEqual("그럼 전공필수는?", draft["question"])
        self.assertEqual(question_key("그럼  전공필수는?!"), draft["original_question_key"])
        self.assertEqual("기존 답변", draft["rejected_answer"])
        self.assertEqual("pending", draft["review"]["status"])
        self.assertEqual("", draft["review"]["corrected_answer"])
        self.assertFalse(draft["review"]["context_checked"])
        self.assertNotIn("user_id", draft)
        self.assertNotIn("chat_id", draft)

    def test_missing_and_ambiguous_question_stay_unpaired(self):
        self.assertEqual("missing_question", build_training_candidate(self.row(), [])["pairing_status"])
        questions = self.questions()
        questions[1]["created_at"] = questions[0]["created_at"]
        draft = build_training_candidate(self.row(), questions)
        self.assertEqual("ambiguous_question", draft["pairing_status"])
        self.assertEqual("", draft["question"])
        questions = self.questions()
        questions[0]["created_at"] = self.row()["chat_messages"]["created_at"]
        self.assertEqual("ambiguous_question", build_training_candidate(self.row(), questions)["pairing_status"])

    def query(self, data):
        query = Mock()
        for method in ("select", "eq", "gt", "lte", "order", "limit"):
            getattr(query, method).return_value = query
        query.execute.return_value = SimpleNamespace(data=data)
        return query

    def client(self, db, *, admin=True):
        app = FastAPI()
        app.include_router(admin_router)

        def fake_user(request: Request):
            request.state.user_email = "admin@pknu.ac.kr" if admin else "student@pknu.ac.kr"
            return "caller"

        app.dependency_overrides[require_user] = fake_user
        app.dependency_overrides[get_state] = lambda: SimpleNamespace(
            supabase=db, settings=SimpleNamespace(admin_emails=["admin@pknu.ac.kr"]))
        return TestClient(app)

    def test_admin_only_before_any_service_role_query(self):
        db = Mock()
        self.assertEqual(403, self.client(db, admin=False).get("/admin/feedback/training-candidates").status_code)
        db.table.assert_not_called()
        app = FastAPI()
        app.include_router(admin_router)
        app.dependency_overrides[get_state] = lambda: SimpleNamespace(supabase=db)
        self.assertEqual(401, TestClient(app).get("/admin/feedback/training-candidates").status_code)
        db.table.assert_not_called()

    def test_pagination_filters_current_downvotes_and_pairs_per_answer(self):
        row = self.row()
        second = {**row, "id": "00000000-0000-0000-0000-000000000002"}
        feedback_query, question_query = self.query([row, second]), self.query(self.questions())
        db = Mock()
        db.table.side_effect = [feedback_query, question_query]
        cursor = "00000000-0000-0000-0000-000000000000"
        result = self.client(db).get(f"/admin/feedback/training-candidates?limit=1&after_id={cursor}")
        self.assertEqual(200, result.status_code)
        self.assertEqual("no-store", result.headers["cache-control"])
        self.assertEqual(row["id"], result.json()["nextCursor"])
        self.assertEqual(1, len(result.json()["items"]))
        feedback_query.eq.assert_called_once_with("rating", "down")
        feedback_query.gt.assert_called_once_with("id", cursor)
        feedback_query.limit.assert_called_once_with(2)
        question_query.eq.assert_any_call("chat_id", "private-chat")
        question_query.lte.assert_called_once_with("created_at", row["chat_messages"]["created_at"])

    def test_final_page_and_query_validation(self):
        db = Mock()
        db.table.return_value = self.query([])
        client = self.client(db)
        self.assertEqual({"items": [], "nextCursor": None}, client.get("/admin/feedback/training-candidates").json())
        db.reset_mock()
        for query in ("limit=0", "limit=51", "after_id=invalid"):
            self.assertEqual(422, client.get(f"/admin/feedback/training-candidates?{query}").status_code)
        db.table.assert_not_called()


if __name__ == "__main__":
    unittest.main()
