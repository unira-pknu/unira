"""The daily crawl runs each crawler as a file, not a module; they must still import."""

import os
import subprocess
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
# Same invocations as scripts/vectorization/preprocessing_pipeline_recent.py:crawl_dataset().
# 2026-10-24~26 the daily crawl failed because pknu_notice.py imported
# `scripts.*` before adding the project root to sys.path.
CRAWLERS = (
    "scripts/crawlers/departments/engine.py",
    "scripts/crawlers/pknu_notice.py",
    "scripts/crawlers/pknu_student_life.py",
    "scripts/crawlers/pknu_rule.py",
)


class CrawlerEntrypointTests(unittest.TestCase):
    def test_crawlers_start_when_run_as_files(self):
        env = {**os.environ, "PYTHONPATH": ""}
        for crawler in CRAWLERS:
            with self.subTest(crawler=crawler):
                result = subprocess.run(
                    [sys.executable, crawler, "--help"],
                    cwd=PROJECT_ROOT, env=env, capture_output=True, text=True,
                    encoding="utf-8", errors="replace", timeout=120,
                )
                self.assertEqual(0, result.returncode, result.stderr[-2000:])


if __name__ == "__main__":
    unittest.main()
