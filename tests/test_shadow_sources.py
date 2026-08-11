import unittest

import numpy as np

from calc_shadow_sources import (
    BUILDING_CODE,
    NODATA_CODE,
    NONE_CODE,
    TERRAIN_CODE,
    TREE_CODE,
    classify_shadow_sources,
    measure_artificial_shadow_length,
)


class ShadowSourceClassificationTest(unittest.TestCase):
    def test_assigns_one_exclusive_cause_and_preserves_total_shadow(self) -> None:
        terrain = np.array([[0, 1, 0, 0, 255]], dtype=np.uint8)
        terrain_building = np.array([[0, 1, 1, 0, 255]], dtype=np.uint8)
        total = np.array([[0, 1, 1, 1, 255]], dtype=np.uint8)

        actual = classify_shadow_sources(terrain, terrain_building, total)

        np.testing.assert_array_equal(
            actual,
            np.array(
                [[NONE_CODE, TERRAIN_CODE, BUILDING_CODE, TREE_CODE, NODATA_CODE]],
                dtype=np.uint8,
            ),
        )
        np.testing.assert_array_equal(actual != NONE_CODE, total != 0)

    def test_rejects_non_monotonic_shadow_masks(self) -> None:
        with self.assertRaisesRegex(ValueError, "부분집합"):
            classify_shadow_sources(
                np.array([[1]], dtype=np.uint8),
                np.array([[0]], dtype=np.uint8),
                np.array([[1]], dtype=np.uint8),
            )

    def test_fifty_metre_obstacle_shadow_is_within_two_cells(self) -> None:
        cases = (
            (42.533, 52.9),
            (74.136, 13.8),
            (55.770, 33.1),
            (20.490, 129.8),
        )
        for elevation, expected_length_m in cases:
            with self.subTest(elevation=elevation):
                measured = measure_artificial_shadow_length(elevation)
                self.assertLessEqual(abs(measured - expected_length_m), 4.0)


if __name__ == "__main__":
    unittest.main()
