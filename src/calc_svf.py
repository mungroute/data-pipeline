from __future__ import annotations

import argparse
import csv
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import psycopg2
from osgeo import gdal, ogr, osr
from psycopg2.extras import execute_values

from calc_shadow import (
    DEFAULT_DSM_PATH,
    DEFAULT_DTM_PATH,
    assert_same_grid,
    open_raster,
)
from load_segments import build_db_config


gdal.UseExceptions()

PIPELINE_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DECISION_PATH = PIPELINE_ROOT / "config" / "dsm_overlap_decisions.csv"
DEFAULT_REPORT_PATH = PIPELINE_ROOT / "reports" / "d4_svf_qa.md"
DEFAULT_QA_GPKG_PATH = (
    PIPELINE_ROOT / "data" / "interim" / "surface" / "d4_svf_qa.gpkg"
)
EXPECTED_SAMPLE_COUNT = 31_167
EXPECTED_SEGMENT_COUNT = 7_766
DEFAULT_DIRECTION_COUNT = 36
DEFAULT_SEARCH_RADIUS_M = 250.0
DEFAULT_OBSERVER_HEIGHT_M = 1.5
DEFAULT_NEAREST_GROUND_M = 30.0
POLICY_FORCE = "FORCE_BUILDING_SHADE"
POLICY_NEAREST = "SAMPLE_NEAREST_GROUND"


@dataclass(frozen=True)
class Decision:
    segment_id: int
    policy: str
    status: str


@dataclass(frozen=True)
class RasterGrid:
    geotransform: tuple[float, float, float, float, float, float]
    width: int
    height: int
    pixel_size_m: float
    dtm: np.ndarray
    obstacle_surface: np.ndarray
    valid: np.ndarray
    building: np.ndarray


@dataclass(frozen=True)
class SampleLocation:
    sample_id: int
    segment_id: int
    row: int
    column: int
    policy: str | None
    nearest_distance_m: float | None


@dataclass(frozen=True)
class SvfResult:
    sample_id: int
    segment_id: int
    svf: float | None
    policy: str | None
    nearest_distance_m: float | None

    def database_row(self) -> tuple[int, float | None]:
        return self.sample_id, None if self.svf is None else round(self.svf, 3)


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="D4 건물 전용 36방위 horizon 방식으로 샘플점 SVF를 계산하고 링크 평균을 적재합니다."
    )
    parser.add_argument("--dtm", type=Path, default=DEFAULT_DTM_PATH)
    parser.add_argument("--dsm", type=Path, default=DEFAULT_DSM_PATH)
    parser.add_argument("--decisions", type=Path, default=DEFAULT_DECISION_PATH)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT_PATH)
    parser.add_argument("--qa-gpkg", type=Path, default=DEFAULT_QA_GPKG_PATH)
    parser.add_argument("--directions", type=int, default=DEFAULT_DIRECTION_COUNT)
    parser.add_argument("--search-radius-m", type=float, default=DEFAULT_SEARCH_RADIUS_M)
    parser.add_argument("--observer-height-m", type=float, default=DEFAULT_OBSERVER_HEIGHT_M)
    parser.add_argument(
        "--maximum-nearest-ground-m", type=float, default=DEFAULT_NEAREST_GROUND_M
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="검증된 결과를 segment_sample_point와 route_segment에 반영합니다.",
    )
    return parser.parse_args()


def build_building_obstacle_surface(
    dtm: np.ndarray, dsm: np.ndarray, valid: np.ndarray
) -> np.ndarray:
    """건물 DSM만 이용해 SVF용 절대표고 장애물 격자를 생성합니다.

    수목 CDSM은 시간대별 수목 그림자 계산에만 사용합니다. 수관을 불투명한
    고체로 근사한 CDSM을 SVF에도 넣으면 가로수 아래 하늘 차폐가 이중 반영되고
    과대 계산되므로 의도적으로 제외합니다.
    """
    obstacle_surface = np.maximum(dsm, dtm).astype(np.float32)
    obstacle_surface[~valid | ~np.isfinite(dsm)] = np.nan
    return obstacle_surface


