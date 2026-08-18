from __future__ import annotations

import argparse
from pathlib import Path

import geopandas as gpd
import pandas as pd
from pyproj import Transformer
from shapely.geometry import box


PIPELINE_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CODE_CSV = PIPELINE_ROOT / "data" / "raw" / "tdata" / "SWM_BASIC_CODE.csv"
DEFAULT_SIDEWALK_CSV = PIPELINE_ROOT / "data" / "raw" / "tdata" / "SWM_WKAR_AS.csv"
DEFAULT_THERMAL_GPKG = (
    PIPELINE_ROOT / "data" / "interim" / "thermal" / "d5_thermal_qa_final.gpkg"
)
DEFAULT_QA_GPKG = (
    PIPELINE_ROOT / "data" / "interim" / "surface" / "d5_tdata_sidewalk_qa.gpkg"
)
DEFAULT_SAMPLE_CSV = (
    PIPELINE_ROOT / "data" / "processed" / "d5" / "tdata_sample_candidates.csv"
)
DEFAULT_SEGMENT_CSV = (
    PIPELINE_ROOT / "data" / "processed" / "d5" / "tdata_segment_candidates.csv"
)
DEFAULT_REPORT = PIPELINE_ROOT / "reports" / "d5_tdata_sidewalk_qa.md"

MIN_SAFE_SAMPLE_COUNT = 2
MIN_SAFE_COVERAGE_RATIO = 0.60
MIN_SAFE_AGREEMENT_RATIO = 1.00

JUNGGU_PNU_PREFIX = "11140"
SOURCE_CRS = "EPSG:5181"
TARGET_CRS = "EPSG:5186"
KNOWN_PAVEMENT_CODES = {"SWB001", "SWB002", "SWB003", "SWB004", "SWB007", "SWB008"}
KNOWN_ASPHALT_CODES = {"SWB005"}
REVIEW_CODES = {"SWB006", "SWB999", "SWB000", "-", ""}


def parse_arguments() -> argparse.Namespace:
    """Read reproducible input/output paths without changing existing D5 results."""
    parser = argparse.ArgumentParser(
        description="Build T-DATA sidewalk bbox candidates and compare them with D5 samples."
    )
    parser.add_argument("--code-csv", type=Path, default=DEFAULT_CODE_CSV)
    parser.add_argument("--sidewalk-csv", type=Path, default=DEFAULT_SIDEWALK_CSV)
    parser.add_argument("--thermal-gpkg", type=Path, default=DEFAULT_THERMAL_GPKG)
    parser.add_argument("--qa-gpkg", type=Path, default=DEFAULT_QA_GPKG)
    parser.add_argument("--sample-csv", type=Path, default=DEFAULT_SAMPLE_CSV)
    parser.add_argument("--segment-csv", type=Path, default=DEFAULT_SEGMENT_CSV)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--overwrite", action="store_true")
    return parser.parse_args()


def normalize_swb_code(value: object) -> str:
    """Normalize both `001` and `SWB001` forms to one stable identifier."""
    text = "" if pd.isna(value) else str(value).strip()
    if text in {"", "-"}:
        return text
    if text.startswith("SWB"):
        return text
    return f"SWB{text.zfill(3)}"


def candidate_surface_type(swb_code: object) -> str:
    """Map official paving codes only when they fit the current thermal categories."""
    code = normalize_swb_code(swb_code)
    if code in KNOWN_PAVEMENT_CODES:
        return "pavement"
    if code in KNOWN_ASPHALT_CODES:
        return "asphalt"
    return "review"


def bbox_match_status(overlap_count: int, area_m2: float, max_dimension_m: float,
                      surface_type: str) -> str:
    """Flag uncertain envelope matches instead of treating a bbox as exact pavement geometry."""
    if surface_type == "review":
        return "UNKNOWN_MATERIAL"
    if overlap_count > 1:
        return "MULTIPLE_BBOX"
    if area_m2 > 2500.0 or max_dimension_m > 50.0:
        return "SINGLE_LARGE_BBOX"
    return "SINGLE_SMALL_BBOX"


