from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
from osgeo import gdal
from shapely.geometry import Point


gdal.UseExceptions()

PIPELINE_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_NETWORK_PATH = (
    PIPELINE_ROOT
    / "data"
    / "processed"
    / "network"
    / "junggu_walk_network_snapped.gpkg"
)
DEFAULT_NETWORK_LAYER = "snapped_links"
DEFAULT_HEIGHT_PATH = (
    PIPELINE_ROOT
    / "data"
    / "processed"
    / "surface"
    / "junggu_building_height_2m.tif"
)
DEFAULT_QUALITY_PATH = (
    PIPELINE_ROOT
    / "data"
    / "processed"
    / "surface"
    / "junggu_building_height_quality_2m.tif"
)
DEFAULT_EXCLUSION_PATH = PIPELINE_ROOT / "config" / "network_exclusions.csv"
DEFAULT_OUTPUT_PATH = (
    PIPELINE_ROOT
    / "data"
    / "interim"
    / "surface"
    / "d3_dsm_spatial_qa.gpkg"
)
DEFAULT_REPORT_PATH = PIPELINE_ROOT / "reports" / "d3_dsm_spatial_qa.md"

TARGET_EPSG = 5186
SAMPLE_INTERVAL_M = 2.0
ACTIVE_EXCLUSION_STATUSES = {"EXCLUDE", "EXCLUDE_TEMPORARY"}
EXPECTED_ROUTABLE_LINK_COUNT = 7_766


@dataclass(frozen=True)
class RasterValues:
    """건물 높이와 높이 출처 품질 배열 및 좌표 변환 정보를 보관한다."""

    height: np.ndarray
    quality: np.ndarray
    geotransform: tuple[float, float, float, float, float, float]


def parse_arguments() -> argparse.Namespace:
    """DSM 공간 QA 입력과 출력 경로를 해석한다."""
    parser = argparse.ArgumentParser(
        description="도보 링크와 건물 높이 래스터의 중첩 후보를 생성합니다."
    )
    parser.add_argument("--network", type=Path, default=DEFAULT_NETWORK_PATH)
    parser.add_argument("--network-layer", default=DEFAULT_NETWORK_LAYER)
    parser.add_argument("--height", type=Path, default=DEFAULT_HEIGHT_PATH)
    parser.add_argument("--quality", type=Path, default=DEFAULT_QUALITY_PATH)
    parser.add_argument("--exclusions", type=Path, default=DEFAULT_EXCLUSION_PATH)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_PATH)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT_PATH)
    parser.add_argument("--overwrite", action="store_true")
    return parser.parse_args()


def validate_input_paths(paths: list[Path]) -> None:
    """공간 QA에 필요한 모든 파일이 존재하는지 확인한다."""
    missing = [str(path) for path in paths if not path.is_file()]
    if missing:
        raise FileNotFoundError("입력 파일이 없습니다: " + ", ".join(missing))


def read_active_exclusion_ids(path: Path) -> set[int]:
    """D2에서 확정한 활성 제외 LINK의 segment_id를 읽는다."""
    excluded_ids: set[int] = set()
    with path.open("r", encoding="utf-8-sig", newline="") as file:
        for row in csv.DictReader(file):
            if row["status"].strip().upper() in ACTIVE_EXCLUSION_STATUSES:
                excluded_ids.add(int(row["segment_id"]))
    return excluded_ids


def load_routable_links(
    network_path: Path,
    layer_name: str,
    exclusion_ids: set[int],
) -> gpd.GeoDataFrame:
    """스냅 도보망에서 D2 제외 LINK를 제거하고 CRS·개수를 검증한다."""
    links = gpd.read_file(network_path, layer=layer_name)
    required_columns = {"segment_id", "geometry"}
    missing_columns = required_columns - set(links.columns)
    if missing_columns:
        raise ValueError(f"도보망 필수 컬럼이 없습니다: {sorted(missing_columns)}")
    if links.crs is None or links.crs.to_epsg() != TARGET_EPSG:
        raise ValueError(f"도보망 CRS가 EPSG:{TARGET_EPSG}이 아닙니다: {links.crs}")

    links = links.loc[~links["segment_id"].isin(exclusion_ids)].copy()
    if len(links) != EXPECTED_ROUTABLE_LINK_COUNT:
        raise ValueError(
            f"QA 대상 LINK 수가 다릅니다: {len(links):,} / "
            f"예상 {EXPECTED_ROUTABLE_LINK_COUNT:,}"
        )
    return links


