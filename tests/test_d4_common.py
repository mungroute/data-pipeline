import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from d4_common import sample_support_weights, weighted_mean, weighted_mode


class D4CommonTests(unittest.TestCase):
    def test_support_weights_preserve_short_last_interval(self) -> None:
        weights = sample_support_weights([0.0, 10.0, 20.0, 23.0], 23.0)
        np.testing.assert_allclose(weights, [5.0, 10.0, 6.5, 1.5])
        self.assertAlmostEqual(float(weights.sum()), 23.0)

    def test_single_sample_represents_entire_short_link(self) -> None:
        np.testing.assert_allclose(sample_support_weights([0.0], 4.2), [4.2])

    def test_weighted_mode_uses_length(self) -> None:
        self.assertEqual(weighted_mode(["grass", "asphalt"], [8, 2]), "grass")

    def test_weighted_mode_tie_is_conservative(self) -> None:
        self.assertEqual(weighted_mode(["grass", "asphalt"], [5, 5]), "asphalt")

    def test_weighted_mean(self) -> None:
        self.assertAlmostEqual(weighted_mean([0.1, 0.3], [1, 3]), 0.25)


if __name__ == "__main__":
    unittest.main()
