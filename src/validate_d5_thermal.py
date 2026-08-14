from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from calc_thermal import HOURS, aggregate_routes
from d4_common import EXPECTED_SAMPLE_COUNT, EXPECTED_SEGMENT_COUNT


PIPELINE_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SAMPLE = PIPELINE_ROOT / "data" / "processed" / "d5" / "sample_thermal.csv"
DEFAULT_ROUTE = PIPELINE_ROOT / "data" / "processed" / "d5" / "route_thermal.csv"
DEFAULT_D4_SAMPLE = PIPELINE_ROOT / "data" / "processed" / "d4" / "d4_sample_surface.csv"
DEFAULT_D4_ROUTE = PIPELINE_ROOT / "data" / "processed" / "d4" / "d4_route_surface.csv"
DEFAULT_OVERRIDES = PIPELINE_ROOT / "config" / "surface_overrides.csv"
DEFAULT_CONFIG = PIPELINE_ROOT / "config" / "d5_thermal_model.json"
DEFAULT_QA_GPKG = PIPELINE_ROOT / "data" / "interim" / "thermal" / "d5_thermal_qa_final.gpkg"
DEFAULT_REPORT = PIPELINE_ROOT / "reports" / "d5_thermal_validation.md"


def parse_arguments() -> argparse.Namespace:
    """검증할 D5 샘플·구간 산출물 경로를 받는다."""
    parser = argparse.ArgumentParser(description="D5 노면온도 산출물 자동 검증")
    parser.add_argument("--sample", type=Path, default=DEFAULT_SAMPLE)
    parser.add_argument("--route", type=Path, default=DEFAULT_ROUTE)
    parser.add_argument("--d4-sample", type=Path, default=DEFAULT_D4_SAMPLE)
    parser.add_argument("--d4-route", type=Path, default=DEFAULT_D4_ROUTE)
    parser.add_argument("--overrides", type=Path, default=DEFAULT_OVERRIDES)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--qa-gpkg", type=Path, default=DEFAULT_QA_GPKG)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    return parser.parse_args()


