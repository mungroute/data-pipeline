from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd


PIPELINE_ROOT = Path(__file__).resolve().parents[1]
RAW_DIR = PIPELINE_ROOT / "data" / "raw"
OUTPUT_DIR = PIPELINE_ROOT / "data" / "processed" / "d5"
REPORT_PATH = PIPELINE_ROOT / "reports" / "d5_input_qa.md"
ACTUAL_WORKBOOK = RAW_DIR / "field_measurement" / "field_measurement_actual_round1.xlsx"
SYNTHETIC_WORKBOOK = RAW_DIR / "field_measurement" / "field_measurement_with_synthetic_round2.xlsx"
ASOS_DIR = RAW_DIR / "weather" / "asos"
SDOT_PATH = RAW_DIR / "weather" / "sdot" / "seoul_sdot_environment.csv"
MEASUREMENT_DATE = pd.Timestamp("2026-08-11")
REQUIRED_HOURS = (9, 12, 15, 18)


def parse_arguments() -> argparse.Namespace:
    """D5 입력 전처리 경로를 명령행에서 덮어쓸 수 있게 한다."""
    parser = argparse.ArgumentParser(description="D5 실측·ASOS·S-DoT 입력 전처리")
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    parser.add_argument("--report", type=Path, default=REPORT_PATH)
    return parser.parse_args()


def excel_time_to_hour(value: object) -> int:
    """엑셀 시간값, 문자열 또는 datetime을 정시의 hour로 변환한다."""
    if isinstance(value, (int, float, np.number)):
        return int(round(float(value) * 24)) % 24
    parsed = pd.to_datetime(str(value), errors="coerce")
    if pd.isna(parsed):
        raise ValueError(f"시간을 해석할 수 없습니다: {value!r}")
    return int(parsed.hour)


def load_actual_measurements(path: Path) -> pd.DataFrame:
    """1차 본측정 시트에서 실제 측정 12쌍과 반복 측정 품질지표를 추출한다."""
    raw = pd.read_excel(path, sheet_name="1차_본측정", header=None)
    records: list[dict[str, object]] = []
    for _, row in raw.iloc[6:18].iterrows():
        if pd.isna(row.iloc[0]):
            continue
        sun = pd.to_numeric(row.iloc[6:11], errors="coerce").dropna().to_numpy(float)
        shade = pd.to_numeric(row.iloc[12:17], errors="coerce").dropna().to_numpy(float)
        hour = excel_time_to_hour(row.iloc[5])
        records.append({
            "measurement_id": f"ACTUAL-R1-{int(row.iloc[0]):02d}",
            "data_status": "ACTUAL",
            "calibration_eligible": True,
            "measured_at": MEASUREMENT_DATE + pd.Timedelta(hours=hour),
            "weather_date_used": MEASUREMENT_DATE.date().isoformat(),
            "weather_status": "OBSERVED_ASOS_SAME_DATE",
            "material": str(row.iloc[1]).strip().lower(),
            "location_note": str(row.iloc[2]).strip(),
            "latitude": float(row.iloc[3]),
            "longitude": float(row.iloc[4]),
            "hour": hour,
            "sun_mean_c": float(sun.mean()),
            "shade_mean_c": float(shade.mean()),
            "delta_t_c": float(sun.mean() - shade.mean()),
            "sun_sd_c": float(sun.std(ddof=1)),
            "shade_sd_c": float(shade.std(ddof=1)),
            "sun_range_c": float(sun.max() - sun.min()),
            "shade_range_c": float(shade.max() - shade.min()),
            "replicate_count": min(len(sun), len(shade)),
            "shadow_source": str(row.iloc[19]).strip(),
            "distance_cm": float(row.iloc[20]),
        })
    frame = pd.DataFrame(records)
    frame["quality_flag"] = np.where(
        (frame[["sun_sd_c", "shade_sd_c"]].max(axis=1) > 2.0)
        | (frame[["sun_range_c", "shade_range_c"]].max(axis=1) > 5.0),
        "HIGH_REPLICATE_SPREAD",
        "OK",
    )
    return frame