def load_raster_grid(dtm_path: Path, dsm_path: Path) -> RasterGrid:
    """DTM과 건물 DSM을 건물 전용 SVF 장애물 격자로 읽습니다."""
    dtm_dataset = open_raster(dtm_path)
    dsm_dataset = open_raster(dsm_path)
    assert_same_grid(dtm_dataset, dsm_dataset, "DSM")
    if dtm_dataset.RasterCount < 2:
        raise ValueError("DTM에 중구 유효 영역을 나타내는 Alpha band가 없습니다.")

    dtm = dtm_dataset.GetRasterBand(1).ReadAsArray().astype(np.float32)
    alpha = dtm_dataset.GetRasterBand(2).ReadAsArray()
    dsm = dsm_dataset.GetRasterBand(1).ReadAsArray().astype(np.float32)
    valid = (alpha > 0) & np.isfinite(dtm) & (dtm != 0.0)
    building = valid & np.isfinite(dsm) & ((dsm - dtm) > 0.1)
    obstacle_surface = build_building_obstacle_surface(dtm, dsm, valid)

    geotransform = tuple(dtm_dataset.GetGeoTransform())
    pixel_size_m = abs(float(geotransform[1]))
    if (
        abs(geotransform[2]) > 1e-12
        or abs(geotransform[4]) > 1e-12
        or geotransform[1] <= 0
        or geotransform[5] >= 0
        or abs(abs(geotransform[5]) - pixel_size_m) > 1e-9
    ):
        raise ValueError("회전 없는 정사각형 north-up 래스터만 지원합니다.")

    grid = RasterGrid(
        geotransform=geotransform,
        width=dtm_dataset.RasterXSize,
        height=dtm_dataset.RasterYSize,
        pixel_size_m=pixel_size_m,
        dtm=dtm,
        obstacle_surface=obstacle_surface,
        valid=valid,
        building=building,
    )
    dtm_dataset = dsm_dataset = None
    return grid


def load_decisions(path: Path) -> dict[int, Decision]:
    """D3에서 확정한 건물 중첩 보행로 정책을 SVF 원점에도 동일하게 적용합니다."""
    if not path.is_file():
        raise FileNotFoundError(f"DSM 중첩 판정 파일이 없습니다: {path}")
    decisions: dict[int, Decision] = {}
    with path.open("r", encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file)
        required = {"segment_id", "shadow_policy", "status"}
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise ValueError("중첩 판정 필수 컬럼이 없습니다: " + ", ".join(sorted(missing)))
        for row in reader:
            segment_id = int(row["segment_id"])
            policy = row["shadow_policy"].strip().upper()
            status = row["status"].strip().upper()
            if policy not in {POLICY_FORCE, POLICY_NEAREST}:
                raise ValueError(f"지원하지 않는 중첩 정책입니다: {segment_id}={policy}")
            decisions[segment_id] = Decision(segment_id, policy, status)
    return decisions


def world_to_cell(
    geotransform: tuple[float, float, float, float, float, float], x: float, y: float
) -> tuple[int, int]:
    """EPSG:5186 좌표를 래스터 행·열로 변환합니다."""
    origin_x, pixel_x, _, origin_y, _, pixel_y = geotransform
    return (
        int(np.floor((y - origin_y) / pixel_y)),
        int(np.floor((x - origin_x) / pixel_x)),
    )


