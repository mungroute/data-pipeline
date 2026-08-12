import sys
import unittest
from pathlib import Path

import geopandas as gpd
import pandas as pd
from shapely.geometry import Polygon

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from calc_park_proximity import calculate_distances


class ParkProximityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.parks = gpd.GeoDataFrame(
            {"ATRB_SE": ["UQT210"], "DGM_NM": ["test"]},
            geometry=[Polygon([(0, 0), (10, 0), (10, 10), (0, 10)])],
            crs="EPSG:5186",
        )

    def sample(self, x: float, y: float) -> pd.DataFrame:
        return pd.DataFrame([
            {"sample_id": 1, "segment_id": 1, "seq": 0, "x": x, "y": y,
             "chainage_m": 0.0, "length_m": 10.0}
        ])

    def test_inside_park_distance_is_zero_not_boundary_distance(self) -> None:
        result = calculate_distances(self.sample(5, 5), self.parks)
        self.assertEqual(result.loc[0, "park_proximity_m"], 0.0)
        self.assertEqual(result.loc[0, "park_boundary_m"], 5.0)
        self.assertTrue(result.loc[0, "inside_park"])

    def test_outside_park_uses_polygon_distance(self) -> None:
        result = calculate_distances(self.sample(13, 5), self.parks)
        self.assertAlmostEqual(result.loc[0, "park_proximity_m"], 3.0)
        self.assertAlmostEqual(result.loc[0, "park_boundary_m"], 3.0)
        self.assertFalse(result.loc[0, "inside_park"])


if __name__ == "__main__":
    unittest.main()
