import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd


SRC_DIR = Path(__file__).resolve().parents[1] / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from calc_thermal import HOURS, aggregate_routes


class AggregateRoutesTest(unittest.TestCase):
    @staticmethod
    def _samples(shade_values: list[object]) -> pd.DataFrame:
        data: dict[str, object] = {
            "segment_id": [1, 1, 1],
            "chainage_m": [0.0, 10.0, 20.0],
            "length_m": [20.0, 20.0, 20.0],
            "surface_type": ["pavement", "pavement", "pavement"],
        }
        for hour in HOURS:
            data[f"surface_temp_{hour:02d}_c"] = [30.0, 40.0, 50.0]
            data[f"is_shaded_{hour:02d}"] = shade_values
        return pd.DataFrame(data)

    def test_shade_ratio_uses_only_observed_values(self) -> None:
        result = aggregate_routes(self._samples([True, np.nan, False])).iloc[0]
        self.assertEqual(result["surface_type"], "pavement")
        for hour in HOURS:
            self.assertEqual(result[f"shade_ratio_{hour:02d}"], 0.5)

    def test_shade_ratio_remains_null_when_all_values_unknown(self) -> None:
        result = aggregate_routes(self._samples([np.nan, np.nan, np.nan])).iloc[0]
        for hour in HOURS:
            self.assertTrue(pd.isna(result[f"shade_ratio_{hour:02d}"]))

    def test_mixed_surface_types_fail_fast(self) -> None:
        samples = self._samples([False, False, False])
        samples.loc[1, "surface_type"] = "asphalt"
        with self.assertRaises(ValueError):
            aggregate_routes(samples)


if __name__ == "__main__":
    unittest.main()