def nearest_ground_cell(
    building: np.ndarray,
    valid: np.ndarray,
    row: int,
    column: int,
    maximum_distance_cells: int,
) -> tuple[int, int, float]:
    """footprint 오차로 건물과 겹친 점을 가장 가까운 비건물 지면으로 옮깁니다."""
    row_min = max(0, row - maximum_distance_cells)
    row_max = min(building.shape[0] - 1, row + maximum_distance_cells)
    column_min = max(0, column - maximum_distance_cells)
    column_max = min(building.shape[1] - 1, column + maximum_distance_cells)
    window_valid = valid[row_min : row_max + 1, column_min : column_max + 1]
    window_ground = ~building[row_min : row_max + 1, column_min : column_max + 1]
    candidate_rows, candidate_columns = np.nonzero(window_valid & window_ground)
    if candidate_rows.size == 0:
        raise ValueError(f"인접 비건물 지면을 찾지 못했습니다: row={row}, col={column}")
    absolute_rows = candidate_rows + row_min
    absolute_columns = candidate_columns + column_min
    distance_squared = (absolute_rows - row) ** 2 + (absolute_columns - column) ** 2
    within = (distance_squared > 0) & (
        distance_squared <= maximum_distance_cells * maximum_distance_cells
    )
    if not np.any(within):
        raise ValueError(f"허용 거리 안의 비건물 지면이 없습니다: row={row}, col={column}")
    indices = np.flatnonzero(within)
    best = indices[np.argmin(distance_squared[indices])]
    return (
        int(absolute_rows[best]),
        int(absolute_columns[best]),
        float(np.sqrt(distance_squared[best])),
    )


