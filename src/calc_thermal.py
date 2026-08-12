from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

from d4_common import EXPECTED_SAMPLE_COUNT, EXPECTED_SEGMENT_COUNT, ensure_expected_count, sample_support_weights
from finalize_d4 import build_integrated, validate_integrated
from thermal_model import solve_surface_temperature_array


PIPELINE_ROOT = Path(__file__).resolve().parents[1]
D5_DIR = PIPELINE_ROOT / "data" / "processed" / "d5"
DEFAULT_CONFIG = PIPELINE_ROOT / "config" / "d5_thermal_model.json"
DEFAULT_REPORT = PIPELINE_ROOT / "reports" / "d5_thermal_compute_qa.md"
HOURS = (9, 12, 15, 18)


def parse_arguments() -> argparse.Namespace:
    """전 구간 계산의 입력과 출력 경로를 받는다."""
    parser = argparse.ArgumentParser(description="D5 전 샘플·링크 노면온도 계산")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--weather", type=Path, default=D5_DIR / "weather_observed_20260811.csv")
    parser.add_argument("--sample-output", type=Path, default=D5_DIR / "sample_thermal.csv")
    parser.add_argument("--route-output", type=Path, default=D5_DIR / "route_thermal.csv")
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    return parser.parse_args()


def aggregate_routes(samples: pd.DataFrame) -> pd.DataFrame:
    """약 10m 샘플의 대표구간 길이를 이용해 링크별 온도를 길이가중 집계한다."""
    rows: list[dict[str, object]] = []
    for segment_id, group in samples.groupby("segment_id", sort=True):
        ordered = group.sort_values("chainage_m")
        length_m = float(ordered["length_m"].iloc[0])
        weights = sample_support_weights(ordered["chainage_m"].to_numpy(float), length_m)
        record: dict[str, object] = {
            "segment_id": int(segment_id),
            "length_m": round(length_m, 2),
            "sample_count": int(len(ordered)),
            "weather_status": "OBSERVED_ASOS_SAME_DATE",
            "model_confidence": "LOW",
        }
        for hour in HOURS:
            values = ordered[f"surface_temp_{hour:02d}_c"].to_numpy(float)
            record[f"surface_temp_{hour:02d}_c"] = round(float(np.average(values, weights=weights)), 2)
            record[f"surface_temp_{hour:02d}_max_c"] = round(float(values.max()), 2)
            record[f"shade_ratio_{hour:02d}"] = round(
                float(np.average(ordered[f"is_shaded_{hour:02d}"].astype(float), weights=weights)), 3
            )
        record["surface_temp_peak_c"] = max(record[f"surface_temp_{hour:02d}_c"] for hour in HOURS)
        rows.append(record)
    return pd.DataFrame(rows)