def load_code_names(path: Path) -> dict[str, str]:
    """Load the official SWB code labels used for QGIS inspection and reports."""
    table = pd.read_csv(path, dtype=str, encoding="utf-8-sig").fillna("")
    swb = table.loc[table["대분류"].eq("SWB"), ["코드", "코드명"]].copy()
    return {
        normalize_swb_code(row["코드"]): str(row["코드명"]).strip()
        for _, row in swb.iterrows()
    }


def load_junggu_sidewalk_boxes(path: Path, code_names: dict[str, str]) -> gpd.GeoDataFrame:
    """Convert Jung-gu T-DATA centimeter bbox coordinates from EPSG:5181 to EPSG:5186."""
    required = [
        "G2_ID", "G2_XMIN", "G2_YMIN", "G2_XMAX", "G2_YMAX",
        "SWB_CODE", "RN_NM", "PNU", "BDL_ARA", "BDL_WID", "BDL_LEN",
    ]
    table = pd.read_csv(path, dtype=str, encoding="utf-8-sig", usecols=required).fillna("")
    table = table.loc[table["PNU"].str.startswith(JUNGGU_PNU_PREFIX)].copy()
    if table.empty:
        raise ValueError("PNU 11140으로 식별되는 서울 중구 보도면이 없습니다.")

    numeric_columns = ["G2_XMIN", "G2_YMIN", "G2_XMAX", "G2_YMAX"]
    for column in numeric_columns:
        table[column] = pd.to_numeric(table[column], errors="coerce") / 100.0
    if table[numeric_columns].isna().any().any():
        raise ValueError("중구 보도면 경계박스 좌표에 숫자가 아닌 값이 있습니다.")

    transformer = Transformer.from_crs(SOURCE_CRS, TARGET_CRS, always_xy=True)
    min_xy = [transformer.transform(x, y) for x, y in zip(table.G2_XMIN, table.G2_YMIN)]
    max_xy = [transformer.transform(x, y) for x, y in zip(table.G2_XMAX, table.G2_YMAX)]
    table["xmin_5186"] = [value[0] for value in min_xy]
    table["ymin_5186"] = [value[1] for value in min_xy]
    table["xmax_5186"] = [value[0] for value in max_xy]
    table["ymax_5186"] = [value[1] for value in max_xy]
    table["bbox_width_m"] = table["xmax_5186"] - table["xmin_5186"]
    table["bbox_height_m"] = table["ymax_5186"] - table["ymin_5186"]
    table["bbox_area_m2"] = table["bbox_width_m"] * table["bbox_height_m"]
    table["swb_code"] = table["SWB_CODE"].map(normalize_swb_code)
    table["swb_name"] = table["swb_code"].map(code_names).fillna("미확인")
    table["candidate_surface"] = table["swb_code"].map(candidate_surface_type)
    table["geometry"] = [
        box(xmin, ymin, xmax, ymax)
        for xmin, ymin, xmax, ymax in zip(
            table.xmin_5186, table.ymin_5186, table.xmax_5186, table.ymax_5186
        )
    ]
    result = gpd.GeoDataFrame(table, geometry="geometry", crs=TARGET_CRS)
    return result.rename(columns={"G2_ID": "g2_id", "RN_NM": "road_name", "PNU": "pnu"})


def load_thermal_samples(path: Path) -> gpd.GeoDataFrame:
    """Read the already-reviewed D5 sample points, preserving current thermal attributes."""
    samples = gpd.read_file(path, layer="sample_thermal")
    if samples.crs is None:
        raise ValueError("sample_thermal 레이어에 CRS가 없습니다.")
    samples = samples.to_crs(TARGET_CRS)
    if not samples["sample_id"].is_unique:
        raise ValueError("sample_thermal의 sample_id가 중복되었습니다.")
    return samples


