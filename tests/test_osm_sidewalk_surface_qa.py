import sys
import unittest
from pathlib import Path

import geopandas as gpd
from shapely.geometry import LineString

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from prepare_osm_sidewalk_surface_qa import match_routes_to_osm


class OsmSidewalkSurfaceQaTests(unittest.TestCase):
    def test_parallel_explicit_sidewalk_creates_review_conflict(self) -> None:
        routes = gpd.GeoDataFrame(
            {"segment_id": [1], "surface_type": ["asphalt"], "classification_basis": ["BASE"]},
            geometry=[LineString([(0, 0), (30, 0)])],
            crs="EPSG:5186",
        )
        sidewalks = gpd.GeoDataFrame(
            {"osm_id": ["w1"], "osm_surface": ["paving_stones"], "mapped_surface": ["pavement"]},
            geometry=[LineString([(0, 1), (30, 1)])],
            crs="EPSG:5186",
        )
        result = match_routes_to_osm(routes, sidewalks)
        self.assertEqual(len(result), 1)
        self.assertEqual(result.iloc[0]["decision"], "REVIEW_OSM_CONFLICT")
        self.assertEqual(result.iloc[0]["proposed_surface"], "pavement")

    def test_perpendicular_crossing_is_rejected(self) -> None:
        routes = gpd.GeoDataFrame(
            {"segment_id": [1], "surface_type": ["asphalt"]},
            geometry=[LineString([(0, 0), (30, 0)])],
            crs="EPSG:5186",
        )
        sidewalks = gpd.GeoDataFrame(
            {"osm_id": ["w1"], "osm_surface": ["paving_stones"], "mapped_surface": ["pavement"]},
            geometry=[LineString([(15, -5), (15, 5)])],
            crs="EPSG:5186",
        )
        result = match_routes_to_osm(routes, sidewalks)
        self.assertTrue(result.empty)


if __name__ == "__main__":
    unittest.main()