def main() -> None:
    """D4 확정 입력과 보정 설정으로 모든 샘플·링크 온도를 계산한다."""
    args = parse_arguments()
    config = json.loads(args.config.read_text(encoding="utf-8"))
    weather = pd.read_csv(args.weather, encoding="utf-8-sig").set_index("hour")
    if set(HOURS) - set(weather.index.astype(int)):
        raise ValueError("09·12·15·18시 기상 입력이 완전하지 않습니다.")
    samples, routes, _ = build_integrated()
    validate_integrated(samples, routes)
    ensure_expected_count(len(samples), EXPECTED_SAMPLE_COUNT, "D5 샘플")
    transmission = float(config["shade_solar_transmission"])

    for hour in HOURS:
        current = weather.loc[hour]
        shaded = samples[f"is_shaded_{hour:02d}"].astype(bool).to_numpy()
        solar = np.where(shaded, float(current["solar_w_m2"]) * transmission, float(current["solar_w_m2"]))
        temperature = solve_surface_temperature_array(
            albedo=samples["albedo"].to_numpy(float),
            emissivity=samples["emissivity"].to_numpy(float),
            ground_flux_ratio=samples["ground_flux_ratio"].to_numpy(float),
            air_temp_c=np.full(len(samples), float(current["air_temp_c"])),
            wind_m_s=np.full(len(samples), float(current["wind_m_s"])),
            solar_w_m2=solar,
            svf=samples["svf_effective"].to_numpy(float),
            park_proximity_m=samples["park_proximity_m"].to_numpy(float),
            park_cooling_max_c=float(config["park_cooling_max_c"]),
            park_cooling_distance_m=float(config["park_cooling_distance_m"]),
        )
        samples[f"surface_temp_{hour:02d}_c"] = np.round(temperature, 2)
        samples[f"excess_temp_{hour:02d}_c"] = np.round(temperature - float(current["air_temp_c"]), 2)
    samples["surface_temp_peak_c"] = samples[[f"surface_temp_{hour:02d}_c" for hour in HOURS]].max(axis=1)
    samples["weather_status"] = "OBSERVED_ASOS_SAME_DATE"
    samples["model_confidence"] = "LOW"

    route_results = aggregate_routes(samples)
    ensure_expected_count(len(route_results), EXPECTED_SEGMENT_COUNT, "D5 링크")
    if samples[[f"surface_temp_{hour:02d}_c" for hour in HOURS]].isna().any().any():
        raise ValueError("샘플 계산 결과에 NULL이 있습니다.")
    if route_results[[f"surface_temp_{hour:02d}_c" for hour in HOURS]].isna().any().any():
        raise ValueError("링크 계산 결과에 NULL이 있습니다.")

    sample_columns = [
        "sample_id", "segment_id", "seq", "chainage_m", "length_m", "surface_type",
        "albedo", "emissivity", "ground_flux_ratio", "svf_effective", "park_proximity_m",
        *[field for hour in HOURS for field in (
            f"is_shaded_{hour:02d}", f"shade_src_{hour:02d}",
            f"surface_temp_{hour:02d}_c", f"excess_temp_{hour:02d}_c",
        )],
        "surface_temp_peak_c", "surface_quality", "weather_status", "model_confidence",
    ]
    args.sample_output.parent.mkdir(parents=True, exist_ok=True)
    samples[sample_columns].to_csv(args.sample_output, index=False, encoding="utf-8-sig")
    route_results.to_csv(args.route_output, index=False, encoding="utf-8-sig")

    min_temp = float(samples[[f"surface_temp_{hour:02d}_c" for hour in HOURS]].min().min())
    max_temp = float(samples[[f"surface_temp_{hour:02d}_c" for hour in HOURS]].max().max())
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(f"""# D5 전 구간 열모델 계산 QA

## 결과

- 샘플: {len(samples):,}개
- 링크: {len(route_results):,}개
- 계산 시각: 09·12·15·18시
- 노면온도 전체 범위: {min_temp:.2f}~{max_temp:.2f}°C
- NULL 온도: 0개
- 링크 집계: 샘플 대표구간 길이 가중평균

## 입력 정책

- 기상: 2026-08-11 서울 ASOS 108번, `OBSERVED_ASOS_SAME_DATE`
- 그림자: D3의 09·12·15·18시 샘플 판정
- 공간 입력: D4의 SVF·재질 물성·공원거리
- 그늘 유효 일사 투과율: {transmission:.3f}
- 공원 냉각: 200m 이내 선형, 최대 {config['park_cooling_max_c']:.1f}°C
- 신뢰도: **LOW** (관측소와 실측 지점의 공간 차이, 적은 현장 표본 및 일부 반복값 편차 때문)

## 계산식 재검토 사항

- 정상상태 에너지수지로 흡수 단파 = 지중열 + 대류 + 장파 손실을 푼다.
- 낮은 SVF는 차가운 하늘로의 장파 방출을 줄이는 방향으로만 반영했다.
- 그림자와 SVF를 동일 효과로 중복 차감하지 않았다.
- 공원 냉각은 관측 보정 전의 보수적 상한이며 추가 실측 후 재보정 대상이다.
- D3 그림자와 기상은 모두 8월 11일 기준이므로 날짜 불일치에 따른 태양 고도 오차는 해소됐다.

이 산출물은 D5 5단계 계산 결과이며 QGIS 통합 QA 및 DB 반영 전 상태다.
""", encoding="utf-8")
    print(f"[D5 계산 완료] samples={len(samples):,}, routes={len(route_results):,}, temp={min_temp:.2f}~{max_temp:.2f}°C")


if __name__ == "__main__":
    main()
