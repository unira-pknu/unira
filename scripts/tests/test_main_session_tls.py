"""Main-site sessions keep their own TLS adapter after a body-image download."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from scripts.crawlers.departments.tls import get_with_tls_policy
from scripts.crawlers.main.common.http import build_session


class MainSessionTlsTests(unittest.TestCase):
    def test_body_image_download_does_not_swap_the_adapter(self):
        # 2026-11-10: the shared TLS helper mounted a verifying adapter on this
        # verify=False session and every later page request failed.
        session = build_session()
        before = session.get_adapter("https://www.pknu.ac.kr/main/94")
        with patch.object(session, "get", return_value=object()):
            get_with_tls_policy(session, "https://www.pknu.ac.kr/upload/image.png", timeout=1)
        self.assertIs(before, session.get_adapter("https://www.pknu.ac.kr/main/96"))


if __name__ == "__main__":
    unittest.main()
