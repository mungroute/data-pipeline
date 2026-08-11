import unittest

import numpy as np

from sample_shadow import SampleResult, nearest_ground_cell, world_to_cell


class RasterSamplingTest(unittest.TestCase):
    def test_database_row_preserves_uncovered_nulls_and_boolean_contract(self) -> None:
        uncovered = SampleResult(
            1, 10, (None, None, None, None), False, None, None, False
        )
        shaded = SampleResult(2, 10, ("N", "R", "B", "T"), False, None, None, False)

        self.assertEqual(
            uncovered.database_row(),
            (1, None, None, None, None, None, None, None, None),
        )
        self.assertEqual(
            shaded.database_row(),
            (2, False, "N", True, "R", True, "B", True, "T"),
        )

    def test_world_coordinate_maps_to_north_up_raster_cell(self) -> None:
        transform = (100.0, 2.0, 0.0, 200.0, 0.0, -2.0)
        self.assertEqual(world_to_cell(transform, 105.0, 195.0), (2, 2))

    def test_nearest_ground_cell_uses_stable_row_then_column_tie_break(self) -> None:
        building = np.zeros((5, 5), dtype=bool)
        building[2, 2] = True
        valid = np.ones((5, 5), dtype=bool)

        self.assertEqual(
            nearest_ground_cell(building, valid, 2, 2, maximum_distance_cells=2),
            (1, 2, 1.0),
        )

    def test_nearest_ground_cell_respects_maximum_distance(self) -> None:
        building = np.ones((5, 5), dtype=bool)
        building[0, 0] = False
        valid = np.ones((5, 5), dtype=bool)

        with self.assertRaisesRegex(ValueError, "지면 셀"):
            nearest_ground_cell(building, valid, 2, 2, maximum_distance_cells=1)


if __name__ == "__main__":
    unittest.main()