def fetch_samples(db_config: dict[str, str | int]) -> list[tuple[int, int, float, float]]:
    """D2에서 만든 공통 10m 샘플점의 ID, 링크 ID, EPSG:5186 좌표를 조회합니다."""
    with psycopg2.connect(**db_config) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT sample_id, segment_id, ST_X(geom), ST_Y(geom)
                FROM segment_sample_point
                ORDER BY sample_id
                """
            )
            rows = [
                (int(a), int(b), float(c), float(d)) for a, b, c, d in cursor.fetchall()
            ]
    if len(rows) != EXPECTED_SAMPLE_COUNT:
        raise RuntimeError(
            f"샘플 수가 예상과 다릅니다: {len(rows):,} / {EXPECTED_SAMPLE_COUNT:,}"
        )
    return rows


def resolve_sample_locations(
    samples: list[tuple[int, int, float, float]],
    grid: RasterGrid,
    decisions: dict[int, Decision],
    maximum_nearest_ground_m: float,
) -> tuple[list[SampleLocation], list[SvfResult]]:
    """유효 샘플 원점을 정하고 DTM 밖 샘플은 명시적인 NULL 결과로 분리합니다."""
    maximum_cells = int(np.floor(maximum_nearest_ground_m / grid.pixel_size_m))
    if maximum_cells < 1:
        raise ValueError("최근접 지면 탐색 거리는 한 픽셀 이상이어야 합니다.")
    locations: list[SampleLocation] = []
    null_results: list[SvfResult] = []
    for sample_id, segment_id, x, y in samples:
        row, column = world_to_cell(grid.geotransform, x, y)
        if not (0 <= row < grid.height and 0 <= column < grid.width) or not grid.valid[
            row, column
        ]:
            null_results.append(SvfResult(sample_id, segment_id, None, None, None))
            continue
        decision = decisions.get(segment_id) if grid.building[row, column] else None
        nearest_distance_m: float | None = None
        if decision and decision.policy == POLICY_NEAREST:
            row, column, distance_cells = nearest_ground_cell(
                grid.building, grid.valid, row, column, maximum_cells
            )
            nearest_distance_m = distance_cells * grid.pixel_size_m
        locations.append(
            SampleLocation(
                sample_id,
                segment_id,
                row,
                column,
                decision.policy if decision else None,
                nearest_distance_m,
            )
        )
    return locations, null_results


def directional_offsets(
    direction_count: int, radius_cells: int
) -> list[list[tuple[int, int, float]]]:
    """각 방위의 중복 없는 래스터 셀 오프셋과 셀 단위 실제 거리를 만듭니다."""
    if direction_count < 4:
        raise ValueError("방위 수는 4 이상이어야 합니다.")
    if radius_cells < 1:
        raise ValueError("탐색 반경은 한 픽셀 이상이어야 합니다.")
    result: list[list[tuple[int, int, float]]] = []
    for azimuth in np.linspace(0.0, 2.0 * np.pi, direction_count, endpoint=False):
        offsets: list[tuple[int, int, float]] = []
        seen: set[tuple[int, int]] = set()
        for distance_cells in range(1, radius_cells + 1):
            row_offset = int(np.rint(-np.cos(azimuth) * distance_cells))
            column_offset = int(np.rint(np.sin(azimuth) * distance_cells))
            key = (row_offset, column_offset)
            if key == (0, 0) or key in seen:
                continue
            seen.add(key)
            offsets.append(
                (row_offset, column_offset, float(np.hypot(row_offset, column_offset)))
            )
        result.append(offsets)
    return result


def calculate_svf_at_cells(
    grid: RasterGrid,
    rows: np.ndarray,
    columns: np.ndarray,
    direction_count: int,
    search_radius_m: float,
    observer_height_m: float,
) -> np.ndarray:
    """방위별 최대 고도각으로 SVF = 1 - mean(sin²φ)를 계산합니다."""
    if rows.shape != columns.shape:
        raise ValueError("샘플 행과 열 배열 크기가 다릅니다.")
    if search_radius_m <= 0.0 or observer_height_m < 0.0:
        raise ValueError("탐색 반경은 양수이고 관측자 높이는 0 이상이어야 합니다.")
    radius_cells = int(np.ceil(search_radius_m / grid.pixel_size_m))
    offsets_by_direction = directional_offsets(direction_count, radius_cells)
    observer_elevation = grid.dtm[rows, columns].astype(np.float64) + observer_height_m
    sin_squared_sum = np.zeros(rows.size, dtype=np.float64)
    valid_direction_count = np.zeros(rows.size, dtype=np.int16)

    for offsets in offsets_by_direction:
        maximum_angle = np.zeros(rows.size, dtype=np.float64)
        direction_has_data = np.zeros(rows.size, dtype=bool)
        for row_offset, column_offset, distance_cells in offsets:
            candidate_rows = rows + row_offset
            candidate_columns = columns + column_offset
            in_bounds = (
                (candidate_rows >= 0)
                & (candidate_rows < grid.height)
                & (candidate_columns >= 0)
                & (candidate_columns < grid.width)
            )
            if not np.any(in_bounds):
                continue
            sample_indices = np.flatnonzero(in_bounds)
            candidate_valid = grid.valid[
                candidate_rows[sample_indices], candidate_columns[sample_indices]
            ]
            sample_indices = sample_indices[candidate_valid]
            if sample_indices.size == 0:
                continue
            direction_has_data[sample_indices] = True
            obstacle_elevation = grid.obstacle_surface[
                candidate_rows[sample_indices], candidate_columns[sample_indices]
            ].astype(np.float64)
            vertical_difference = obstacle_elevation - observer_elevation[sample_indices]
            angles = np.arctan2(
                np.maximum(vertical_difference, 0.0),
                distance_cells * grid.pixel_size_m,
            )
            maximum_angle[sample_indices] = np.maximum(
                maximum_angle[sample_indices], angles
            )
        sin_squared_sum += np.sin(maximum_angle) ** 2
        valid_direction_count += direction_has_data.astype(np.int16)

    result = np.full(rows.size, np.nan, dtype=np.float64)
    covered = valid_direction_count > 0
    result[covered] = 1.0 - (
        sin_squared_sum[covered] / valid_direction_count[covered]
    )
    return np.clip(result, 0.0, 1.0)


def calculate_results(
    locations: list[SampleLocation],
    null_results: list[SvfResult],
    grid: RasterGrid,
    direction_count: int,
    search_radius_m: float,
    observer_height_m: float,
) -> list[SvfResult]:
    """개방 샘플은 horizon 계산, 실제 지붕 아래 샘플은 물리적으로 SVF 0을 적용합니다."""
    calculated_locations = [item for item in locations if item.policy != POLICY_FORCE]
    force_locations = [item for item in locations if item.policy == POLICY_FORCE]
    values = calculate_svf_at_cells(
        grid,
        np.asarray([item.row for item in calculated_locations], dtype=np.int32),
        np.asarray([item.column for item in calculated_locations], dtype=np.int32),
        direction_count,
        search_radius_m,
        observer_height_m,
    )
    results = list(null_results)
    for item, value in zip(calculated_locations, values):
        results.append(
            SvfResult(
                item.sample_id,
                item.segment_id,
                None if not np.isfinite(value) else float(value),
                item.policy,
                item.nearest_distance_m,
            )
        )
    results.extend(
        SvfResult(item.sample_id, item.segment_id, 0.0, item.policy, None)
        for item in force_locations
    )
    results.sort(key=lambda item: item.sample_id)
    if len(results) != EXPECTED_SAMPLE_COUNT:
        raise RuntimeError(f"SVF 결과 수가 예상과 다릅니다: {len(results):,}")
    return results


def summarize_results(results: list[SvfResult]) -> dict[str, Any]:
    """보고서와 적용 전 게이트에서 사용할 커버리지·분포 통계를 계산합니다."""
    values = np.asarray([item.svf for item in results if item.svf is not None], dtype=float)
    if values.size == 0:
        raise RuntimeError("계산된 SVF가 한 건도 없습니다.")
    if np.any((values < 0.0) | (values > 1.0)):
        raise RuntimeError("0~1 범위를 벗어난 SVF가 있습니다.")
    policy_counts = Counter(item.policy or "NORMAL" for item in results)
    distribution = {
        "0.0~0.2": int(np.count_nonzero((values >= 0.0) & (values <= 0.2))),
        "0.2~0.4": int(np.count_nonzero((values > 0.2) & (values <= 0.4))),
        "0.4~0.6": int(np.count_nonzero((values > 0.4) & (values <= 0.6))),
        "0.6~0.8": int(np.count_nonzero((values > 0.6) & (values <= 0.8))),
        "0.8~1.0": int(np.count_nonzero((values > 0.8) & (values <= 1.0))),
    }
    return {
        "total": len(results),
        "filled": int(values.size),
        "null": len(results) - int(values.size),
        "minimum": float(np.min(values)),
        "median": float(np.median(values)),
        "mean": float(np.mean(values)),
        "maximum": float(np.max(values)),
        "p10": float(np.percentile(values, 10)),
        "p25": float(np.percentile(values, 25)),
        "p75": float(np.percentile(values, 75)),
        "p90": float(np.percentile(values, 90)),
        "distribution": distribution,
        "policy_counts": policy_counts,
        "nearest_max_m": max(
            (item.nearest_distance_m or 0.0 for item in results), default=0.0
        ),
    }


def classify_qa_value(value: float | None, policy: str | None) -> str:
    """QGIS에서 SVF 분포와 예외 정책을 빠르게 확인할 QA 등급을 반환한다."""
    if value is None:
        return "NULL"
    if policy == POLICY_FORCE:
        return "COVERED_STRUCTURE"
    if value <= 0.2:
        return "VERY_LOW"
    if value <= 0.4:
        return "LOW"
    if value <= 0.6:
        return "MEDIUM"
    if value <= 0.8:
        return "HIGH"
    return "VERY_HIGH"


def write_qa_gpkg(
    path: Path,
    samples: list[tuple[int, int, float, float]],
    results: list[SvfResult],
) -> None:
    """샘플 좌표와 SVF 결과를 QGIS에서 바로 열 수 있는 GeoPackage로 저장한다."""
    if len(samples) != len(results):
        raise RuntimeError("QA GeoPackage 입력 샘플 수와 SVF 결과 수가 다릅니다.")
    sample_by_id = {
        sample_id: (segment_id, x, y) for sample_id, segment_id, x, y in samples
    }
    if set(sample_by_id) != {item.sample_id for item in results}:
        raise RuntimeError("QA GeoPackage의 sample_id 집합이 SVF 결과와 다릅니다.")

    path.parent.mkdir(parents=True, exist_ok=True)
    driver = ogr.GetDriverByName("GPKG")
    if driver is None:
        raise RuntimeError("GDAL GeoPackage 드라이버를 찾을 수 없습니다.")
    if path.exists():
        driver.DeleteDataSource(str(path))
    dataset = driver.CreateDataSource(str(path))
    if dataset is None:
        raise RuntimeError(f"QA GeoPackage를 만들 수 없습니다: {path}")

    spatial_reference = osr.SpatialReference()
    spatial_reference.ImportFromEPSG(5186)
    layer = dataset.CreateLayer(
        "sample_svf", spatial_reference, ogr.wkbPoint, options=["SPATIAL_INDEX=YES"]
    )
    for name, field_type in (
        ("sample_id", ogr.OFTInteger64),
        ("segment_id", ogr.OFTInteger64),
        ("svf", ogr.OFTReal),
        ("policy", ogr.OFTString),
        ("nearest_m", ogr.OFTReal),
        ("qa_class", ogr.OFTString),
    ):
        field = ogr.FieldDefn(name, field_type)
        if name == "svf":
            field.SetWidth(4)
            field.SetPrecision(3)
        layer.CreateField(field)

    layer.StartTransaction()
    definition = layer.GetLayerDefn()
    for result in results:
        segment_id, x, y = sample_by_id[result.sample_id]
        feature = ogr.Feature(definition)
        feature.SetField("sample_id", result.sample_id)
        feature.SetField("segment_id", segment_id)
        if result.svf is not None:
            feature.SetField("svf", round(result.svf, 3))
        feature.SetField("policy", result.policy or "NORMAL")
        if result.nearest_distance_m is not None:
            feature.SetField("nearest_m", round(result.nearest_distance_m, 2))
        feature.SetField("qa_class", classify_qa_value(result.svf, result.policy))
        geometry = ogr.Geometry(ogr.wkbPoint)
        geometry.AddPoint_2D(x, y)
        feature.SetGeometry(geometry)
        if layer.CreateFeature(feature) != ogr.OGRERR_NONE:
            raise RuntimeError(f"QA 피처 저장 실패: sample_id={result.sample_id}")
    layer.CommitTransaction()
    dataset = None


def apply_results(
    db_config: dict[str, str | int], results: list[SvfResult]
) -> dict[str, int | float | None]:
    """샘플 SVF와 링크 평균을 단일 트랜잭션으로 원자적으로 반영합니다."""
    with psycopg2.connect(**db_config) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                CREATE TEMP TABLE d4_svf_staging (
                    sample_id BIGINT PRIMARY KEY,
                    svf NUMERIC(4,3)
                ) ON COMMIT DROP
                """
            )
            execute_values(
                cursor,
                "INSERT INTO d4_svf_staging (sample_id, svf) VALUES %s",
                [item.database_row() for item in results],
                page_size=2_000,
            )
            cursor.execute("SELECT COUNT(*) FROM d4_svf_staging")
            if int(cursor.fetchone()[0]) != EXPECTED_SAMPLE_COUNT:
                raise RuntimeError("D4 SVF staging 행 수가 예상과 다릅니다.")
            cursor.execute(
                """
                UPDATE segment_sample_point AS target
                SET svf = source.svf
                FROM d4_svf_staging AS source
                WHERE target.sample_id = source.sample_id
                  AND target.svf IS DISTINCT FROM source.svf
                """
            )
            sample_updates = int(cursor.rowcount)
            cursor.execute(
                """
                CREATE TEMP TABLE d4_segment_svf ON COMMIT DROP AS
                SELECT segment_id, ROUND(AVG(svf), 3) AS svf
                FROM segment_sample_point
                GROUP BY segment_id
                """
            )
            cursor.execute(
                """
                UPDATE route_segment AS target
                SET svf = source.svf
                FROM d4_segment_svf AS source
                WHERE target.segment_id = source.segment_id
                  AND target.svf IS DISTINCT FROM source.svf
                """
            )
            segment_updates = int(cursor.rowcount)
            cursor.execute(
                """
                SELECT COUNT(*), COUNT(svf), MIN(svf), MAX(svf), AVG(svf)
                FROM segment_sample_point
                """
            )
            total, filled, minimum, maximum, average = cursor.fetchone()
            cursor.execute("SELECT COUNT(*), COUNT(svf) FROM route_segment")
            segment_total, segment_filled = map(int, cursor.fetchone())
            if int(total) != EXPECTED_SAMPLE_COUNT:
                raise RuntimeError(f"DB 샘플 수가 예상과 다릅니다: {int(total):,}")
            if segment_total != EXPECTED_SEGMENT_COUNT:
                raise RuntimeError(f"DB 링크 수가 예상과 다릅니다: {segment_total:,}")
    return {
        "sample_updates": sample_updates,
        "segment_updates": segment_updates,
        "sample_total": int(total),
        "sample_filled": int(filled),
        "sample_minimum": None if minimum is None else float(minimum),
        "sample_maximum": None if maximum is None else float(maximum),
        "sample_average": None if average is None else float(average),
        "segment_total": segment_total,
        "segment_filled": segment_filled,
    }


