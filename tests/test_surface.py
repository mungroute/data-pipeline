import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from classify_surface import classify_material, has_vehicle_access


class SurfaceClassificationTests(unittest.TestCase):
    def test_vehicle_bit_position(self) -> None:
        self.assertFalse(has_vehicle_access("1000"))
        self.assertTrue(has_vehicle_access("1100"))
        self.assertTrue(has_vehicle_access("1111"))

    def test_natural_cover_overrides_link_type(self) -> None:
        self.assertEqual(classify_material("423", "1111"), ("grass", "LANDCOVER_NATURAL", "A"))
        self.assertEqual(classify_material("623", "1111"), ("soil", "LANDCOVER_NATURAL", "A"))

    def test_urban_vehicle_link_is_asphalt(self) -> None:
        self.assertEqual(classify_material("154", "1111"), ("asphalt", "LANDCOVER_LINKTYPE", "B"))

    def test_urban_pedestrian_only_link_is_pavement(self) -> None:
        self.assertEqual(classify_material("154", "1000"), ("pavement", "LANDCOVER_LINKTYPE", "B"))

    def test_unknown_is_downgraded_default(self) -> None:
        self.assertEqual(classify_material(None, "1000"), ("asphalt", "DEFAULT_ASPHALT", "D"))


if __name__ == "__main__":
    unittest.main()