def load_synthetic_measurements(path: Path) -> pd.DataFrame:
    """모의 2차 시트를 동적 테스트용 레코드로 읽되 보정 사용을 강제로 금지한다."""
    raw = pd.read_excel(path, sheet_name="2차_시각대비", header=None)
    records: list[dict[str, object]] = []
    for _, row in raw.iloc[5:17].iterrows():
        if pd.isna(row.iloc[0]):
            continue
        hour = excel_time_to_hour(row.iloc[3])
        records.append({
            "measurement_id": f"SYNTHETIC-R2-{int(row.iloc[0]):02d}-{hour:02d}",
            "data_status": "SYNTHETIC",
            "calibration_eligible": False,
            "measured_at": MEASUREMENT_DATE + pd.Timedelta(hours=hour),
            "weather_date_used": MEASUREMENT_DATE.date().isoformat(),
            "weather_status": "OBSERVED_ASOS_SAME_DATE",
            "material": str(row.iloc[1]).strip().lower(),
            "location_note": str(row.iloc[2]).strip(),
            "latitude": np.nan,
            "longitude": np.nan,
            "hour": hour,
            "sun_mean_c": float(row.iloc[4]),
            "shade_mean_c": float(row.iloc[5]),
            "delta_t_c": float(row.iloc[6]),
            "sun_sd_c": np.nan,
            "shade_sd_c": np.nan,
            "sun_range_c": np.nan,
            "shade_range_c": np.nan,
            "replicate_count": 0,
            "shadow_source": "SYNTHETIC",
            "distance_cm": 50.0,
            "quality_flag": "SYNTHETIC_DO_NOT_VALIDATE",
        })
    return pd.DataFrame(records)


def read_latest_asos() -> tuple[pd.DataFrame, Path]:
    """ASOS 폴더에서 시간 범위가 가장 긴 파일을 선택하고 표준 열로 정규화한다."""
    candidates = sorted(ASOS_DIR.glob("*.csv"))
    if not candidates:
        raise FileNotFoundError(f"ASOS CSV가 없습니다: {ASOS_DIR}")
    best: tuple[pd.Timestamp, pd.DataFrame, Path] | None = None
    for path in candidates:
        frame = pd.read_csv(path, encoding="cp949")
        frame["observed_at"] = pd.to_datetime(frame["일시"], errors="raise")
        end = frame["observed_at"].max()
        if best is None or end > best[0]:
            best = end, frame, path
    assert best is not None
    frame, path = best[1], best[2]
    normalized = pd.DataFrame({
        "station_id": frame["지점"].astype(int),
        "station_name": frame["지점명"].astype(str),
        "observed_at": frame["observed_at"],
        "air_temp_c": pd.to_numeric(frame["기온(°C)"], errors="coerce"),
        "precip_mm": pd.to_numeric(frame["강수량(mm)"], errors="coerce").fillna(0.0),
        "wind_m_s": pd.to_numeric(frame["풍속(m/s)"], errors="coerce"),
        "humidity_pct": pd.to_numeric(frame["습도(%)"], errors="coerce"),
        "solar_mj_m2": pd.to_numeric(frame["일사(MJ/m2)"], errors="coerce"),
        "cloud_tenths": pd.to_numeric(frame["전운량(10분위)"], errors="coerce"),
        "ground_temp_c": pd.to_numeric(frame["지면온도(°C)"], errors="coerce"),
    })
    normalized["solar_w_m2"] = normalized["solar_mj_m2"] * (1_000_000 / 3_600)
    return normalized, path


def load_sdot(path: Path) -> pd.DataFrame:
    """S-DoT 중구 관측을 시간별 도시 미기후 참고값으로 집계한다."""
    raw = pd.read_csv(path, encoding="cp949", low_memory=False)
    raw["observed_at"] = pd.to_datetime(raw["측정시간"], format="%Y-%m-%d_%H:%M:%S", errors="coerce")
    raw["air_temp_c"] = pd.to_numeric(raw["온도 평균(℃)"], errors="coerce")
    raw["wind_m_s"] = pd.to_numeric(raw["풍속 평균(m/s)"], errors="coerce")
    raw["humidity_pct"] = pd.to_numeric(raw["습도 평균(%)"], errors="coerce")
    district = raw["자치구"].astype(object).map(lambda value: str(value).strip().lower())
    subset = raw[district.isin({"중구", "jung-gu"}) & raw["observed_at"].notna()].copy()
    subset["observed_hour"] = subset["observed_at"].dt.floor("h")
    return subset.groupby("observed_hour", as_index=False).agg(
        air_temp_c=("air_temp_c", "median"),
        wind_m_s=("wind_m_s", "median"),
        humidity_pct=("humidity_pct", "median"),
        sensor_count=("시리얼", "nunique"),
    )