def match_samples(samples: gpd.GeoDataFrame,
                  sidewalk_boxes: gpd.GeoDataFrame) -> tuple[gpd.GeoDataFrame, gpd.GeoDataFrame]:
    """Choose the smallest intersecting bbox while retaining overlap count and ambiguity flags."""
    box_columns = [
        "g2_id", "swb_code", "swb_name", "candidate_surface", "road_name", "pnu",
        "bbox_width_m", "bbox_height_m", "bbox_area_m2", "geometry",
    ]
    joined = gpd.sjoin(
        samples[["sample_id", "segment_id", "surface_type", "geometry"]],
        sidewalk_boxes[box_columns], how="left", predicate="intersects"
    )
    matched_rows = joined.loc[joined["g2_id"].notna()].copy()
    overlap_counts = matched_rows.groupby("sample_id")["g2_id"].size().rename("overlap_count")

    if matched_rows.empty:
        best = samples.iloc[0:0].copy()
    else:
        matched_rows = matched_rows.join(overlap_counts, on="sample_id")
        best = (
            matched_rows.sort_values(["sample_id", "bbox_area_m2", "g2_id"])
            .drop_duplicates("sample_id", keep="first")
            .drop(columns="index_right")
        )
        best["bbox_max_dim_m"] = best[["bbox_width_m", "bbox_height_m"]].max(axis=1)
        best["match_status"] = [
            bbox_match_status(int(count), float(area), float(max_dim), str(surface))
            for count, area, max_dim, surface in zip(
                best.overlap_count, best.bbox_area_m2,
                best.bbox_max_dim_m, best.candidate_surface
            )
        ]
        best["agrees_current"] = best["candidate_surface"].eq(best["surface_type"])

    matched_ids = set(best["sample_id"].astype(int))
    unmatched = samples.loc[~samples["sample_id"].astype(int).isin(matched_ids)].copy()
    return gpd.GeoDataFrame(best, geometry="geometry", crs=TARGET_CRS), unmatched


def aggregate_segment_candidates(samples: gpd.GeoDataFrame,
                                 all_sample_count: pd.Series) -> pd.DataFrame:
    """Summarize sample candidates by segment without writing them back to route_segment."""
    if samples.empty:
        return pd.DataFrame()
    records: list[dict[str, object]] = []
    for segment_id, group in samples.groupby("segment_id", sort=True):
        known = group.loc[group["candidate_surface"].isin(["pavement", "asphalt"])]
        safe = known.loc[known["match_status"].eq("SINGLE_SMALL_BBOX")]
        counts = known["candidate_surface"].value_counts()
        safe_counts = safe["candidate_surface"].value_counts()
        candidate = "review" if counts.empty else str(counts.index[0])
        safe_candidate = "review" if safe_counts.empty else str(safe_counts.index[0])
        known_count = int(len(known))
        safe_count = int(len(safe))
        mode_count = 0 if counts.empty else int(counts.iloc[0])
        safe_mode_count = 0 if safe_counts.empty else int(safe_counts.iloc[0])
        segment_total = int(all_sample_count.get(segment_id, 0))
        safe_coverage = 0.0 if segment_total == 0 else safe_count / segment_total
        safe_agreement = 0.0 if safe_count == 0 else safe_mode_count / safe_count
        current_counts = group["surface_type"].value_counts()
        current_surface = "unknown" if current_counts.empty else str(current_counts.index[0])
        if (
            safe_candidate == "pavement"
            and current_surface == "asphalt"
            and safe_count >= MIN_SAFE_SAMPLE_COUNT
            and safe_coverage >= MIN_SAFE_COVERAGE_RATIO
            and safe_agreement >= MIN_SAFE_AGREEMENT_RATIO
        ):
            correction_policy = "AUTO_PAVEMENT"
        elif safe_candidate in {"pavement", "asphalt"} and current_surface != safe_candidate:
            correction_policy = "REVIEW_PARTIAL"
        else:
            correction_policy = "KEEP_OR_NO_EVIDENCE"
        records.append({
            "segment_id": int(segment_id),
            "total_sample_count": segment_total,
            "bbox_matched_count": int(len(group)),
            "known_material_count": known_count,
            "candidate_surface": candidate,
            "candidate_agreement_ratio": 0.0 if known_count == 0 else mode_count / known_count,
            "bbox_coverage_ratio": 0.0 if segment_total == 0 else len(group) / segment_total,
            "ambiguous_sample_count": int(group["match_status"].ne("SINGLE_SMALL_BBOX").sum()),
            "safe_sample_count": safe_count,
            "safe_candidate_surface": safe_candidate,
            "safe_candidate_agreement_ratio": safe_agreement,
            "safe_coverage_ratio": safe_coverage,
            "current_surface_type": current_surface,
            "correction_policy": correction_policy,
            "current_disagreement_count": int(
                (group["candidate_surface"].isin(["pavement", "asphalt"]) &
                 ~group["agrees_current"]).sum()
            ),
        })
    return pd.DataFrame.from_records(records)


