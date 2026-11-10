"""Daily department crawl: one slice a day, gentle on the shared school server."""

from __future__ import annotations

import subprocess
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from scripts.vectorization import preprocessing_pipeline_recent as recent

DEPARTMENTS = ["ce", "chem", "english", "me", "mae", "physics", "stat"]


def shard_args(**overrides):
    values = {"department_shard": "0/3", "between_datasets_delay": 5.0, "max_consecutive_crawl_failures": 3}
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


class CrawlDatasetsTests(unittest.TestCase):
    def test_a_broken_site_does_not_stop_the_shard_and_sites_are_spaced(self):
        calls = []

        def crawl(name, args):
            calls.append(name)
            if name == "english":
                raise subprocess.CalledProcessError(1, ["engine"])

        with patch.object(recent, "crawl_dataset", side_effect=crawl), \
             patch.object(recent.time, "sleep") as sleep:
            failed = recent.crawl_datasets(["chem", "english", "me"], shard_args())
        self.assertEqual(["chem", "english", "me"], calls)
        self.assertEqual(["english"], failed)
        self.assertEqual(2, sleep.call_count)

    def test_a_run_of_failures_stops_before_hammering_the_server(self):
        calls = []

        def crawl(name, args):
            calls.append(name)
            raise subprocess.CalledProcessError(1, ["engine"])

        with patch.object(recent, "crawl_dataset", side_effect=crawl), patch.object(recent.time, "sleep"):
            failed = recent.crawl_datasets(["a", "b", "c", "d", "e"], shard_args())
        self.assertEqual(["a", "b", "c"], calls)
        self.assertEqual(["a", "b", "c"], failed)

    def test_the_fixed_daily_set_still_fails_fast(self):
        with patch.object(recent, "crawl_dataset", side_effect=subprocess.CalledProcessError(1, ["x"])):
            with self.assertRaises(subprocess.CalledProcessError):
                recent.crawl_datasets(["ce", "pknu_notice"], shard_args(department_shard=None))


if __name__ == "__main__":
    unittest.main()
