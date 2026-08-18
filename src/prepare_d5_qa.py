from __future__ import annotations

import argparse
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd


PIPELINE_ROOT = Path(__file__).resolve().parents[1]
D5_DIR = PIPELINE_ROOT / "data" / "processed" / "d5"
DEFAULT_D4_GPKG = PIPELINE_ROOT / "data" / "interim" / "surface" / "d4_integrated_qa_final.gpkg"
DEFAULT_OUTPUT = PIPELINE_ROOT / "data" / "interim" / "thermal" / "d5_thermal_qa.gpkg"
DEFAULT_REPORT = PIPELINE_ROOT / "reports" / "d5_integrated_qa.md"
HOURS = (9, 12, 15, 18)
EXPECTED_SAMPLES = 31_167
EXPECTED_ROUTES = 7_766


def parse_arguments() -> argparse.Namespace:
    """D5 QGIS 산출물의 입력·출력 경로를 명령행에서 받는다."""
    parser = argparse.ArgumentParser(description="D5 노면온도 QGIS 통합 QA 생성")
    parser.add_argument("--d4-gpkg", type=Path, default=DEFAULT_D4_GPKG)
    parser.add_argument("--sample", type=Path, default=D5_DIR / "sample_thermal.csv")
    parser.add_argument("--route", type=Path, default=D5_DIR / "route_thermal.csv")
    parser.add_argument("--calibration", type=Path, default=D5_DIR / "calibration_predictions_actual.csv")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--overwrite", action="store_true")
    return parser.parse_args()


def read_inputs(args: argparse.Namespace) -> tuple[gpd.GeoDataFrame, gpd.GeoDataFrame, pd.DataFrame]:
    """D4 geometry와 D5 계산 결과를 ID 기준 일대일로 결합한다."""
    for path in (args.d4_gpkg, args.sample, args.route, args.calibration):
        if not path.is_file():
            raise FileNotFoundError(f"QA 입력 파일이 없습니다: {path}")

    sample_geom = gpd.read_file(args.d4_gpkg, layer="sample_d4")[["sample_id", "geometry"]]
    route_geom = gpd.read_file(args.d4_gpkg, layer="route_d4")[["segment_id", "geometry"]]
    sample_values = pd.read_csv(args.sample, encoding="utf-8-sig")
    route_values = pd.read_csv(args.route, encoding="utf-8-sig")
    calibration = pd.read_csv(args.calibration, encoding="utf-8-sig")

    if len(sample_values) != EXPECTED_SAMPLES or len(route_values) != EXPECTED_ROUTES:
        raise ValueError("D5 계산 행 수가 예상 샘플·링크 수와 다릅니다.")
    if not sample_values["sample_id"].is_unique or not route_values["segment_id"].is_unique:
        raise ValueError("D5 계산 결과 ID가 중복되었습니다.")

    samples = sample_geom.merge(sample_values, on="sample_id", validate="one_to_one")
    routes = route_geom.merge(route_values, on="segment_id", validate="one_to_one")
    if len(samples) != EXPECTED_SAMPLES or len(routes) != EXPECTED_ROUTES:
        raise ValueError("D4 geometry와 D5 계산 결과의 ID 결합이 완전하지 않습니다.")
    return samples, routes, calibration


