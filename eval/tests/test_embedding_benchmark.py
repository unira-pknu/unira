"""embedding_benchmark.run: a model that cannot load is skipped, the others are kept."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from eval.tools import embedding_benchmark as bench


class SkipFailingModelTests(unittest.TestCase):
    def test_failed_model_is_recorded_and_the_rest_still_scored(self):
        corpus = [{"url": "u1", "title": "휴학 안내", "text": "휴학"}, {"url": "u2", "title": "장학", "text": "장학"}]
        queries = [{"id": "q1", "q": "휴학", "gold_urls": ["u1"], "gold_titles": []}]

        def fake_embed(out, key, device, max_seq, batch):
            if key == "bad":
                raise OSError("weights are .bin only")
            return np.array([[1.0, 0.0]]), np.array([[1.0, 0.0], [0.0, 1.0]]), 0

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            for name, rows in (("corpus.jsonl", corpus), ("queries.jsonl", queries)):
                (out / name).write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows), encoding="utf-8")
            with patch.object(bench, "embed", side_effect=fake_embed):
                bench.run(out, ["bad", "good"], ["none"], "cpu", 128, 8)
            results = json.loads((out / "results.json").read_text(encoding="utf-8"))
        self.assertIn("weights are .bin only", results["embedding"]["bad"]["skipped"])
        self.assertEqual(1.0, results["embedding"]["good"]["top1"])


if __name__ == "__main__":
    unittest.main()
