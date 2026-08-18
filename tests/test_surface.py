import sys
import unittest
from pathlib import Path

import pandas as pd
import geopandas as gpd
from shapely.geometry import LineString

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from classify_surface import (
    apply_major_road_sidewalk_overrides,
    apply_surface_overrides,
    apply_tdata_surface_overrides,
    classify_material,
    has_vehicle_access,
    load_tdata_surface_overrides,
    identify_major_road_sidewalk_candidates,
)


class SurfaceClassificationTests(unittest.TestCase):
    def test_vehicle_bit_position(self) -> None:
        self.assertFalse(has_vehicle_access("1000"))
        self.assertTrue(has_vehicle_access("1100"))
        self.assertTrue(has_vehicle_access("1111"))

    def test_natural_cover_is_context_not_walking_surface(self) -> None:
        self.assertEqual(
            classify_material("423", "1111"),
            ("asphalt", "LANDCOVER_CONTEXT_LINKTYPE", "C"),
        )
        self.assertEqual(
            classify_material("623", "1000"),
            ("pavement", "LANDCOVER_CONTEXT_LINKTYPE", "C"),
        )

    def test_urban_vehicle_link_is_asphalt(self) -> None:
        self.assertEqual(classify_material("154", "1111"), ("asphalt", "LANDCOVER_LINKTYPE", "B"))

    def test_urban_pedestrian_only_link_is_pavement(self) -> None:
        self.assertEqual(classify_material("154", "1000"), ("pavement", "LANDCOVER_LINKTYPE", "B"))

    def test_unknown_is_downgraded_link_default(self) -> None:
        self.assertEqual(classify_material(None, "1000"), ("pavement", "DEFAULT_LINKTYPE", "D"))

    def test_manual_override_replaces_entire_segment(self) -> None:
        samples = pd.DataFrame(
            {
                "segment_id": [10, 10, 11],
                "surface_type": ["pavement", "pavement", "asphalt"],
                "albedo": [0.28, 0.28, 0.12],
                "emissivity": [0.92, 0.92, 0.95],
                "ground_flux_ratio": [0.30, 0.30, 0.30],
                "classification_basis": ["LANDCOVER_LINKTYPE"] * 3,
                "surface_quality": ["B"] * 3,
            }
        )
        overrides = pd.DataFrame(
            [{"segment_id": 10, "surface_type": "soil", "reason": "field", "verified_by": "qa"}]
        )
        result = apply_surface_overrides(samples, overrides)
        changed = result[result["segment_id"] == 10]
        self.assertTrue((changed["surface_type"] == "soil").all())
        self.assertTrue((changed["classification_basis"] == "MANUAL_VERIFIED").all())
        self.assertTrue((changed["surface_quality"] == "A").all())
        self.assertEqual(result.loc[result["segment_id"] == 11, "surface_type"].item(), "asphalt")

    def test_tdata_auto_pavement_replaces_only_selected_segment(self) -> None:
        samples = pd.DataFrame(
            {
                "segment_id": [10, 10, 11],
                "surface_type": ["asphalt", "asphalt", "asphalt"],
                "albedo": [0.12, 0.12, 0.12],
                "emissivity": [0.95, 0.95, 0.95],
                "ground_flux_ratio": [0.30, 0.30, 0.30],
                "classification_basis": ["LANDCOVER_LINKTYPE"] * 3,
                "surface_quality": ["B"] * 3,
            }
        )
        candidates = pd.DataFrame({"segment_id": [10]})
        result = apply_tdata_surface_overrides(samples, candidates)
        changed = result[result["segment_id"] == 10]
        self.assertTrue((changed["surface_type"] == "pavement").all())
        self.assertTrue((changed["classification_basis"] == "T_DATA_SAFE_SIDEWALK").all())
        self.assertTrue((changed["surface_quality"] == "B").all())
        self.assertEqual(result.loc[result["segment_id"] == 11, "surface_type"].item(), "asphalt")

    def test_tdata_loader_accepts_only_safe_auto_pavement(self) -> None:
        path = Path(self.id().replace(".", "_") + ".csv")
        try:
            pd.DataFrame(
                [
                    {
                        "segment_id": 10,
                        "safe_candidate_surface": "pavement",
                        "safe_sample_count": 2,
                        "safe_coverage_ratio": 0.6,
                        "safe_candidate_agreement_ratio": 1.0,
                        "correction_policy": "AUTO_PAVEMENT",
                    },
                    {
                        "segment_id": 11,
                        "safe_candidate_surface": "pavement",
                        "safe_sample_count": 1,
                        "safe_coverage_ratio": 0.4,
                        "safe_candidate_agreement_ratio": 1.0,
                        "correction_policy": "REVIEW_PARTIAL",
                    },
                ]
            ).to_csv(path, index=False, encoding="utf-8-sig")
            result = load_tdata_surface_overrides(path)
            self.assertEqual(result["segment_id"].tolist(), [10])
        finally:
            path.unlink(missing_ok=True)

    def test_second_stage_requires_parallel_pair_near_tdata_seed(self) -> None:
        samples = pd.DataFrame(
            {
                "segment_id": [100, 101, 102],
                "surface_type": ["asphalt", "pavement", "pavement"],
                "classification_basis": [
                    "LANDCOVER_LINKTYPE", "LANDCOVER_LINKTYPE", "T_DATA_SAFE_SIDEWALK"
                ],
            }
        )
        routes = gpd.GeoDataFrame(
            {
                "segment_id": [100, 101, 102],
                "source": [1, 3, 5],
                "target": [2, 4, 6],
                "length_m": [30.0, 30.0, 30.0],
                "link_type_code": ["1111", "1000", "1000"],
            },
            geometry=[
                LineString([(0, 0), (30, 0)]),
                LineString([(0, 20), (30, 20)]),
                LineString([(0, 5), (30, 5)]),
            ],
            crs="EPSG:5186",
        )
        candidates = identify_major_road_sidewalk_candidates(samples, routes)
        self.assertEqual(candidates["segment_id"].tolist(), [100])
        self.assertEqual(candidates["peer_segment_id"].tolist(), [101])
        self.assertEqual(candidates["tdata_seed_distance_m"].tolist(), [5.0])
        self.assertEqual(
            candidates["correction_policy"].tolist(),
            ["REVIEW_MAJOR_ROAD_CORRIDOR"],
        )

        classified = pd.DataFrame(
            {
                "segment_id": [100, 101],
                "surface_type": ["asphalt", "pavement"],
                "albedo": [0.12, 0.28],
                "emissivity": [0.95, 0.92],
                "ground_flux_ratio": [0.30, 0.30],
                "classification_basis": ["LANDCOVER_LINKTYPE"] * 2,
                "surface_quality": ["B"] * 2,
            }
        )
        annotated = apply_major_road_sidewalk_overrides(classified, candidates)
        target = annotated.loc[annotated["segment_id"].eq(100)].iloc[0]
        self.assertEqual(target["surface_type"], "asphalt")
        self.assertEqual(target["classification_basis"], "LANDCOVER_LINKTYPE")
        self.assertEqual(target["surface_quality"], "B")
        self.assertEqual(target["secondary_decision"], "REVIEW_MAJOR_ROAD_CORRIDOR")
        self.assertEqual(target["secondary_peer_segment_id"], 101)


if __name__ == "__main__":
    unittest.main()
