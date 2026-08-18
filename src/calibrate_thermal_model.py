from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

from d4_common import SURFACE_PROPERTIES
from thermal_model import solve_surface_temperature_array


PIPELINE_ROOT = Path(__file__).resolve().parents[1]
D5_DIR = PIPELINE_ROOT / "data" / "processed" / "d5"
DEFAULT_CONFIG = PIPELINE_ROOT / "config" / "d5_thermal_model.json"
DEFAULT_REPORT = PIPELINE_ROOT / "reports" / "d5_calibration_qa.md"


def parse_arguments() -> argparse.Namespace:
    """보정 입력과 산출물 경로를 받는다."""
    parser = argparse.ArgumentParser(description="D5 그늘 일사 감쇠계수 보정")
    parser.add_argument("--actual", type=Path, default=D5_DIR / "field_measurements_actual.csv")
    parser.add_argument("--synthetic", type=Path, default=D5_DIR / "field_measurements_synthetic.csv")
    parser.add_argument("--weather", type=Path, default=D5_DIR / "weather_observed_20260811.csv")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    return parser.parse_args()


def huber_loss(residual: np.ndarray, delta: float = 3.0) -> np.ndarray:
    """큰 반복측정 오차 한두 건이 보정을 지배하지 않도록 Huber 손실을 쓴다."""
    absolute = np.abs(residual)
    return np.where(absolute <= delta, 0.5 * residual**2, delta * (absolute - 0.5 * delta))


def attach_inputs(measurements: pd.DataFrame, weather: pd.DataFrame) -> pd.DataFrame:
    """재질 물성과 실측일의 시간별 ASOS 기상을 실제 측정쌍에 결합한다."""
    frame = measurements.copy()
    frame["calibration_eligible"] = frame["calibration_eligible"].astype(str).str.lower().eq("true")
    if not frame["calibration_eligible"].all() or not (frame["data_status"] == "ACTUAL").all():
        raise ValueError("보정 입력에는 ACTUAL이면서 calibration_eligible인 행만 허용됩니다.")
    frame = frame.merge(
        weather[["hour", "air_temp_c", "wind_m_s", "solar_w_m2"]],
        on="hour", validate="many_to_one",
    )
    for field in ("albedo", "emissivity", "ground_flux_ratio"):
        frame[field] = frame["material"].map(lambda name: getattr(SURFACE_PROPERTIES[str(name)], field))
    if frame[["air_temp_c", "wind_m_s", "solar_w_m2", "albedo", "emissivity", "ground_flux_ratio"]].isna().any().any():
        raise ValueError("보정 입력 결합 후 필수값에 결측이 있습니다.")
    return frame


def predict_pair(frame: pd.DataFrame, transmission: float) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """한 감쇠계수에서 양지·그늘 온도와 두 온도의 차이를 계산한다."""
    common = dict(
        albedo=frame["albedo"].to_numpy(float),
        emissivity=frame["emissivity"].to_numpy(float),
        ground_flux_ratio=frame["ground_flux_ratio"].to_numpy(float),
        air_temp_c=frame["air_temp_c"].to_numpy(float),
        wind_m_s=frame["wind_m_s"].to_numpy(float),
        svf=np.full(len(frame), 0.8),
        park_proximity_m=np.full(len(frame), 9999.0),
    )
    sun = solve_surface_temperature_array(solar_w_m2=frame["solar_w_m2"].to_numpy(float), **common)
    shade = solve_surface_temperature_array(
        solar_w_m2=frame["solar_w_m2"].to_numpy(float) * transmission, **common
    )
    return sun, shade, sun - shade


def fit_transmission(frame: pd.DataFrame) -> float:
    """0.05~0.50 구간의 강건 격자탐색으로 유효 그늘 일사 투과율을 정한다."""
    observed = frame["delta_t_c"].to_numpy(float)
    spread = frame[["sun_sd_c", "shade_sd_c"]].max(axis=1).fillna(2.0).to_numpy(float)
    weights = 1.0 / (1.0 + spread)
    candidates = np.linspace(0.05, 0.50, 901)
    losses: list[float] = []
    for value in candidates:
        _, _, predicted = predict_pair(frame, float(value))
        losses.append(float(np.average(huber_loss(predicted - observed), weights=weights)))
    return float(candidates[int(np.argmin(losses))])


def leave_one_hour_out(frame: pd.DataFrame) -> pd.DataFrame:
    """네 시각 중 하나씩 제외해 감쇠계수의 시간대 외삽 안정성을 확인한다."""
    rows: list[dict[str, float]] = []
    for hour in sorted(frame["hour"].unique()):
        train = frame[frame["hour"] != hour]
        test = frame[frame["hour"] == hour]
        fitted = fit_transmission(train)
        _, _, predicted = predict_pair(test, fitted)
        residual = predicted - test["delta_t_c"].to_numpy(float)
        rows.append({
            "held_out_hour": int(hour),
            "fitted_transmission": fitted,
            "mae_delta_c": float(np.mean(np.abs(residual))),
            "rmse_delta_c": float(np.sqrt(np.mean(residual**2))),
        })
    return pd.DataFrame(rows)