def write_report(
    path: Path,
    statistics: dict[str, Any],
    database_statistics: dict[str, int | float | None] | None,
    arguments: argparse.Namespace,
) -> None:
    """입력·알고리즘·정책·커버리지·제약사항을 재현 가능한 QA 문서로 남깁니다."""
    policy_counts: Counter[str] = statistics["policy_counts"]
    distribution: dict[str, int] = statistics["distribution"]
    database_section = "- DB 미적용(dry-run)"
    if database_statistics is not None:
        database_section = "\n".join(
            [
                f"- 샘플 UPDATE: {database_statistics['sample_updates']:,}",
                f"- 링크 UPDATE: {database_statistics['segment_updates']:,}",
                f"- 샘플 커버리지: {database_statistics['sample_filled']:,} / {database_statistics['sample_total']:,}",
                f"- 링크 커버리지: {database_statistics['segment_filled']:,} / {database_statistics['segment_total']:,}",
            ]
        )
    content = f"""# D4 건물 전용 SVF 계산 QA

## 계산 정책

- DB의 `svf`는 DTM과 건물 DSM만 사용한 기하학적 하늘 개방도다.
- 수목 CDSM은 SVF에서 제외하고 D3의 시간대별 수목 그림자 자료로 별도 반영한다.
- 수관 CDSM을 불투명한 고체로 취급해 발생하던 가로수 아래 과대 차폐와 이중 반영을 방지한다.
- 실제 지붕·상부구조 아래로 판정한 `FORCE_BUILDING_SHADE` 구간은 예외적으로 `svf=0`을 유지한다.

## 계산 개요

- 입력 DTM: `{arguments.dtm.resolve()}`
- 입력 건물 DSM: `{arguments.dsm.resolve()}`
- 공통 샘플점: `segment_sample_point` ({statistics['total']:,}개)
- 방위 수: {arguments.directions}
- 최대 탐색 반경: {arguments.search_radius_m:.1f}m
- 관측자 높이: 지면 + {arguments.observer_height_m:.1f}m
- 식: `SVF = 1 - mean(sin²(max_horizon_angle))`

## 중첩 보행로 정책

- 일반 계산: {policy_counts['NORMAL']:,}개
- 건물 footprint 오차로 최근접 지면 승계: {policy_counts[POLICY_NEAREST]:,}개
- 실제 상부 구조 아래라 SVF=0 적용: {policy_counts[POLICY_FORCE]:,}개
- 최근접 지면 최대 이동거리: {statistics['nearest_max_m']:.1f}m

## 계산 결과

- 유효 SVF: {statistics['filled']:,}개
- DTM/Alpha 범위 밖 NULL: {statistics['null']:,}개
- 최소 / 중앙 / 평균 / 최대: {statistics['minimum']:.3f} / {statistics['median']:.3f} / {statistics['mean']:.3f} / {statistics['maximum']:.3f}
- P10 / P25 / P75 / P90: {statistics['p10']:.3f} / {statistics['p25']:.3f} / {statistics['p75']:.3f} / {statistics['p90']:.3f}

| SVF 구간 | 샘플 수 |
| --- | ---: |
| 0.0~0.2 | {distribution['0.0~0.2']:,} |
| 0.2~0.4 | {distribution['0.2~0.4']:,} |
| 0.4~0.6 | {distribution['0.4~0.6']:,} |
| 0.6~0.8 | {distribution['0.6~0.8']:,} |
| 0.8~1.0 | {distribution['0.8~1.0']:,} |

## DB 반영

{database_section}

## 수목 CDSM 제외 근거

- 장충단로 가로수 인접 샘플 202개 중 기존 수목 포함 SVF가 0.2 이하인 점은 103개였다.
- 해당 103개의 평균은 수목 포함 0.055, 건물 전용 0.769로 차이가 0.713이었다.
- 저SVF 점의 97.1%가 불투명 원기둥으로 근사한 수관 픽셀 안에 있어 수목 차폐 과대로 판정했다.

## 해석 및 제한

- SVF는 시간·날씨와 무관한 정적 하늘 개방도이며 값의 범위는 0~1입니다.
- 중구 Alpha 경계 바깥 장애물은 입력에 없으므로 경계 인접 지점은 SVF가 높게 추정될 수 있습니다.
- QGIS QA 산출물: `{arguments.qa_gpkg.resolve()}`의 `sample_svf` 레이어
- 국립극장·장충단로의 개방 구간, 좁은 골목, 실제 상부구조 구간을 함께 시각 확인합니다.
"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def main() -> None:
    arguments = parse_arguments()
    if arguments.directions < 4:
        raise ValueError("--directions는 4 이상이어야 합니다.")
    grid = load_raster_grid(arguments.dtm, arguments.dsm)
    decisions = load_decisions(arguments.decisions)
    db_config = build_db_config()
    samples = fetch_samples(db_config)
    locations, null_results = resolve_sample_locations(
        samples, grid, decisions, arguments.maximum_nearest_ground_m
    )
    print(
        f"[SVF] {len(samples):,}개 샘플, {arguments.directions}방위, "
        f"반경 {arguments.search_radius_m:.0f}m 계산 시작"
    )
    results = calculate_results(
        locations,
        null_results,
        grid,
        arguments.directions,
        arguments.search_radius_m,
        arguments.observer_height_m,
    )
    statistics = summarize_results(results)
    write_qa_gpkg(arguments.qa_gpkg, samples, results)
    database_statistics = apply_results(db_config, results) if arguments.apply else None
    write_report(arguments.report, statistics, database_statistics, arguments)
    print(
        f"[SVF 완료] 유효 {statistics['filled']:,}, NULL {statistics['null']:,}, "
        f"평균 {statistics['mean']:.3f}, 범위 "
        f"{statistics['minimum']:.3f}~{statistics['maximum']:.3f}"
    )
    print(f"[QA 보고서] {arguments.report.resolve()}")
    if not arguments.apply:
        print("[안내] dry-run입니다. 검증 후 --apply로 DB에 반영하세요.")


if __name__ == "__main__":
    main()
