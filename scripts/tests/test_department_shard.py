"""Daily department crawl: one slice a day, gentle on the shared school server."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts.vectorization import preprocessing_pipeline_recent as recent

DEPARTMENTS = ["ce", "chem", "english", "me", "mae", "physics", "stat"]


def shard_args(**overrides):
    values = {
        "department_shard": "0/3",
        "between_datasets_delay": 5.0,
        "max_consecutive_crawl_failures": 3,
        "completed_file": None,
        "time_budget_minutes": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class DepartmentShardTests(unittest.TestCase):
    def test_slices_cover_every_department_once_without_ce(self):
        slices = [recent.department_shard(f"{n}/3", DEPARTMENTS) for n in range(3)]
        self.assertEqual(sorted(name for part in slices for name in part), sorted(set(DEPARTMENTS) - {"ce"}))
        self.assertEqual(["chem", "me"], slices[0])  # sorted: chem english mae me physics stat

    def test_bad_shard_spec_is_rejected(self):
        for spec in ("3/3", "-1/3", "0/0"):
            with self.subTest(spec=spec), self.assertRaises(ValueError):
                recent.department_shard(spec, DEPARTMENTS)


class DepartmentShardRunTests(unittest.TestCase):
    def run_shard(self, names, crawl=None, process=None, **overrides):
        calls = {"crawl": [], "process": []}

        def fake_crawl(name, args):
            calls["crawl"].append(name)
            if crawl:
                crawl(name)

        def fake_process(datasets, started_at, args):
            calls["process"].append(list(datasets))
            if process:
                process(datasets[0])

        with tempfile.TemporaryDirectory() as tmp:
            completed = Path(tmp) / "completed.txt"
            args = shard_args(completed_file=completed, **overrides)
            with patch.object(recent, "crawl_dataset", side_effect=fake_crawl),                  patch.object(recent, "process_recent", side_effect=fake_process),                  patch.object(recent.time, "sleep") as sleep:
                try:
                    recent.run_department_shard(names, args)
                    error = None
                except RuntimeError as raised:
                    error = raised
            done = completed.read_text(encoding="utf-8").split() if completed.exists() else []
        return calls, done, error, sleep

    def test_each_department_is_loaded_before_the_next_is_crawled(self):
        calls, done, error, sleep = self.run_shard(["chem", "me"])
        self.assertEqual([["chem"], ["me"]], calls["process"])
        self.assertEqual(["chem", "me"], done)
        self.assertIsNone(error)
        self.assertEqual(1, sleep.call_count)

    def test_a_broken_site_is_not_recorded_and_does_not_stop_the_shard(self):
        def crawl(name):
            if name == "english":
                raise subprocess.CalledProcessError(1, ["engine"])

        calls, done, error, _ = self.run_shard(["chem", "english", "me"], crawl=crawl)
        self.assertEqual(["chem", "english", "me"], calls["crawl"])
        self.assertEqual(["chem", "me"], done)
        self.assertIn("english", str(error))

    def test_a_failed_load_is_not_recorded(self):
        def process(name):
            if name == "chem":
                raise RuntimeError("db down")

        _, done, error, _ = self.run_shard(["chem", "me"], process=process)
        self.assertEqual(["me"], done)
        self.assertIn("chem", str(error))

    def test_a_run_of_failures_stops_before_hammering_the_server(self):
        def crawl(name):
            raise subprocess.CalledProcessError(1, ["engine"])

        calls, done, _, _ = self.run_shard(["a", "b", "c", "d", "e"], crawl=crawl)
        self.assertEqual(["a", "b", "c"], calls["crawl"])
        self.assertEqual([], done)

    def test_time_budget_leaves_the_rest_for_next_turn(self):
        clock = iter([0.0, 0.0, 61.0, 61.0])
        with patch.object(recent.time, "monotonic", side_effect=lambda: next(clock)):
            calls, done, error, _ = self.run_shard(["chem", "me", "stat"], time_budget_minutes=1)
        self.assertEqual(["chem"], calls["crawl"])
        self.assertEqual(["chem"], done)
        self.assertIsNone(error)


class FixedDailySetTests(unittest.TestCase):
    def test_the_fixed_daily_set_still_fails_fast(self):
        with patch.object(recent, "crawl_dataset", side_effect=subprocess.CalledProcessError(1, ["x"])):
            with self.assertRaises(subprocess.CalledProcessError):
                recent.crawl_datasets(["ce", "pknu_notice"], shard_args(department_shard=None))


if __name__ == "__main__":
    unittest.main()