def load_raster_values(height_path: Path, quality_path: Path) -> RasterValues:
    """동일 격자인 건물 높이·품질 래스터를 배열로 읽어 검증한다."""
    height_dataset = gdal.Open(str(height_path), gdal.GA_ReadOnly)
    quality_dataset = gdal.Open(str(quality_path), gdal.GA_ReadOnly)
    if height_dataset is None or quality_dataset is None:
        raise RuntimeError("건물 높이 또는 품질 래스터를 열 수 없습니다.")
    if (
        height_dataset.RasterXSize != quality_dataset.RasterXSize
        or height_dataset.RasterYSize != quality_dataset.RasterYSize
        or height_dataset.GetGeoTransform() != quality_dataset.GetGeoTransform()
    ):
        raise ValueError("건물 높이와 품질 래스터의 격자가 다릅니다.")

    height = height_dataset.GetRasterBand(1).ReadAsArray().astype(np.float32)
    quality = quality_dataset.GetRasterBand(1).ReadAsArray().astype(np.uint8)
    if not np.array_equal(height > 0.0, quality > 0):
        raise ValueError("건물 높이와 품질 래스터의 유효 마스크가 다릅니다.")
    return RasterValues(
        height=height,
        quality=quality,
        geotransform=height_dataset.GetGeoTransform(),
    )


def interpolate_line_points(geometry, interval_m: float) -> list[Point]:
    """LINK를 최대 약 2m 간격의 동일 길이 구간으로 나누고 중심점을 만든다."""
    point_count = max(1, int(np.ceil(geometry.length / interval_m)))
    return [
        geometry.interpolate((index + 0.5) / point_count, normalized=True)
        for index in range(point_count)
    ]


def sample_raster(point: Point, raster: RasterValues) -> tuple[float, int]:
    """점 좌표를 래스터 행·열로 변환해 건물 높이와 품질 코드를 반환한다."""
    origin_x, pixel_x, _, origin_y, _, pixel_y = raster.geotransform
    column = int(np.floor((point.x - origin_x) / pixel_x))
    row = int(np.floor((point.y - origin_y) / pixel_y))
    if (
        row < 0
        or column < 0
        or row >= raster.height.shape[0]
        or column >= raster.height.shape[1]
    ):
        return 0.0, 0
    return float(raster.height[row, column]), int(raster.quality[row, column])


def severity_for_overlap(overlap_m: float, overlap_ratio: float) -> str:
    """QGIS 검토 순서를 정하기 위한 보수적인 중첩 심각도를 부여한다."""
    if overlap_m >= 6.0 and overlap_ratio >= 0.5:
        return "HIGH"
    if overlap_m >= 4.0 and overlap_ratio >= 0.2:
        return "MEDIUM"
    return "LOW"


def analyze_route_overlaps(
    links: gpd.GeoDataFrame,
    raster: RasterValues,
) -> tuple[gpd.GeoDataFrame, gpd.GeoDataFrame, int]:
    """각 LINK의 건물 중첩 비율을 집계하고 실제 중첩 샘플점을 생성한다."""
    candidate_rows: list[dict] = []
    overlap_point_rows: list[dict] = []
    total_sample_count = 0

    for link in links.itertuples(index=False):
        points = interpolate_line_points(link.geometry, SAMPLE_INTERVAL_M)
        total_sample_count += len(points)
        samples = [sample_raster(point, raster) for point in points]
        overlap_indexes = [
            index for index, (height_m, _) in enumerate(samples) if height_m > 0.0
        ]
        if not overlap_indexes:
            continue

        overlap_count = len(overlap_indexes)
        overlap_ratio = overlap_count / len(points)
        overlap_m = min(float(link.geometry.length), overlap_count * SAMPLE_INTERVAL_M)
        overlap_heights = [samples[index][0] for index in overlap_indexes]
        overlap_qualities = [samples[index][1] for index in overlap_indexes]
        candidate_rows.append(
            {
                "segment_id": int(link.segment_id),
                "length_m": float(link.geometry.length),
                "sample_count": len(points),
                "overlap_count": overlap_count,
                "overlap_m": overlap_m,
                "overlap_ratio": overlap_ratio,
                "max_height_m": max(overlap_heights),
                "measured_ratio": overlap_qualities.count(1) / overlap_count,
                "severity": severity_for_overlap(overlap_m, overlap_ratio),
                "geometry": link.geometry,
            }
        )
        for index in overlap_indexes:
            height_m, quality = samples[index]
            overlap_point_rows.append(
                {
                    "segment_id": int(link.segment_id),
                    "seq": index,
                    "height_m": height_m,
                    "quality": quality,
                    "geometry": points[index],
                }
            )

    candidates = gpd.GeoDataFrame(candidate_rows, geometry="geometry", crs=links.crs)
    overlap_points = gpd.GeoDataFrame(
        overlap_point_rows,
        geometry="geometry",
        crs=links.crs,
    )
    if not candidates.empty:
        candidates["severity_order"] = candidates["severity"].map(
            {"HIGH": 1, "MEDIUM": 2, "LOW": 3}
        )
        candidates = candidates.sort_values(
            ["severity_order", "overlap_ratio", "overlap_m"],
            ascending=[True, False, False],
        ).drop(columns="severity_order")
    return candidates, overlap_points, total_sample_count


