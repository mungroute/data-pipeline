from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import numpy as np


PIPELINE_ROOT = Path(__file__).resolve().parents[1]
D4_OUTPUT_DIR = PIPELINE_ROOT / "data" / "processed" / "d4"
D4_QA_DIR = PIPELINE_ROOT / "data" / "interim" / "surface"

EXPECTED_SAMPLE_COUNT = 31_167
EXPECTED_SEGMENT_COUNT = 7_766


@dataclass(frozen=True)
class SurfaceProperties:
    """D4/D5에서 공통으로 사용하는 노면 물성의 기준값이다."""

    albedo: float
    emissivity: float
    ground_flux_ratio: float


SURFACE_PROPERTIES = {
    "asphalt": SurfaceProperties(0.12, 0.95, 0.30),
    "pavement": SurfaceProperties(0.28, 0.92, 0.30),
    "soil": SurfaceProperties(0.20, 0.94, 0.25),
    "grass": SurfaceProperties(0.23, 0.97, 0.15),
}


def sample_support_weights(chainages: Iterable[float], length_m: float) -> np.ndarray:
    """등간격 점이 담당하는 실제 선 길이를 Voronoi/사다리꼴 방식으로 계산한다.

    단순 평균은 짧은 마지막 간격과 양 끝점을 다른 점과 같은 비중으로 세므로,
    각 점 좌우의 중간점까지를 그 점의 대표 길이로 사용한다.
    """
    values = np.asarray(list(chainages), dtype=float)
    if length_m <= 0:
        raise ValueError("링크 길이는 0보다 커야 합니다.")
    if values.size == 0:
        raise ValueError("가중치를 계산할 샘플점이 없습니다.")
    if values.size == 1:
        return np.asarray([length_m], dtype=float)
    if np.any(np.diff(values) < -1e-8):
        raise ValueError("chainage는 오름차순이어야 합니다.")

    clipped = np.clip(values, 0.0, float(length_m))
    boundaries = np.empty(values.size + 1, dtype=float)
    boundaries[0] = 0.0
    boundaries[-1] = float(length_m)
    boundaries[1:-1] = (clipped[:-1] + clipped[1:]) / 2.0
    weights = np.diff(boundaries)
    if np.any(weights < -1e-8) or not np.isclose(weights.sum(), length_m):
        raise ValueError("샘플 대표 길이 계산이 비정상입니다.")
    return np.maximum(weights, 0.0)


def weighted_mode(values: Iterable[str], weights: Iterable[float]) -> str:
    """가중 최빈값을 결정하고 동률이면 보수적인 열부하 순서를 적용한다."""
    totals: dict[str, float] = {}
    for value, weight in zip(values, weights, strict=True):
        totals[value] = totals.get(value, 0.0) + float(weight)
    if not totals:
        raise ValueError("최빈값을 계산할 값이 없습니다.")
    # 동률일 때 더 뜨거워질 가능성이 큰 포장을 택해 과소평가를 피한다.
    tie_priority = {"asphalt": 0, "pavement": 1, "soil": 2, "grass": 3}
    return min(totals, key=lambda value: (-totals[value], tie_priority[value]))


def weighted_mean(values: Iterable[float], weights: Iterable[float]) -> float:
    """링크 길이를 보존하는 가중평균을 반환한다."""
    value_array = np.asarray(list(values), dtype=float)
    weight_array = np.asarray(list(weights), dtype=float)
    if value_array.size == 0 or value_array.size != weight_array.size:
        raise ValueError("가중평균 입력 길이가 올바르지 않습니다.")
    if weight_array.sum() <= 0:
        return float(value_array.mean())
    return float(np.average(value_array, weights=weight_array))


def ensure_expected_count(actual: int, expected: int, label: str) -> None:
    """입력 누락이나 다른 DB 연결을 조기에 차단한다."""
    if actual != expected:
        raise ValueError(f"{label} 수가 다릅니다: {actual:,} / 예상 {expected:,}")