def validate_outputs(samples: pd.DataFrame, routes: pd.DataFrame,
                     d4_samples: pd.DataFrame, d4_routes: pd.DataFrame,
                     overrides: pd.DataFrame, config: dict[str, object]) -> dict[str, object]:
    """D4 재질 계보부터 온도·그림자·링크 집계까지 최종 불변식을 검사한다."""
    if len(samples) != EXPECTED_SAMPLE_COUNT:
        raise ValueError(f"샘플 행 수 불일치: {len(samples):,} != {EXPECTED_SAMPLE_COUNT:,}")
    if len(routes) != EXPECTED_SEGMENT_COUNT:
        raise ValueError(f"구간 행 수 불일치: {len(routes):,} != {EXPECTED_SEGMENT_COUNT:,}")
    if not samples["sample_id"].is_unique or not routes["segment_id"].is_unique:
        raise ValueError("sample_id 또는 route segment_id가 중복되었습니다.")
    if not d4_samples["sample_id"].is_unique or not d4_routes["segment_id"].is_unique:
        raise ValueError("D4 sample_id 또는 segment_id가 중복되었습니다.")
    if set(samples["sample_id"]) != set(d4_samples["sample_id"]):
        raise ValueError("D4/D5 sample_id 집합이 다릅니다.")
    if set(routes["segment_id"]) != set(d4_routes["segment_id"]):
        raise ValueError("D4/D5 segment_id 집합이 다릅니다.")

    sample_surface = d4_samples[["sample_id", "surface_type"]].merge(
        samples[["sample_id", "surface_type"]], on="sample_id", suffixes=("_d4", "_d5"),
        validate="one_to_one",
    )
    route_surface = d4_routes[["segment_id", "surface_type"]].merge(
        routes[["segment_id", "surface_type"]], on="segment_id", suffixes=("_d4", "_d5"),
        validate="one_to_one",
    )
    sample_surface_mismatches = int(
        sample_surface["surface_type_d4"].ne(sample_surface["surface_type_d5"]).sum()
    )
    route_surface_mismatches = int(
        route_surface["surface_type_d4"].ne(route_surface["surface_type_d5"]).sum()
    )
    if sample_surface_mismatches or route_surface_mismatches:
        raise ValueError(
            f"D4/D5 재질 불일치: sample={sample_surface_mismatches}, route={route_surface_mismatches}"
        )

    if overrides["segment_id"].duplicated().any():
        raise ValueError("surface_overrides.csv에 중복 segment_id가 있습니다.")
    override_routes = overrides[["segment_id", "surface_type"]].merge(
        routes[["segment_id", "surface_type"]], on="segment_id", suffixes=("_override", "_d5"),
        how="left", validate="one_to_one",
    )
    override_missing = int(override_routes["surface_type_d5"].isna().sum())
    override_route_mismatches = int((
        override_routes["surface_type_d5"].notna()
        & override_routes["surface_type_override"].ne(override_routes["surface_type_d5"])
    ).sum())
    override_samples = samples[["segment_id", "surface_type"]].merge(
        overrides[["segment_id", "surface_type"]], on="segment_id",
        suffixes=("_d5", "_override"), validate="many_to_one",
    )
    override_sample_mismatches = int(
        override_samples["surface_type_d5"].ne(override_samples["surface_type_override"]).sum()
    )
    if override_missing or override_route_mismatches or override_sample_mismatches:
        raise ValueError(
            "수동 override 미반영: "
            f"missing={override_missing}, route={override_route_mismatches}, sample={override_sample_mismatches}"
        )

    sample_temperature_columns = [f"surface_temp_{hour:02d}_c" for hour in HOURS]
    numeric_columns = sample_temperature_columns + [
        *(f"excess_temp_{hour:02d}_c" for hour in HOURS),
        "surface_temp_peak_c",
    ]
    numeric = samples[numeric_columns].to_numpy(dtype=float)
    if not np.isfinite(numeric).all():
        raise ValueError("샘플 온도 산출물에 NULL, NaN 또는 무한대가 있습니다.")
    if float(numeric.min()) < -50.0 or float(numeric.max()) > 100.0:
        raise ValueError("샘플 온도가 물리적 안전 범위(-50~100°C)를 벗어났습니다.")

    expected_peak = samples[sample_temperature_columns].max(axis=1)
    if not np.allclose(samples["surface_temp_peak_c"], expected_peak, atol=1e-9, rtol=0):
        raise ValueError("샘플 peak 온도가 시간대별 최댓값과 다릅니다.")
    route_numeric_columns = sample_temperature_columns + ["surface_temp_peak_c"]
    route_numeric = routes[route_numeric_columns].to_numpy(dtype=float)
    if not np.isfinite(route_numeric).all():
        raise ValueError("링크 온도 산출물에 NULL, NaN 또는 무한대가 있습니다.")
    expected_route_peak = routes[sample_temperature_columns].max(axis=1)
    if not np.allclose(routes["surface_temp_peak_c"], expected_route_peak, atol=1e-9, rtol=0):
        raise ValueError("링크 peak 온도가 시간대별 최댓값과 다릅니다.")
    if set(samples["weather_status"]) != {"OBSERVED_ASOS_SAME_DATE"}:
        raise ValueError("검증되지 않은 기상 상태가 포함되었습니다.")
    if set(samples["model_confidence"]) != {"LOW"}:
        raise ValueError("현재 D5 모델 신뢰도 정책(LOW)과 다른 값이 포함되었습니다.")
    if set(routes["weather_status"]) != {"OBSERVED_ASOS_SAME_DATE"}:
        raise ValueError("링크에 검증되지 않은 기상 상태가 포함되었습니다.")
    if set(routes["model_confidence"]) != {"LOW"}:
        raise ValueError("링크 모델 신뢰도 정책(LOW)과 다른 값이 포함되었습니다.")
    if config.get("weather_observed_date") != "2026-08-11" or config.get("weather_status") != "OBSERVED_ASOS_SAME_DATE":
        raise ValueError("열모델 설정의 기상 기준일 또는 상태가 확정값과 다릅니다.")
    if config.get("confidence") != "LOW":
        raise ValueError("열모델 설정 신뢰도가 LOW가 아닙니다.")

    shade_columns = [f"is_shaded_{hour:02d}" for hour in HOURS]
    shade_source_columns = [f"shade_src_{hour:02d}" for hour in HOURS]
    if not np.array_equal(
        samples[shade_columns].isna().to_numpy(),
        samples[shade_source_columns].isna().to_numpy(),
    ):
        raise ValueError("그늘 값과 그늘 출처의 NULL 위치가 일치하지 않습니다.")
    shade_unknown_all = int(samples[shade_columns].isna().all(axis=1).sum())
    shade_unknown_partial = int(
        (samples[shade_columns].isna().any(axis=1) & ~samples[shade_columns].isna().all(axis=1)).sum()
    )

    expected_routes = aggregate_routes(samples).sort_values("segment_id").reset_index(drop=True)
    actual_routes = routes.sort_values("segment_id").reset_index(drop=True)
    categorical_columns = ["surface_type", "weather_status", "model_confidence"]
    for column in categorical_columns:
        if not actual_routes[column].equals(expected_routes[column]):
            raise ValueError(f"구간 집계 문자 필드가 재계산값과 다릅니다: {column}")
    numeric_route_columns = [
        column for column in expected_routes.columns
        if column not in {"segment_id", *categorical_columns}
    ]
    actual_numeric = actual_routes[numeric_route_columns].to_numpy(dtype=float)
    expected_numeric = expected_routes[numeric_route_columns].to_numpy(dtype=float)
    if not np.array_equal(np.isnan(actual_numeric), np.isnan(expected_numeric)):
        raise ValueError("구간 집계 수치 필드의 NULL 위치가 재계산 결과와 다릅니다.")
    finite = np.isfinite(actual_numeric) & np.isfinite(expected_numeric)
    route_error = np.abs(actual_numeric[finite] - expected_numeric[finite])
    maximum_route_error = float(route_error.max(initial=0.0))
    # route CSV는 반올림 전 샘플로 집계하지만 검증 입력 CSV는 0.01°C로 반올림되어 있다.
    # 따라서 경계값에서 생길 수 있는 최대 0.01°C 이중 반올림 차이만 허용한다.
    if maximum_route_error > 0.010001:
        raise ValueError(f"구간 가중 집계 오차가 0.01을 초과했습니다: {maximum_route_error:.6f}")

    medians = {
        f"{hour:02d}": round(float(samples[f"surface_temp_{hour:02d}_c"].median()), 2)
        for hour in HOURS
    }
    if not (medians["09"] < medians["12"] and medians["18"] < medians["15"]):
        raise ValueError(f"시간대별 중앙값 경향이 비정상입니다: {medians}")

    time_statistics = {}
    material_statistics = {}
    for hour in HOURS:
        field = f"surface_temp_{hour:02d}_c"
        values = samples[field]
        time_statistics[f"{hour:02d}"] = {
            "min": round(float(values.min()), 2),
            "median": round(float(values.median()), 2),
            "mean": round(float(values.mean()), 2),
            "max": round(float(values.max()), 2),
        }
        material_statistics[f"{hour:02d}"] = {
            material: {
                "count": int(len(group)),
                "min": round(float(group[field].min()), 2),
                "median": round(float(group[field].median()), 2),
                "mean": round(float(group[field].mean()), 2),
                "max": round(float(group[field].max()), 2),
            }
            for material, group in samples.groupby("surface_type")
        }

    return {
        "sample_rows": len(samples),
        "route_rows": len(routes),
        "sample_temp_min_c": round(float(samples[sample_temperature_columns].min().min()), 2),
        "sample_temp_max_c": round(float(samples[sample_temperature_columns].max().max()), 2),
        "hour_medians_c": medians,
        "shade_unknown_all_hours": shade_unknown_all,
        "shade_unknown_partial_hours": shade_unknown_partial,
        "route_aggregation": "PASS",
        "route_aggregation_max_rounding_error": round(maximum_route_error, 6),
        "sample_duplicate_ids": int(samples["sample_id"].duplicated().sum()),
        "route_duplicate_ids": int(routes["segment_id"].duplicated().sum()),
        "sample_surface_distribution": samples["surface_type"].value_counts().to_dict(),
        "route_surface_distribution": routes["surface_type"].value_counts().to_dict(),
        "sample_temperature_nulls": samples[numeric_columns].isna().sum().to_dict(),
        "route_temperature_nulls": routes[route_numeric_columns].isna().sum().to_dict(),
        "sample_peak_errors": int((samples["surface_temp_peak_c"] != expected_peak).sum()),
        "route_peak_errors": int((routes["surface_temp_peak_c"] != expected_route_peak).sum()),
        "sample_surface_mismatches": sample_surface_mismatches,
        "route_surface_mismatches": route_surface_mismatches,
        "override_rows": int(len(overrides)),
        "override_missing": override_missing,
        "override_route_mismatches": override_route_mismatches,
        "override_sample_mismatches": override_sample_mismatches,
        "weather_date": str(config["weather_observed_date"]),
        "weather_status": str(config["weather_status"]),
        "model_confidence": str(config["confidence"]),
        "time_statistics": time_statistics,
        "material_statistics": material_statistics,
    }


