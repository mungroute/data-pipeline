import sys
import unittest
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from classify_surface import apply_surface_overrides, classify_material, has_vehicle_access


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


if __name__ == "__main__":
    unittest.main()
