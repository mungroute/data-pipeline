from __future__ import annotations

from dataclasses import dataclass

import numpy as np


STEFAN_BOLTZMANN = 5.670374419e-8


@dataclass(frozen=True)
class ThermalParameters:
    """D5 정상상태 노면 열수지에 필요한 물성·환경 입력이다."""

    albedo: float
    emissivity: float
    ground_flux_ratio: float
    air_temp_c: float
    wind_m_s: float
    solar_w_m2: float
    svf: float = 1.0
    sky_offset_c: float = 10.0
    park_proximity_m: float = 9999.0


def convection_coefficient(wind_m_s: np.ndarray | float) -> np.ndarray:
    """도시 외부 표면에 쓰는 선형 대류 열전달계수(W/m²K)를 계산한다."""
    return 5.7 + 3.8 * np.maximum(np.asarray(wind_m_s, dtype=float), 0.0)


def energy_balance_residual(surface_temp_c: np.ndarray, *, albedo: np.ndarray,
                            emissivity: np.ndarray, ground_flux_ratio: np.ndarray,
                            air_temp_c: np.ndarray, wind_m_s: np.ndarray,
                            solar_w_m2: np.ndarray, svf: np.ndarray,
                            sky_offset_c: float = 10.0) -> np.ndarray:
    """흡수 단파에서 지중·대류·장파 손실을 뺀 열수지 잔차를 반환한다."""
    surface_k = surface_temp_c + 273.15
    air_k = air_temp_c + 273.15
    sky_k = np.maximum(air_temp_c - sky_offset_c, -80.0) + 273.15
    absorbed = (1.0 - albedo) * np.maximum(solar_w_m2, 0.0)
    available = absorbed * (1.0 - ground_flux_ratio)
    convection = convection_coefficient(wind_m_s) * (surface_temp_c - air_temp_c)
    longwave = emissivity * STEFAN_BOLTZMANN * (
        svf * (surface_k**4 - sky_k**4) + (1.0 - svf) * (surface_k**4 - air_k**4)
    )
    return available - convection - longwave


def solve_surface_temperature_array(*, albedo: np.ndarray, emissivity: np.ndarray,
                                    ground_flux_ratio: np.ndarray, air_temp_c: np.ndarray,
                                    wind_m_s: np.ndarray, solar_w_m2: np.ndarray,
                                    svf: np.ndarray, park_proximity_m: np.ndarray,
                                    park_cooling_max_c: float = 1.5,
                                    park_cooling_distance_m: float = 200.0) -> np.ndarray:
    """벡터화 이분법으로 정상상태 노면온도를 풀고 200m 공원 냉각항을 적용한다."""
    arrays = [np.asarray(value, dtype=float) for value in (
        albedo, emissivity, ground_flux_ratio, air_temp_c, wind_m_s,
        solar_w_m2, svf, park_proximity_m,
    )]
    albedo, emissivity, ground_flux_ratio, air_temp_c, wind_m_s, solar_w_m2, svf, park_proximity_m = np.broadcast_arrays(*arrays)
    if np.any((albedo < 0) | (albedo > 1)) or np.any((emissivity <= 0) | (emissivity > 1)):
        raise ValueError("알베도와 방사율 범위를 벗어났습니다.")
    svf = np.clip(svf, 0.0, 1.0)
    low = air_temp_c - 30.0
    high = air_temp_c + 90.0
    for _ in range(64):
        mid = (low + high) / 2.0
        residual = energy_balance_residual(
            mid, albedo=albedo, emissivity=emissivity,
            ground_flux_ratio=ground_flux_ratio, air_temp_c=air_temp_c,
            wind_m_s=wind_m_s, solar_w_m2=solar_w_m2, svf=svf,
        )
        low = np.where(residual > 0, mid, low)
        high = np.where(residual > 0, high, mid)
    solved = (low + high) / 2.0
    cooling = park_cooling_max_c * np.clip(
        1.0 - np.maximum(park_proximity_m, 0.0) / park_cooling_distance_m, 0.0, 1.0
    )
    return solved - cooling


def solve_surface_temperature(params: ThermalParameters) -> float:
    """단일 지점 계산을 위한 읽기 쉬운 래퍼 함수다."""
    result = solve_surface_temperature_array(
        albedo=np.asarray([params.albedo]), emissivity=np.asarray([params.emissivity]),
        ground_flux_ratio=np.asarray([params.ground_flux_ratio]),
        air_temp_c=np.asarray([params.air_temp_c]), wind_m_s=np.asarray([params.wind_m_s]),
        solar_w_m2=np.asarray([params.solar_w_m2]), svf=np.asarray([params.svf]),
        park_proximity_m=np.asarray([params.park_proximity_m]),
    )
    return float(result[0])
