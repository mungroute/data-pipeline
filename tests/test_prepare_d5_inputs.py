import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from prepare_d5_inputs import (
    ACTUAL_WORKBOOK,
    SYNTHETIC_WORKBOOK,
    load_actual_measurements,
    load_synthetic_measurements,
    read_latest_asos,
    select_measurement_weather,
)


class PrepareD5InputsTests(unittest.TestCase):
    def test_actual_rows_are_calibration_eligible(self) -> None:
        frame = load_actual_measurements(ACTUAL_WORKBOOK)
        self.assertEqual(len(frame), 12)
        self.assertTrue(frame["calibration_eligible"].all())
        self.assertTrue((frame["data_status"] == "ACTUAL").all())

    def test_synthetic_rows_can_never_calibrate(self) -> None:
        frame = load_synthetic_measurements(SYNTHETIC_WORKBOOK)
        self.assertEqual(len(frame), 12)
        self.assertFalse(frame["calibration_eligible"].any())
        self.assertTrue((frame["data_status"] == "SYNTHETIC").all())

    def test_same_date_asos_has_all_required_hours(self) -> None:
        asos, _ = read_latest_asos()
        weather = select_measurement_weather(asos)
        self.assertEqual(set(weather["hour"]), {9, 12, 15, 18})
        self.assertTrue((weather["weather_status"] == "OBSERVED_ASOS_SAME_DATE").all())
        self.assertEqual(weather["observed_at"].dt.date.nunique(), 1)
        self.assertEqual(str(weather["observed_at"].dt.date.iloc[0]), "2026-08-11")


if __name__ == "__main__":
    unittest.main()