def validate_outputs(boxes: gpd.GeoDataFrame, samples: gpd.GeoDataFrame,
                     matched: gpd.GeoDataFrame, unmatched: gpd.GeoDataFrame) -> None:
    """Fail fast on missing IDs, invalid geometry, or sample accounting errors."""
    if len(boxes) != 2679:
        raise ValueError(f"중구 PNU 보도면 기대값 2,679건과 다릅니다: {len(boxes):,}")
    if boxes.geometry.is_empty.any() or not boxes.geometry.is_valid.all():
        raise ValueError("보도면 후보 경계박스에 비어 있거나 유효하지 않은 도형이 있습니다.")
    if len(matched) + len(unmatched) != len(samples):
        raise ValueError("매칭/미매칭 표본 합계가 원본 표본 수와 다릅니다.")
    if not matched["sample_id"].is_unique:
        raise ValueError("대표 후보 선정 후에도 sample_id가 중복되었습니다.")


def write_outputs(boxes: gpd.GeoDataFrame, matched: gpd.GeoDataFrame,
                  unmatched: gpd.GeoDataFrame, segments: pd.DataFrame,
                  routes: gpd.GeoDataFrame,
                  qa_path: Path, sample_path: Path, segment_path: Path,
                  overwrite: bool) -> None:
    """Write QGIS layers and audit CSVs; never update the production database here."""
    for path in (qa_path, sample_path, segment_path):
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and not overwrite:
            raise FileExistsError(f"기존 산출물이 있습니다. --overwrite를 사용하세요: {path}")
        if path.exists():
            path.unlink()

    box_output_columns = [
        "g2_id", "swb_code", "swb_name", "candidate_surface", "road_name", "pnu",
        "bbox_width_m", "bbox_height_m", "bbox_area_m2", "geometry",
    ]
    boxes[box_output_columns].to_file(qa_path, layer="tdata_sidewalk_bbox", driver="GPKG")
    matched.to_file(qa_path, layer="sample_tdata_candidate", driver="GPKG", mode="a")
    ambiguous = matched.loc[matched["match_status"].ne("SINGLE_SMALL_BBOX")].copy()
    if not ambiguous.empty:
        ambiguous.to_file(qa_path, layer="sample_tdata_ambiguous", driver="GPKG", mode="a")
    unmatched.to_file(qa_path, layer="sample_tdata_unmatched", driver="GPKG", mode="a")

    route_review = routes[["segment_id", "geometry"]].merge(
        segments, on="segment_id", how="inner", validate="one_to_one"
    )
    route_review = gpd.GeoDataFrame(route_review, geometry="geometry", crs=routes.crs)
    route_review.to_file(qa_path, layer="segment_tdata_review", driver="GPKG", mode="a")
    auto_pavement = route_review.loc[
        route_review["correction_policy"].eq("AUTO_PAVEMENT")
    ].copy()
    if not auto_pavement.empty:
        auto_pavement.to_file(
            qa_path, layer="segment_tdata_auto_pavement", driver="GPKG", mode="a"
        )

    matched.drop(columns="geometry").to_csv(sample_path, index=False, encoding="utf-8-sig")
    segments.to_csv(segment_path, index=False, encoding="utf-8-sig")


