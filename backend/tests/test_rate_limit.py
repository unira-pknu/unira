"""/ask daily cap per account (KST day), on top of the per-minute window."""

import os
import unittest
from datetime import date
from types import SimpleNamespace
from unittest import mock

for name, value in {
    "SUPABASE_URL": "https://example.supabase.co",
    "SUPABASE_SERVICE_ROLE_KEY": "test",
    "OPENAI_API_KEY": "test",
}.items():
    os.environ.setdefault(name, value)

from fastapi import HTTPException  # noqa: E402

from app import rate_limit  # noqa: E402
from app.config import Settings  # noqa: E402


def _call(user_id="u1", email="student@pukyong.ac.kr", **settings):
    request = SimpleNamespace(state=SimpleNamespace(user_email=email))
    state = SimpleNamespace(settings=Settings(**settings))
    return rate_limit.enforce_ask_rate_limit(request, user_id=user_id, state=state)


class DailyLimitTests(unittest.TestCase):
    def setUp(self):
        rate_limit._hits.clear()
        rate_limit._daily.clear()
        patcher = mock.patch.object(rate_limit, "today_kst", return_value=date(2026, 11, 2))
        self.today = patcher.start()
        self.addCleanup(patcher.stop)

    def test_requests_over_the_daily_cap_get_429(self):
        for _ in range(3):
            _call(ask_daily_limit=3)
        with self.assertRaises(HTTPException) as caught:
            _call(ask_daily_limit=3)
        self.assertEqual(429, caught.exception.status_code)
        self.assertIn("3회", caught.exception.detail)

    def test_cap_is_per_account_and_resets_the_next_day(self):
        for _ in range(2):
            _call(ask_daily_limit=2)
        self.assertEqual("u2", _call(user_id="u2", ask_daily_limit=2))
        self.today.return_value = date(2026, 11, 3)
        self.assertEqual("u1", _call(ask_daily_limit=2))

    def test_admins_and_zero_limit_are_not_capped(self):
        for _ in range(5):
            _call(email="admin@pukyong.ac.kr", ask_daily_limit=1, admin_emails=["Admin@pukyong.ac.kr"])
            _call(user_id="u3", ask_daily_limit=0)


if __name__ == "__main__":
    unittest.main()
