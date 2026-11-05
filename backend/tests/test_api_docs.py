"""The API docs pages stay off unless API_DOCS_ENABLED is set (the backend's addresses are public)."""

import os
import unittest
from unittest import mock

for name, value in {
    "SUPABASE_URL": "https://example.supabase.co",
    "SUPABASE_SERVICE_ROLE_KEY": "test",
    "OPENAI_API_KEY": "test",
}.items():
    os.environ.setdefault(name, value)

from fastapi.testclient import TestClient  # noqa: E402

from app import main  # noqa: E402
from app.config import Settings  # noqa: E402


def _client(**overrides) -> TestClient:
    with mock.patch.object(main, "get_settings", return_value=Settings(**overrides)):
        return TestClient(main.create_app())  # no `with`: lifespan (model loading) does not run


class ApiDocsTests(unittest.TestCase):
    def test_docs_are_off_by_default(self):
        client = _client()
        for path in ("/docs", "/redoc", "/openapi.json"):
            self.assertEqual(404, client.get(path).status_code, path)
        self.assertEqual(200, client.get("/health").status_code)

    def test_docs_can_be_turned_on_for_local_use(self):
        client = _client(api_docs_enabled=True)
        self.assertEqual(200, client.get("/docs").status_code)
        self.assertEqual(200, client.get("/openapi.json").status_code)


if __name__ == "__main__":
    unittest.main()
