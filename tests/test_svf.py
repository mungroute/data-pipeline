import unittest

import numpy as np

from calc_svf import (
    POLICY_FORCE,
    RasterGrid,
    SvfResult,
    build_building_obstacle_surface,
    calculate_svf_at_cells,
    classify_qa_value,
    directional_offsets,
    nearest_ground_cell,
    summarize_results,
)


def make_grid(obstacle_surface: np.ndarray) -> RasterGrid:
    height, width = obstacle_surface.shape
    dtm = np.zeros_like(obstacle_surface, dtype=np.float32)
    valid = np.ones_like(obstacle_surface, dtype=bool)
    return RasterGrid(
        geotransform=(0.0, 1.0, 0.0, float(height), 0.0, -1.0),
        width=width,
        height=height,
        pixel_size_m=1.0,
        dtm=dtm,
        obstacle_surface=obstacle_surface.astype(np.float32),
        valid=valid,
        building=obstacle_surface > 0.1,
    )


class SvfCalculationTest(unittest.TestCase):
    def test_building_obstacle_surface_uses_only_dsm(self) -> None:
        dtm = np.asarray([[10.0, 10.0], [10.0, 10.0]], dtype=np.float32)
        dsm = np.asarray([[10.0, 25.0], [9.0, 10.0]], dtype=np.float32)
        valid = np.asarray([[True, True], [True, False]])

        surface = build_building_obstacle_surface(dtm, dsm, valid)

        self.assertEqual(float(surface[0, 0]), 10.0)
        self.assertEqual(float(surface[0, 1]), 25.0)
        self.assertEqual(float(surface[1, 0]), 10.0)
        self.assertTrue(np.isnan(surface[1, 1]))

    def test_directional_offsets_do_not_repeat_cells(self) -> None:
        for offsets in directional_offsets(36, 20):
            cells = [(row, column) for row, column, _ in offsets]
            self.assertEqual(len(cells), len(set(cells)))

    def test_open_flat_surface_has_svf_one(self) -> None:
        grid = make_grid(np.zeros((31, 31), dtype=np.float32))
        result = calculate_svf_at_cells(
            grid,
            np.asarray([15]),
            np.asarray([15]),
            direction_count=36,
            search_radius_m=10.0,
            observer_height_m=1.5,
        )
        self.assertAlmostEqual(float(result[0]), 1.0, places=6)

    def test_surrounding_obstacles_reduce_svf(self) -> None:
        surface = np.zeros((31, 31), dtype=np.float32)
        surface[12:19, 12] = 12.0
        surface[12:19, 18] = 12.0
        surface[12, 12:19] = 12.0
        surface[18, 12:19] = 12.0
        grid = make_grid(surface)
        result = calculate_svf_at_cells(
            grid,
            np.asarray([15]),
            np.asarray([15]),
            direction_count=36,
            search_radius_m=10.0,
            observer_height_m=1.5,
        )
        self.assertLess(float(result[0]), 0.2)
        self.assertGreaterEqual(float(result[0]), 0.0)

    def test_nearest_ground_cell_avoids_building(self) -> None:
        valid = np.ones((7, 7), dtype=bool)
        building = np.zeros((7, 7), dtype=bool)
        building[2:5, 2:5] = True
        row, column, distance = nearest_ground_cell(
            building, valid, row=3, column=3, maximum_distance_cells=3
        )
        self.assertFalse(bool(building[row, column]))
        self.assertAlmostEqual(distance, 2.0)

    def test_summary_distribution_covers_every_filled_result(self) -> None:
        results = [
            SvfResult(1, 1, 0.0, POLICY_FORCE, None),
            SvfResult(2, 1, 0.3, None, None),
            SvfResult(3, 2, 0.5, None, None),
            SvfResult(4, 2, 0.7, None, None),
            SvfResult(5, 3, 0.9, None, None),
            SvfResult(6, 3, None, None, None),
        ]
        statistics = summarize_results(results)
        self.assertEqual(statistics["filled"], 5)
        self.assertEqual(sum(statistics["distribution"].values()), 5)
        self.assertEqual(classify_qa_value(0.0, POLICY_FORCE), "COVERED_STRUCTURE")
        self.assertEqual(classify_qa_value(None, None), "NULL")


if __name__ == "__main__":
    unittest.main()
