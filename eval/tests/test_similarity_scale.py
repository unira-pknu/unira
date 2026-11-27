"""similarity_scale.fit_scale: the fitted line maps the new scores onto the old spread."""

import unittest

import numpy as np

from eval.tools.similarity_scale import PERCENTILES, fit_scale


class FitScaleTests(unittest.TestCase):
    def test_recovers_a_linear_squeeze(self):
        rng = np.random.default_rng(0)
        old = rng.uniform(0.4, 0.75, 3000)
        new = 0.75 + old * 0.15  # packed into 0.81-0.86
        floor, ceiling = fit_scale(old, new)
        scaled = (new - floor) / (ceiling - floor)
        for p in PERCENTILES:
            self.assertAlmostEqual(np.percentile(old, p), np.percentile(scaled, p), places=3)
        self.assertAlmostEqual(0.75, floor, places=3)
        self.assertAlmostEqual(0.90, ceiling, places=3)


if __name__ == "__main__":
    unittest.main()
