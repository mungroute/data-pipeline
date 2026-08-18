import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from thermal_model import (
    ThermalParameters,
    solve_surface_temperature,
    solve_surface_temperature_array,
)


class ThermalModelTests(unittest.TestCase):
    def base(self, **overrides) -> ThermalParameters:
        values = dict(albedo=0.12, emissivity=0.95, ground_flux_ratio=0.30,
                      air_temp_c=32.0, wind_m_s=2.0, solar_w_m2=800.0,
                      svf=0.8, park_proximity_m=9999.0)
        values.update(overrides)
        return ThermalParameters(**values)

    def test_more_solar_is_hotter(self) -> None:
        self.assertGreater(solve_surface_temperature(self.base(solar_w_m2=800)),
                           solve_surface_temperature(self.base(solar_w_m2=100)))

    def test_higher_albedo_is_cooler(self) -> None:
        self.assertLess(solve_surface_temperature(self.base(albedo=0.35)),
                        solve_surface_temperature(self.base(albedo=0.10)))

    def test_more_wind_is_cooler_under_sun(self) -> None:
        self.assertLess(solve_surface_temperature(self.base(wind_m_s=5.0)),
                        solve_surface_temperature(self.base(wind_m_s=0.5)))

    def test_low_svf_reduces_longwave_cooling(self) -> None:
        self.assertGreater(solve_surface_temperature(self.base(svf=0.1)),
                           solve_surface_temperature(self.base(svf=1.0)))

    def test_nearby_park_applies_cooling(self) -> None:
        self.assertLess(solve_surface_temperature(self.base(park_proximity_m=0.0)),
                        solve_surface_temperature(self.base(park_proximity_m=500.0)))

    def test_shade_is_cooler_than_direct_sun(self) -> None:
        self.assertLess(solve_surface_temperature(self.base(solar_w_m2=800.0 * 0.461)),
                        solve_surface_temperature(self.base(solar_w_m2=800.0)))

    def test_park_cooling_is_capped_at_one_point_five_celsius(self) -> None:
        nearby = solve_surface_temperature(self.base(park_proximity_m=0.0))
        distant = solve_surface_temperature(self.base(park_proximity_m=1000.0))
        self.assertAlmostEqual(distant - nearby, 1.5, places=6)

    def test_array_solution_is_finite(self) -> None:
        result = solve_surface_temperature_array(
            albedo=np.array([0.12, 0.28]), emissivity=np.array([0.95, 0.92]),
            ground_flux_ratio=np.array([0.30, 0.30]), air_temp_c=np.array([30.0, 32.0]),
            wind_m_s=np.array([1.0, 3.0]), solar_w_m2=np.array([0.0, 900.0]),
            svf=np.array([0.0, 1.0]), park_proximity_m=np.array([0.0, 1000.0]),
        )
        self.assertTrue(np.all(np.isfinite(result)))

    def test_invalid_ground_flux_ratio_fails_fast(self) -> None:
        with self.assertRaises(ValueError):
            solve_surface_temperature(self.base(ground_flux_ratio=1.1))

    def test_non_finite_input_fails_fast(self) -> None:
        with self.assertRaises(ValueError):
            solve_surface_temperature(self.base(solar_w_m2=float("nan")))


if __name__ == "__main__":
    unittest.main()