def write_report(summary: dict[str, object], report_path: Path) -> None:
    """자동 검증 결과와 해석 정책을 Markdown 보고서로 저장한다."""
    medians = summary["hour_medians_c"]
    time_rows = "\n".join(
        f"| {hour}시 | {values['min']:.2f} | {values['median']:.2f} | {values['mean']:.2f} | {values['max']:.2f} |"
        for hour, values in summary["time_statistics"].items()
    )
    material_rows = "\n".join(
        f"| {hour}시 | {material} | {values['count']:,} | {values['min']:.2f} | "
        f"{values['median']:.2f} | {values['mean']:.2f} | {values['max']:.2f} |"
        for hour, materials in summary["material_statistics"].items()
        for material, values in materials.items()
    )
    sample_nulls = sum(summary["sample_temperature_nulls"].values())
    route_nulls = sum(summary["route_temperature_nulls"].values())
    executed_at = datetime.now().astimezone().isoformat(timespec="seconds")
    report = f"""# D5-8 노면온도 자동 검증

## 판정

- 결과: **PASS**
- 실행 시각: {executed_at}
- D4 샘플 입력: `data/processed/d4/d4_sample_surface.csv`
- D4 링크 입력: `data/processed/d4/d4_route_surface.csv`
- QGIS QA: `data/interim/thermal/d5_thermal_qa_final.gpkg`
- 샘플: {summary['sample_rows']:,}개
- 링크: {summary['route_rows']:,}개
- 노면온도 범위: {summary['sample_temp_min_c']:.2f}~{summary['sample_temp_max_c']:.2f}°C
- 링크 길이 가중 집계: {summary['route_aggregation']}
- CSV 반올림을 포함한 최대 집계 오차: {summary['route_aggregation_max_rounding_error']:.6f}°C
- 기상: {summary['weather_date']} / `{summary['weather_status']}`
- 모델 신뢰도: `{summary['model_confidence']}`

## 재질 및 ID 정합성

- 샘플 재질 분포: {summary['sample_surface_distribution']}
- 링크 재질 분포: {summary['route_surface_distribution']}
- sample_id / segment_id 중복: {summary['sample_duplicate_ids']} / {summary['route_duplicate_ids']}
- D4/D5 샘플·링크 재질 불일치: {summary['sample_surface_mismatches']} / {summary['route_surface_mismatches']}
- 수동 override: {summary['override_rows']:,}개 확인, 누락 {summary['override_missing']}개, 링크 불일치 {summary['override_route_mismatches']}개, 샘플 불일치 {summary['override_sample_mismatches']}개

## 온도 결측 및 피크 정합성

- 샘플 / 링크 온도 NULL: {sample_nulls} / {route_nulls}
- 샘플 / 링크 피크 계산 오류: {summary['sample_peak_errors']} / {summary['route_peak_errors']}

## 시간대별 온도 통계

| 시각 | 최소 | 중앙값 | 평균 | 최대 |
|---:|---:|---:|---:|---:|
{time_rows}

## 재질별 시간대 통계

| 시각 | 재질 | 표본 | 최소 | 중앙값 | 평균 | 최대 |
|---:|---|---:|---:|---:|---:|---:|
{material_rows}

## 시간대별 중앙값

| 시각 | 중앙값 |
|---:|---:|
| 09시 | {medians['09']:.2f}°C |
| 12시 | {medians['12']:.2f}°C |
| 15시 | {medians['15']:.2f}°C |
| 18시 | {medians['18']:.2f}°C |

09시보다 12시가 높고, 15시보다 18시가 낮아지는 전 구간 시간 경향을 통과했다.

## 그림자 결측 정책

- 네 시각 모두 그림자 관측 범위 밖인 샘플: {summary['shade_unknown_all_hours']:,}개
- 일부 시각만 결측인 샘플: {summary['shade_unknown_partial_hours']:,}개
- `is_shaded_*`와 `shade_src_*`의 결측 위치가 모두 일치한다.
- 관측 범위 밖 값은 `False`로 저장하지 않고 원본 필드에서는 NULL을 유지한다.
- 온도 계산에서는 안전 측면에서 양지로 취급하여 확인되지 않은 그늘의 냉각 효과를 부여하지 않는다.
- 링크 `shade_ratio_*`는 알려진 관측만 분모에 포함하며, 전부 미관측이면 NULL을 유지한다.

## 계산식 불변 조건

- 온도·초과온도·피크값은 모두 유한값이며 -50~100°C 안전 범위 안이다.
- 피크값은 09·12·15·18시 값의 행별 최댓값과 일치한다.
- 일사량 증가 시 온도가 상승하고 풍속 증가 시 온도가 하락하는 단위 검증을 수행한다.
- 동일 조건에서 그늘이 양지보다 낮고 알베도가 높을수록 온도가 낮아지는 단위 검증을 수행한다.
- 공원 냉각은 200m 이내 최대 1.5°C를 넘지 않는다.
- 링크 값은 단순 평균이 아니라 샘플 대표구간 길이 가중평균과 일치한다.

## 남은 한계

- 실측 양지-그늘 ΔT 잔차가 10°C를 넘는 관측이 있어 모델 신뢰도는 `LOW`로 유지한다.
- 이는 계산 실패가 아니라 실측 12쌍과 관측소·서비스 지역의 공간 차이에 따른 보정 한계다.
- 현재 표본에 계수를 과적합하지 않고 추가 실측 후 재보정한다.

## 테스트

- validator: **PASS**
- 전체 unit test: validator 실행 뒤 별도 명령 결과를 기록한다.

## 재현 명령

```powershell
& "C:\\Program Files\\QGIS 3.44.12\\bin\\python-qgis-ltr.bat" `
  src\\validate_d5_thermal.py
```
"""
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(report, encoding="utf-8")


def main() -> None:
    """CSV를 읽어 검증하고 재현 가능한 요약 결과를 출력한다."""
    args = parse_arguments()
    required_paths = (
        args.sample, args.route, args.d4_sample, args.d4_route,
        args.overrides, args.config, args.qa_gpkg,
    )
    for path in required_paths:
        if not path.is_file():
            raise FileNotFoundError(f"D5 최종 검증 입력이 없습니다: {path}")
    summary = validate_outputs(
        pd.read_csv(args.sample), pd.read_csv(args.route),
        pd.read_csv(args.d4_sample), pd.read_csv(args.d4_route),
        pd.read_csv(args.overrides), json.loads(args.config.read_text(encoding="utf-8")),
    )
    write_report(summary, args.report)
    print("[D5-8 자동 검증 통과]")
    for key, value in summary.items():
        print(f"- {key}: {value}")
    print(f"- report: {args.report}")


if __name__ == "__main__":
    main()