def select_measurement_weather(asos: pd.DataFrame) -> pd.DataFrame:
    """실측일인 8월 11일의 09·12·15·18시 ASOS 관측값을 선택한다."""
    selected = asos[
        (asos["observed_at"].dt.normalize() == MEASUREMENT_DATE)
        & asos["observed_at"].dt.hour.isin(REQUIRED_HOURS)
    ].copy()
    if set(selected["observed_at"].dt.hour) != set(REQUIRED_HOURS):
        raise ValueError("실측일 2026-08-11의 09·12·15·18시 ASOS가 완전하지 않습니다.")
    required = ["air_temp_c", "wind_m_s", "solar_w_m2", "cloud_tenths"]
    if selected[required].isna().any().any():
        raise ValueError("실측일 ASOS 필수 기상값에 결측이 있습니다.")
    selected["hour"] = selected["observed_at"].dt.hour
    selected["weather_status"] = "OBSERVED_ASOS_SAME_DATE"
    return selected.sort_values("hour")


def write_report(actual: pd.DataFrame, synthetic: pd.DataFrame, asos: pd.DataFrame,
                 weather: pd.DataFrame, sdot: pd.DataFrame, source: Path, path: Path) -> None:
    """입력 데이터 계보, 범위, 결측과 실측일 기상값을 Markdown으로 기록한다."""
    spread = int((actual["quality_flag"] != "OK").sum())
    weather_rows = "\n".join(
        f"- {int(row.hour):02d}시: 기온 {row.air_temp_c:.1f}°C, 풍속 {row.wind_m_s:.1f}m/s, "
        f"일사 {row.solar_w_m2:.1f}W/m², 전운량 {row.cloud_tenths:.0f}/10, 강수 {row.precip_mm:.1f}mm"
        for row in weather.itertuples()
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"""# D5 입력 전처리 QA

## 데이터 계보

- 실제 1차 측정: {len(actual):,}쌍, 측정일 2026-08-11, 보정·검증 사용 가능
- 모의 2차 측정: {len(synthetic):,}행, `SYNTHETIC`, 파서·화면·단위 테스트에만 사용
- ASOS 원본: `{source.name}`, {asos['observed_at'].min()} ~ {asos['observed_at'].max()}
- S-DoT 중구 시간 집계: {len(sdot):,}시간

## 기상 결합 정책

- 실측일과 동일한 **2026-08-11** 서울 ASOS 108번 관측값을 사용한다.
- 필요한 09·12·15·18시의 기온·풍속·일사·전운량이 모두 존재한다.
- 기상 상태는 `OBSERVED_ASOS_SAME_DATE`이며, 이전의 8월 8일 `METEO_PROXY`는 더 이상 사용하지 않는다.
{weather_rows}

## 실측 품질

- 반복 5회 범위 5°C 초과 또는 표준편차 2°C 초과: {spread:,}/{len(actual):,}쌍
- 품질 플래그는 가중치에만 반영하며 실제 측정행을 자동 삭제하지 않는다.
- 실측 위치는 서비스 대상지 밖의 회사 주변 보정 지점이다. 근거리 양지·그늘 쌍의 재질별 열반응 보정에는 사용하되, 중구 공간 전이는 QGIS QA에서 별도로 검증한다.
""", encoding="utf-8")


def main() -> None:
    """D5 단계 1~2 산출물을 생성한다."""
    args = parse_arguments()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    actual = load_actual_measurements(ACTUAL_WORKBOOK)
    synthetic = load_synthetic_measurements(SYNTHETIC_WORKBOOK)
    asos, asos_source = read_latest_asos()
    weather = select_measurement_weather(asos)
    sdot = load_sdot(SDOT_PATH)
    actual.to_csv(args.output_dir / "field_measurements_actual.csv", index=False, encoding="utf-8-sig")
    synthetic.to_csv(args.output_dir / "field_measurements_synthetic.csv", index=False, encoding="utf-8-sig")
    asos.to_csv(args.output_dir / "weather_asos_hourly.csv", index=False, encoding="utf-8-sig")
    sdot.to_csv(args.output_dir / "weather_sdot_junggu_hourly.csv", index=False, encoding="utf-8-sig")
    weather.to_csv(args.output_dir / "weather_observed_20260811.csv", index=False, encoding="utf-8-sig")
    write_report(actual, synthetic, asos, weather, sdot, asos_source, args.report)
    print(f"[D5 입력 완료] 실제 {len(actual):,}, 모의 {len(synthetic):,}, ASOS {len(asos):,}, S-DoT {len(sdot):,}")
    print("[기상 정책] 실측일 2026-08-11 / 동일 날짜 서울 ASOS 관측값")


if __name__ == "__main__":
    main()
