import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from thermal_model import ThermalParameters, solve_surface_temperature


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


if __name__ == "__main__":
    unittest.main()
