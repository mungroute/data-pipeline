import sys
import unittest
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from finalize_d4 import shade_grade, temperature_grade


class D4QualityTests(unittest.TestCase):
    def test_complete_calculation_is_shade_a(self) -> None:
        row = pd.Series({
            "svf_original": 0.5, "shade_ratio_09": 0.2, "shade_ratio_12": 0.1,
            "shade_ratio_15": 0.3, "shade_ratio_18": 0.8,
        })
        self.assertEqual(shade_grade(row), "A")

    def test_open_sky_fallback_is_shade_d(self) -> None:
        row = pd.Series({
            "svf_original": None, "shade_ratio_09": None, "shade_ratio_12": None,
            "shade_ratio_15": None, "shade_ratio_18": None,
        })
        self.assertEqual(shade_grade(row), "D")

    def test_temperature_uses_worst_required_input(self) -> None:
        self.assertEqual(temperature_grade("A", "A", 10.0), "A")
        self.assertEqual(temperature_grade("B", "A", 10.0), "B")
        self.assertEqual(temperature_grade("A", "D", 10.0), "D")
        self.assertEqual(temperature_grade("A", "A", float("nan")), "D")


if __name__ == "__main__":
    unittest.main()