def write_qa_geopackage(
    output_path: Path,
    candidates: gpd.GeoDataFrame,
    overlap_points: gpd.GeoDataFrame,
    overwrite: bool,
) -> None:
    """QGIS에서 확인할 후보 LINK와 중첩 샘플점 레이어를 저장한다."""
    if output_path.exists():
        if not overwrite:
            raise FileExistsError(
                f"QA 파일이 이미 있습니다: {output_path}. --overwrite를 사용하세요."
            )
        output_path.unlink()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if candidates.empty:
        raise RuntimeError("건물 중첩 후보가 없어 GeoPackage를 만들 수 없습니다.")
    candidates.to_file(output_path, layer="route_overlap_candidates", driver="GPKG")
    overlap_points.to_file(output_path, layer="route_overlap_points", driver="GPKG")


def write_report(
    report_path: Path,
    network_path: Path,
    height_path: Path,
    quality_path: Path,
    output_path: Path,
    link_count: int,
    total_sample_count: int,
    candidates: gpd.GeoDataFrame,
    overlap_points: gpd.GeoDataFrame,
) -> None:
    """DSM과 도보망 중첩 후보의 자동 QA 결과를 Markdown으로 기록한다."""
    severity_counts = candidates["severity"].value_counts().to_dict()
    report = f"""# D3 DSM 공간 QA

## 검사 방식

- 최종 D2 도보망을 약 {SAMPLE_INTERVAL_M:.1f}m 간격으로 등분해 중심점을 샘플링했다.
- 건물 높이 래스터가 0보다 큰 점을 건물 중첩 후보로 분류했다.
- 중첩은 자동 삭제 근거가 아니라 QGIS 육안 검토 후보이다.

## 입력·출력

| 구분 | 경로 |
| --- | --- |
| 도보망 | `{network_path}` |
| 건물 높이 | `{height_path}` |
| 높이 품질 | `{quality_path}` |
| QGIS QA | `{output_path}` |

## 결과

| 지표 | 결과 |
| --- | ---: |
| 최종 도보 LINK | {link_count:,} |
| 2m 샘플점 | {total_sample_count:,} |
| 중첩 후보 LINK | {len(candidates):,} |
| 중첩 샘플점 | {len(overlap_points):,} |
| HIGH | {severity_counts.get('HIGH', 0):,} |
| MEDIUM | {severity_counts.get('MEDIUM', 0):,} |
| LOW | {severity_counts.get('LOW', 0):,} |

## QGIS 확인 순서

1. `route_overlap_candidates`를 `severity`로 분류해 HIGH부터 확인한다.
2. 실제 보행로가 건물 옆을 지나는데 footprint 오차로 겹친 경우는 유지한다.
3. 실내·통행불가 건물 내부를 통과하는 경우만 D2 제외 후보로 별도 기록한다.
4. 원본 도보망이나 DSM을 즉시 수정하지 않고 판정 근거를 먼저 남긴다.
"""
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(report, encoding="utf-8")


def main() -> None:
    """DSM 도로 침범 자동 검사와 QGIS 검토 자료 생성을 실행한다."""
    arguments = parse_arguments()
    network_path = arguments.network.resolve()
    height_path = arguments.height.resolve()
    quality_path = arguments.quality.resolve()
    exclusion_path = arguments.exclusions.resolve()
    output_path = arguments.output.resolve()
    report_path = arguments.report.resolve()
    validate_input_paths([network_path, height_path, quality_path, exclusion_path])

    exclusion_ids = read_active_exclusion_ids(exclusion_path)
    links = load_routable_links(network_path, arguments.network_layer, exclusion_ids)
    raster = load_raster_values(height_path, quality_path)
    candidates, overlap_points, total_sample_count = analyze_route_overlaps(links, raster)
    write_qa_geopackage(
        output_path,
        candidates,
        overlap_points,
        overwrite=arguments.overwrite,
    )
    write_report(
        report_path,
        network_path,
        height_path,
        quality_path,
        output_path,
        len(links),
        total_sample_count,
        candidates,
        overlap_points,
    )

    print(f"[도보 LINK] {len(links):,}")
    print(f"[2m 샘플점] {total_sample_count:,}")
    print(f"[중첩 후보 LINK] {len(candidates):,}")
    print(f"[중첩 샘플점] {len(overlap_points):,}")
    print(f"[QGIS QA] {output_path}")
    print(f"[보고서] {report_path}")


if __name__ == "__main__":
    main()