def main() -> None:
    """실제 1차 측정만 사용해 보정하고 설정·QA 보고서를 저장한다."""
    args = parse_arguments()
    actual = pd.read_csv(args.actual, encoding="utf-8-sig")
    synthetic = pd.read_csv(args.synthetic, encoding="utf-8-sig")
    if synthetic["calibration_eligible"].astype(str).str.lower().eq("true").any():
        raise ValueError("SYNTHETIC 행이 보정 가능 상태로 표시되었습니다.")
    weather = pd.read_csv(args.weather, encoding="utf-8-sig")
    frame = attach_inputs(actual, weather)
    transmission = fit_transmission(frame)
    sun, shade, predicted_delta = predict_pair(frame, transmission)
    frame["model_sun_c"] = sun
    frame["model_shade_c"] = shade
    frame["model_delta_c"] = predicted_delta
    frame["delta_residual_c"] = predicted_delta - frame["delta_t_c"]
    delta_mae = float(frame["delta_residual_c"].abs().mean())
    delta_rmse = float(np.sqrt(np.mean(frame["delta_residual_c"] ** 2)))
    absolute_residual = np.concatenate([
        sun - frame["sun_mean_c"].to_numpy(float),
        shade - frame["shade_mean_c"].to_numpy(float),
    ])
    absolute_rmse = float(np.sqrt(np.mean(absolute_residual**2)))
    cv = leave_one_hour_out(frame)

    config = {
        "model_version": "D5-BASELINE-1",
        "measurement_date": "2026-08-11",
        "weather_observed_date": "2026-08-11",
        "weather_station_id": 108,
        "weather_station_name": "서울",
        "weather_status": "OBSERVED_ASOS_SAME_DATE",
        "shade_solar_transmission": round(transmission, 4),
        "sky_offset_c": 10.0,
        "park_cooling_max_c": 1.5,
        "park_cooling_distance_m": 200.0,
        "calibration_rows_actual": int(len(frame)),
        "calibration_rows_synthetic": 0,
        "delta_mae_c": round(delta_mae, 3),
        "delta_rmse_c": round(delta_rmse, 3),
        "absolute_rmse_c_exploratory": round(absolute_rmse, 3),
        "confidence": "LOW",
        "limitations": [
            "서울 ASOS 108번은 실측일과 같지만 회사 주변 실측 지점의 현장 미기후와 차이가 있을 수 있음",
            "서비스 대상지 밖의 단일 보정 지점이며 표본은 12쌍뿐임",
            "일부 반복 측정쌍의 분산이 커 품질 가중치를 적용함",
            "모의 2차 데이터는 보정·검증에서 제외",
        ],
    }
    args.config.parent.mkdir(parents=True, exist_ok=True)
    args.config.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    frame.to_csv(D5_DIR / "calibration_predictions_actual.csv", index=False, encoding="utf-8-sig")
    cv.to_csv(D5_DIR / "calibration_leave_one_hour_out.csv", index=False, encoding="utf-8-sig")
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(f"""# D5 열모델 보정 QA

## 확정값

- 유효 그늘 일사 투과율: **{transmission:.3f}**
- 사용 실측: 실제 1차 {len(frame)}쌍
- 모의 2차 사용: **0행**
- ΔT MAE / RMSE: {delta_mae:.2f}°C / {delta_rmse:.2f}°C
- 절대온도 RMSE: {absolute_rmse:.2f}°C (동일 날짜 ASOS 결합 기준)
- 전체 신뢰도: **LOW**

## 검증 방식

- 알베도·방사율·지중열비는 D4 기준값을 고정했다.
- 0.05~0.50 범위에서 실제 양지-그늘 ΔT에 대한 가중 Huber 손실이 최소인 감쇠계수를 선택했다.
- 반복측정 분산이 큰 쌍은 삭제하지 않고 낮은 가중치를 부여했다.
- 시각별 leave-one-hour-out 평균 MAE: {cv['mae_delta_c'].mean():.2f}°C

## 제한

- 실측일과 동일한 8월 11일 서울 ASOS 108번의 09·12·15·18시 관측값을 사용했다.
- 실측일 불일치 문제는 해소됐지만 관측소와 회사 주변 실측 지점의 공간 차이, 12쌍의 작은 표본, 반복값 편차가 남아 있어 신뢰도는 `LOW`를 유지한다.
""", encoding="utf-8")
    print(f"[D5 보정 완료] shade_solar_transmission={transmission:.3f}, ΔT RMSE={delta_rmse:.2f}°C")


if __name__ == "__main__":
    main()
