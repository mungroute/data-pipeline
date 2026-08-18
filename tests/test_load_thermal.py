import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd


SRC_DIR = Path(__file__).resolve().parents[1] / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from load_thermal import EXPECTED_SEGMENT_COUNT, validate_thermal_frame


class LoadThermalValidationTests(unittest.TestCase):
    @staticmethod
    def frame() -> pd.DataFrame:
        segment_ids = np.arange(1, EXPECTED_SEGMENT_COUNT + 1)
        return pd.DataFrame({
            "segment_id": segment_ids,
            "surface_type": np.where(segment_ids % 2, "pavement", "asphalt"),
            "weather_status": "OBSERVED_ASOS_SAME_DATE",
            "model_confidence": "LOW",
            "surface_temp_09_c": 30.0,
            "surface_temp_12_c": 45.0,
            "surface_temp_15_c": 50.0,
            "surface_temp_18_c": 35.0,
            "surface_temp_peak_c": 50.0,
        })

    def test_valid_frame_is_sorted_and_accepted(self) -> None:
        frame = self.frame().sample(frac=1.0, random_state=5)
        result = validate_thermal_frame(frame)
        self.assertEqual(len(result), EXPECTED_SEGMENT_COUNT)
        self.assertTrue(result["segment_id"].is_monotonic_increasing)

    def test_duplicate_segment_fails(self) -> None:
        frame = self.frame()
        frame.loc[1, "segment_id"] = frame.loc[0, "segment_id"]
        with self.assertRaises(ValueError):
            validate_thermal_frame(frame)

    def test_null_temperature_fails(self) -> None:
        frame = self.frame()
        frame.loc[0, "surface_temp_15_c"] = np.nan
        with self.assertRaises(ValueError):
            validate_thermal_frame(frame)

    def test_incorrect_peak_fails(self) -> None:
        frame = self.frame()
        frame.loc[0, "surface_temp_peak_c"] = 49.0
        with self.assertRaises(ValueError):
            validate_thermal_frame(frame)

    def test_non_low_confidence_fails(self) -> None:
        frame = self.frame()
        frame.loc[0, "model_confidence"] = "HIGH"
        with self.assertRaises(ValueError):
            validate_thermal_frame(frame)


if __name__ == "__main__":
    unittest.main()