def add_temperature_classes(frame: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """QGIS의 단계 구분 없이도 사용할 수 있는 5단계 온도 구간 문자열을 추가한다."""
    result = frame.copy()
    for hour in HOURS:
        field = f"surface_temp_{hour:02d}_c"
        result[f"temp_cls_{hour:02d}"] = pd.cut(
            result[field],
            bins=[-np.inf, 30, 35, 40, 45, np.inf],
            labels=["LT30", "30_35", "35_40", "40_45", "GE45"],
            right=False,
        ).astype(str)
    return result


def build_candidates(samples: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """극단 온도·낮은 입력 품질·낮은 SVF를 시각 검토 후보로 추린다."""
    lower = {hour: samples[f"surface_temp_{hour:02d}_c"].quantile(0.01) for hour in HOURS}
    upper = {hour: samples[f"surface_temp_{hour:02d}_c"].quantile(0.99) for hour in HOURS}
    reasons: list[str] = []
    for row in samples.itertuples(index=False):
        current: list[str] = []
        for hour in HOURS:
            value = float(getattr(row, f"surface_temp_{hour:02d}_c"))
            if value <= lower[hour]:
                current.append(f"LOW_P01_{hour:02d}")
            elif value >= upper[hour]:
                current.append(f"HIGH_P99_{hour:02d}")
        if str(row.surface_quality) in {"C", "D"}:
            current.append("LOW_SURFACE_QUALITY")
        if float(row.svf_effective) <= 0.05:
            current.append("SVF_LE_005")
        reasons.append(";".join(current))
    result = samples.copy()
    result["qa_reason"] = reasons
    return result[result["qa_reason"] != ""].copy()


def validate_physics(samples: pd.DataFrame, calibration: pd.DataFrame) -> list[str]:
    """온도 범위, 그늘 방향, 실측 잔차를 검사하고 주의사항을 반환한다."""
    warnings: list[str] = []
    for hour in HOURS:
        temp = samples[f"surface_temp_{hour:02d}_c"]
        if not temp.between(-20, 80).all():
            warnings.append(f"{hour:02d}시 물리 범위(-20~80°C) 이탈")
        shade_mask = samples[f"is_shaded_{hour:02d}"].fillna(False).astype(bool)
        shaded = samples.loc[shade_mask, f"surface_temp_{hour:02d}_c"]
        sunlit = samples.loc[~shade_mask, f"surface_temp_{hour:02d}_c"]
        if shaded.mean() >= sunlit.mean():
            warnings.append(f"{hour:02d}시 전체 평균에서 그늘이 양지보다 낮지 않음")
    if float(calibration["delta_residual_c"].abs().max()) > 10.0:
        warnings.append("실측 양지-그늘 ΔT 잔차가 10°C를 넘는 행 존재")
    return warnings


def write_report(samples: pd.DataFrame, routes: pd.DataFrame, calibration: pd.DataFrame,
                 candidates: pd.DataFrame, warnings: list[str], path: Path) -> None:
    """시간·재질·그늘별 통계와 계산식 재검토 결과를 Markdown으로 기록한다."""
    lines = [
        "# D5 QGIS 통합 QA 및 계산식 재검토", "", "## 산출물", "",
        f"- 샘플: {len(samples):,}개", f"- 링크: {len(routes):,}개",
        f"- 시각 검토 후보: {len(candidates):,}개", "- geometry CRS: EPSG:5186", "",
        "## 시간대별 노면온도", "",
        "|시각|최소|P05|중앙값|평균|P95|최대|그늘 평균|양지 평균|양지-그늘|",
        "|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for hour in HOURS:
        field = f"surface_temp_{hour:02d}_c"
        values = samples[field]
        shade_mask = samples[f"is_shaded_{hour:02d}"].fillna(False).astype(bool)
        shaded = samples.loc[shade_mask, field]
        sunlit = samples.loc[~shade_mask, field]
        lines.append(
            f"|{hour:02d}|{values.min():.2f}|{values.quantile(.05):.2f}|{values.median():.2f}|"
            f"{values.mean():.2f}|{values.quantile(.95):.2f}|{values.max():.2f}|"
            f"{shaded.mean():.2f}|{sunlit.mean():.2f}|{sunlit.mean()-shaded.mean():.2f}|"
        )

    lines += ["", "## 재질별 15시 평균", ""]
    for material, value in samples.groupby("surface_type")["surface_temp_15_c"].mean().sort_values().items():
        lines.append(f"- {material}: {value:.2f}°C")

    residual = calibration["delta_residual_c"]
    lines += [
        "", "## 계산식 재검토", "",
        "- 단파복사: `(1-albedo) × 일사량`에서 지중열 비율을 제외한 에너지를 사용한다.",
        "- 대류: 풍속이 커질수록 열전달계수가 증가하여 노면온도가 낮아진다.",
        "- 장파복사: SVF는 차가운 하늘과 주변 도시표면의 가시 비율로만 사용한다.",
        "- 그림자: 직접 일사량에 보정된 투과율을 적용하므로 SVF와 그림자 효과를 같은 항에서 중복 차감하지 않는다.",
        "- 공원: 200m 이내 최대 1.5°C의 선형 냉각은 아직 현장 보정 전 사전값이다.",
        f"- 실측 ΔT 잔차 MAE/RMSE: {residual.abs().mean():.2f}°C / {np.sqrt(np.mean(residual**2)):.2f}°C", "",
        "## 자동 검사", "",
    ]
    if warnings:
        lines.extend(f"- 주의: {warning}" for warning in warnings)
    else:
        lines.append("- 물리 범위와 전체 평균의 양지/그늘 방향 검사 통과")
    lines += [
        "", "## QGIS에서 확인할 대표 반례", "",
        "1. 고온 후보: 15시 `HIGH_P99_15`가 건물 내부나 수면에 놓이지 않는지 확인",
        "2. 저온 후보: 09시 또는 18시 `LOW_P01`이 공원·그늘·낮은 SVF와 일치하는지 확인",
        "3. 재질: 현재 정책의 asphalt/pavement 분류가 배경지도와 현저히 다르지 않은지 확인",
        "4. 공원: 공원 경계 안과 인접 구간의 냉각이 최대 1.5°C 이내인지 확인",
        "5. 구조물 아래: 강제 건물그늘 구간이 양지로 계산되지 않았는지 확인", "",
        "이 산출물은 QGIS QA용이며 현재 DB 반영 상태는 `reports/d5_completion.md`에서 확인한다.",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_gpkg(samples: gpd.GeoDataFrame, routes: gpd.GeoDataFrame,
               candidates: gpd.GeoDataFrame, path: Path, overwrite: bool) -> None:
    """샘플·링크·후보를 한 GeoPackage의 세 레이어로 저장한다."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not overwrite:
        raise FileExistsError(f"기존 QA 파일이 있습니다: {path}")
    if path.exists():
        path.unlink()
    samples.to_file(path, layer="sample_thermal", driver="GPKG")
    routes.to_file(path, layer="route_thermal", driver="GPKG", mode="a")
    candidates.to_file(path, layer="qa_candidates", driver="GPKG", mode="a")


def main() -> None:
    """D5 QGIS 통합 QA 파일과 통계 보고서를 생성한다."""
    args = parse_arguments()
    samples, routes, calibration = read_inputs(args)
    samples = add_temperature_classes(samples)
    routes = add_temperature_classes(routes)
    candidates = build_candidates(samples)
    warnings = validate_physics(samples, calibration)
    write_gpkg(samples, routes, candidates, args.output, args.overwrite)
    write_report(samples, routes, calibration, candidates, warnings, args.report)
    print(
        f"[D5 QGIS QA 완료] samples={len(samples):,}, routes={len(routes):,}, "
        f"candidates={len(candidates):,}, warnings={len(warnings):,}"
    )


if __name__ == "__main__":
    main()