def write_report(boxes: gpd.GeoDataFrame, samples: gpd.GeoDataFrame,
                 matched: gpd.GeoDataFrame, segments: pd.DataFrame, path: Path) -> None:
    """Record coverage and limitations so bbox candidates cannot be mistaken for ground truth."""
    material_counts = boxes["swb_code"].value_counts().to_dict()
    status_counts = matched["match_status"].value_counts().to_dict()
    known = matched["candidate_surface"].isin(["pavement", "asphalt"])
    disagreement = known & ~matched["agrees_current"]
    lines = [
        "# D5 T-DATA 보도면 경계박스 후보 QA", "",
        "## 목적", "",
        "- T-DATA 보도면 CSV의 경계박스를 EPSG:5186 후보 레이어로 만든다.",
        "- D5 약 10m 표본점과 겹치는 공식 보도포장재질 후보를 찾는다.",
        "- G2_SPATIAL이 비어 있으므로 경계박스를 실제 보도면 폴리곤으로 확정하지 않는다.",
        "- 이 단계는 후보 생성만 하며 기존 재질 CSV와 DB를 수정하지 않는다.", "",
        "## 입력 검증", "",
        f"- PNU 11140 중구 보도면 후보: {len(boxes):,}",
        f"- D5 표본점: {len(samples):,}",
        f"- 경계박스와 하나 이상 겹친 표본점: {len(matched):,}",
        f"- 미겹침 표본점: {len(samples) - len(matched):,}",
        f"- 겹침률: {len(matched) / len(samples):.2%}", "",
        "## 후보 품질", "",
    ]
    lines.extend(f"- {key}: {value:,}" for key, value in sorted(status_counts.items()))
    lines += [
        "",
        f"- 공식 코드가 pavement/asphalt인 표본: {int(known.sum()):,}",
        f"- 현재 분류와 다른 표본: {int(disagreement.sum()):,}",
        f"- 후보가 존재하는 세그먼트: {len(segments):,}", "",
        "## 세그먼트 보정 후보", "",
        f"- AUTO_PAVEMENT: {int(segments['correction_policy'].eq('AUTO_PAVEMENT').sum()):,}",
        f"- REVIEW_PARTIAL: {int(segments['correction_policy'].eq('REVIEW_PARTIAL').sum()):,}",
        f"- KEEP_OR_NO_EVIDENCE: {int(segments['correction_policy'].eq('KEEP_OR_NO_EVIDENCE').sum()):,}",
        f"- 자동 후보 기준: 안전 표본 {MIN_SAFE_SAMPLE_COUNT}개 이상, "
        f"안전 커버리지 {MIN_SAFE_COVERAGE_RATIO:.0%} 이상, 재질 일치율 "
        f"{MIN_SAFE_AGREEMENT_RATIO:.0%}",
        "- AUTO_PAVEMENT만 자동 보정 가능 후보이며, 이 실행에서는 DB를 수정하지 않는다.", "",
        "## 중구 보도포장재질 코드 분포", "",
    ]
    lines.extend(f"- {key or '(빈 값)'}: {value:,}" for key, value in sorted(material_counts.items()))
    lines += [
        "", "## 판정 제한", "",
        "- 보도면 후보는 최소·최대 좌표로 만든 사각형이며 실제 경계가 아니다.",
        "- 여러 후보가 겹치면 면적이 가장 작은 후보를 대표로 표시하지만 자동 확정하지 않는다.",
        "- 고무·기타·해당없음 코드는 현재 열모델 재질과 직접 대응시키지 않고 review로 둔다.",
        "- 최종 규칙 변경은 QGIS에서 대로변 보도, 좁은 골목, 단지 내부를 각각 확인한 뒤 수행한다.",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    """Run the T-DATA candidate preparation and spatial QA workflow."""
    args = parse_arguments()
    code_names = load_code_names(args.code_csv.resolve())
    boxes = load_junggu_sidewalk_boxes(args.sidewalk_csv.resolve(), code_names)
    samples = load_thermal_samples(args.thermal_gpkg.resolve())
    routes = gpd.read_file(args.thermal_gpkg.resolve(), layer="route_thermal").to_crs(TARGET_CRS)
    matched, unmatched = match_samples(samples, boxes)
    all_sample_count = samples.groupby("segment_id")["sample_id"].size()
    segments = aggregate_segment_candidates(matched, all_sample_count)
    validate_outputs(boxes, samples, matched, unmatched)
    write_outputs(
        boxes, matched, unmatched, segments, routes,
        args.qa_gpkg.resolve(), args.sample_csv.resolve(), args.segment_csv.resolve(),
        args.overwrite,
    )
    write_report(boxes, samples, matched, segments, args.report.resolve())
    print(f"[완료] 중구 T-DATA 경계박스: {len(boxes):,}")
    print(f"[완료] 표본 겹침: {len(matched):,} / {len(samples):,}")
    print(f"[QA] {args.qa_gpkg.resolve()}")
    print("[안내] 기존 재질 CSV와 DB는 수정하지 않았습니다.")


if __name__ == "__main__":
    main()
