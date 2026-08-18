from __future__ import annotations

import argparse
import csv
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import psycopg2
from osgeo import gdal
from psycopg2.extras import execute_values

from calc_shadow import (
    DEFAULT_DATE,
    DEFAULT_TIMES,
    DEFAULT_TIMEZONE,
    NODATA_VALUE,
    assert_same_grid,
    build_timestamps,
    open_raster,
)
from calc_shadow_sources import (
    BUILDING_CODE,
    NONE_CODE,
    SOURCE_LABELS,
    source_output_path,
)
from db_config import add_target_argument, build_db_config, describe_db_target


gdal.UseExceptions()

PIPELINE_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE_DIR = PIPELINE_ROOT / "data" / "processed" / "shadow_sources"
DEFAULT_BUILDING_HEIGHT_PATH = (
    PIPELINE_ROOT / "data" / "processed" / "surface" / "junggu_building_height_2m.tif"
)
DEFAULT_DECISION_PATH = PIPELINE_ROOT / "config" / "dsm_overlap_decisions.csv"
DEFAULT_REPORT_PATH = PIPELINE_ROOT / "reports" / "d3_shade_sampling_qa.md"
EXPECTED_SAMPLE_COUNT = 31_167
TIME_KEYS = ("09", "12", "15", "18")
POLICY_FORCE = "FORCE_BUILDING_SHADE"
POLICY_NEAREST = "SAMPLE_NEAREST_GROUND"
NONE_LABEL = SOURCE_LABELS[NONE_CODE]
BUILDING_LABEL = SOURCE_LABELS[BUILDING_CODE]
VALID_SOURCE_CODES = set(SOURCE_LABELS) | {NODATA_VALUE}
VALID_SOURCE_LABELS = set(SOURCE_LABELS.values())

RATIO_SELECT_SQL = """
SELECT segment_id,
    ROUND(COUNT(*) FILTER (WHERE shade_src_09='T')::NUMERIC/NULLIF(COUNT(shade_src_09),0),3) tree09,
    ROUND(COUNT(*) FILTER (WHERE shade_src_12='T')::NUMERIC/NULLIF(COUNT(shade_src_12),0),3) tree12,
    ROUND(COUNT(*) FILTER (WHERE shade_src_15='T')::NUMERIC/NULLIF(COUNT(shade_src_15),0),3) tree15,
    ROUND(COUNT(*) FILTER (WHERE shade_src_18='T')::NUMERIC/NULLIF(COUNT(shade_src_18),0),3) tree18,
    ROUND(COUNT(*) FILTER (WHERE shade_src_09='B')::NUMERIC/NULLIF(COUNT(shade_src_09),0),3) bldg09,
    ROUND(COUNT(*) FILTER (WHERE shade_src_12='B')::NUMERIC/NULLIF(COUNT(shade_src_12),0),3) bldg12,
    ROUND(COUNT(*) FILTER (WHERE shade_src_15='B')::NUMERIC/NULLIF(COUNT(shade_src_15),0),3) bldg15,
    ROUND(COUNT(*) FILTER (WHERE shade_src_18='B')::NUMERIC/NULLIF(COUNT(shade_src_18),0),3) bldg18,
    ROUND(COUNT(*) FILTER (WHERE shade_src_09 IN ('R','B','T'))::NUMERIC/NULLIF(COUNT(shade_src_09),0),3) shade09,
    ROUND(COUNT(*) FILTER (WHERE shade_src_12 IN ('R','B','T'))::NUMERIC/NULLIF(COUNT(shade_src_12),0),3) shade12,
    ROUND(COUNT(*) FILTER (WHERE shade_src_15 IN ('R','B','T'))::NUMERIC/NULLIF(COUNT(shade_src_15),0),3) shade15,
    ROUND(COUNT(*) FILTER (WHERE shade_src_18 IN ('R','B','T'))::NUMERIC/NULLIF(COUNT(shade_src_18),0),3) shade18
FROM segment_sample_point GROUP BY segment_id
"""


@dataclass(frozen=True)
class Decision:
    segment_id: int
    policy: str
    status: str


@dataclass(frozen=True)
class SampleResult:
    sample_id: int
    segment_id: int
    sources: tuple[str | None, str | None, str | None, str | None]
    overlapped_building: bool
    applied_policy: str | None
    nearest_distance_m: float | None
    provisional: bool

    def database_row(self) -> tuple[Any, ...]:
        values: list[Any] = [self.sample_id]
        for source in self.sources:
            values.extend((None if source is None else source != NONE_LABEL, source))
        return tuple(values)


@dataclass(frozen=True)
class RasterInputs:
    geotransform: tuple[float, float, float, float, float, float]
    width: int
    height: int
    sources: tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]
    valid: np.ndarray
    building: np.ndarray
    pixel_size_m: float


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="D3 원인 래스터를 산책로 분할점에 샘플링하고 route_segment 비율을 집계합니다."
    )
    parser.add_argument("--source-dir", type=Path, default=DEFAULT_SOURCE_DIR)
    parser.add_argument("--building-height", type=Path, default=DEFAULT_BUILDING_HEIGHT_PATH)
    parser.add_argument("--decisions", type=Path, default=DEFAULT_DECISION_PATH)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT_PATH)
    parser.add_argument("--maximum-nearest-ground-m", type=float, default=30.0)
    parser.add_argument("--apply", action="store_true", help="검증된 staging 결과를 한 트랜잭션으로 DB에 반영합니다.")
    add_target_argument(parser)
    return parser.parse_args()


def world_to_cell(
    geotransform: tuple[float, float, float, float, float, float],
    x: float,
    y: float,
) -> tuple[int, int]:
    origin_x, pixel_x, rotation_x, origin_y, rotation_y, pixel_y = geotransform
    if abs(rotation_x) > 1e-12 or abs(rotation_y) > 1e-12 or pixel_x <= 0 or pixel_y >= 0:
        raise ValueError("회전 없는 north-up 래스터만 샘플링할 수 있습니다.")
    column = int(np.floor((x - origin_x) / pixel_x))
    row = int(np.floor((y - origin_y) / pixel_y))
    return row, column


def nearest_ground_cell(
    building: np.ndarray,
    valid: np.ndarray,
    row: int,
    column: int,
    maximum_distance_cells: int,
) -> tuple[int, int, float]:
    if maximum_distance_cells < 1:
        raise ValueError("최대 탐색 거리는 최소 1셀이어야 합니다.")
    row_min = max(0, row - maximum_distance_cells)
    row_max = min(building.shape[0] - 1, row + maximum_distance_cells)
    column_min = max(0, column - maximum_distance_cells)
    column_max = min(building.shape[1] - 1, column + maximum_distance_cells)
    candidates: list[tuple[int, int, int]] = []
    maximum_squared = maximum_distance_cells * maximum_distance_cells
    for candidate_row in range(row_min, row_max + 1):
        for candidate_column in range(column_min, column_max + 1):
            distance_squared = (candidate_row - row) ** 2 + (candidate_column - column) ** 2
            if (
                0 < distance_squared <= maximum_squared
                and valid[candidate_row, candidate_column]
                and not building[candidate_row, candidate_column]
            ):
                candidates.append((distance_squared, candidate_row, candidate_column))
    if not candidates:
        raise ValueError(
            f"{maximum_distance_cells}셀 이내에 유효한 비건물 지면 셀이 없습니다: row={row}, col={column}"
        )
    distance_squared, candidate_row, candidate_column = min(candidates)
    return candidate_row, candidate_column, float(np.sqrt(distance_squared))


def load_decisions(path: Path) -> dict[int, Decision]:
    if not path.exists():
        raise FileNotFoundError(f"DSM 중첩 판정 파일이 없습니다: {path}")
    decisions: dict[int, Decision] = {}
    with path.open("r", encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file)
        required = {"segment_id", "shadow_policy", "status"}
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise ValueError("중첩 판정 파일 필수 컬럼이 없습니다: " + ", ".join(sorted(missing)))
        for row in reader:
            segment_id = int(row["segment_id"])
            policy = row["shadow_policy"].strip().upper()
            status = row["status"].strip().upper()
            if policy not in {POLICY_FORCE, POLICY_NEAREST}:
                raise ValueError(f"지원하지 않는 shadow_policy입니다: {segment_id}={policy}")
            if segment_id in decisions:
                raise ValueError(f"중첩 판정 segment_id가 중복되었습니다: {segment_id}")
            decisions[segment_id] = Decision(segment_id, policy, status)
    return decisions


def load_rasters(source_dir: Path, building_height_path: Path) -> RasterInputs:
    timestamps = build_timestamps(DEFAULT_DATE, list(DEFAULT_TIMES), DEFAULT_TIMEZONE)
    source_paths = [source_output_path(source_dir, timestamp) for timestamp in timestamps]
    datasets = [open_raster(path) for path in source_paths]
    reference = datasets[0]
    for dataset, label in zip(datasets[1:], TIME_KEYS[1:]):
        assert_same_grid(reference, dataset, f"{label}시 원인 래스터")
    building_dataset = open_raster(building_height_path)
    assert_same_grid(reference, building_dataset, "건물 높이 래스터")
    sources = tuple(
        dataset.GetRasterBand(1).ReadAsArray().astype(np.uint8) for dataset in datasets
    )
    valid = np.logical_and.reduce([source != NODATA_VALUE for source in sources])
    for source, label in zip(sources, TIME_KEYS):
        unexpected = set(np.unique(source).tolist()) - VALID_SOURCE_CODES
        if unexpected:
            raise ValueError(f"{label}시 원인 래스터에 계약 밖 값이 있습니다: {sorted(unexpected)}")
    building_height = building_dataset.GetRasterBand(1).ReadAsArray().astype(np.float32)
    building = valid & np.isfinite(building_height) & (building_height > 0.1)
    result = RasterInputs(
        geotransform=tuple(reference.GetGeoTransform()),
        width=reference.RasterXSize,
        height=reference.RasterYSize,
        sources=sources,
        valid=valid,
        building=building,
        pixel_size_m=abs(float(reference.GetGeoTransform()[1])),
    )
    datasets = []
    reference = building_dataset = None
    return result


def fetch_samples(db_config: dict[str, str | int]) -> list[tuple[int, int, float, float]]:
    with psycopg2.connect(**db_config) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT sample_id, segment_id, ST_X(geom), ST_Y(geom)
                FROM segment_sample_point
                ORDER BY sample_id
                """
            )
            rows = [(int(a), int(b), float(c), float(d)) for a, b, c, d in cursor.fetchall()]
    if len(rows) != EXPECTED_SAMPLE_COUNT:
        raise RuntimeError(f"sample 수가 다릅니다: {len(rows):,} / 예상 {EXPECTED_SAMPLE_COUNT:,}")
    return rows


def build_staging_results(
    samples: list[tuple[int, int, float, float]],
    rasters: RasterInputs,
    decisions: dict[int, Decision],
    maximum_nearest_ground_m: float,
) -> list[SampleResult]:
    maximum_cells = int(np.floor(maximum_nearest_ground_m / rasters.pixel_size_m))
    if maximum_cells < 1:
        raise ValueError("--maximum-nearest-ground-m은 래스터 1셀 이상이어야 합니다.")
    sample_segment_ids = {segment_id for _, segment_id, _, _ in samples}
    missing_decision_segments = set(decisions) - sample_segment_ids
    if missing_decision_segments:
        missing = ", ".join(map(str, sorted(missing_decision_segments)))
        raise ValueError(f"DB sample에 없는 수동 판정 segment_id가 있습니다: {missing}")
    results: list[SampleResult] = []
    for sample_id, segment_id, x, y in samples:
        row, column = world_to_cell(rasters.geotransform, x, y)
        in_bounds = 0 <= row < rasters.height and 0 <= column < rasters.width
        if not in_bounds or not rasters.valid[row, column]:
            results.append(
                SampleResult(
                    sample_id=sample_id,
                    segment_id=segment_id,
                    sources=(None, None, None, None),
                    overlapped_building=False,
                    applied_policy=None,
                    nearest_distance_m=None,
                    provisional=False,
                )
            )
            continue
        overlapped = bool(rasters.building[row, column])
        decision = decisions.get(segment_id) if overlapped else None
        applied_policy = decision.policy if decision else None
        nearest_distance_m: float | None = None
        sample_row, sample_column = row, column
        if decision and decision.policy == POLICY_FORCE:
            source_labels = (BUILDING_LABEL,) * len(TIME_KEYS)
        else:
            if decision and decision.policy == POLICY_NEAREST:
                sample_row, sample_column, distance_cells = nearest_ground_cell(
                    rasters.building, rasters.valid, row, column, maximum_cells
                )
                nearest_distance_m = distance_cells * rasters.pixel_size_m
            source_labels = tuple(
                SOURCE_LABELS[int(source[sample_row, sample_column])]
                for source in rasters.sources
            )
        if any(label not in VALID_SOURCE_LABELS for label in source_labels):
            raise RuntimeError(f"잘못된 원인 코드입니다: sample_id={sample_id}, {source_labels}")
        results.append(
            SampleResult(
                sample_id=sample_id,
                segment_id=segment_id,
                sources=source_labels,
                overlapped_building=overlapped,
                applied_policy=applied_policy,
                nearest_distance_m=nearest_distance_m,
                provisional=bool(decision and decision.status == "PROVISIONAL"),
            )
        )
    return results


def apply_results(
    db_config: dict[str, str | int], results: list[SampleResult]
) -> dict[str, int | float]:
    rows = [result.database_row() for result in results]
    with psycopg2.connect(**db_config) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                CREATE TEMP TABLE d3_shadow_staging (
                    sample_id BIGINT PRIMARY KEY,
                    is_shaded_09 BOOLEAN, shade_src_09 CHAR(1),
                    is_shaded_12 BOOLEAN, shade_src_12 CHAR(1),
                    is_shaded_15 BOOLEAN, shade_src_15 CHAR(1),
                    is_shaded_18 BOOLEAN, shade_src_18 CHAR(1)
                ) ON COMMIT DROP
                """
            )
            execute_values(
                cursor,
                "INSERT INTO d3_shadow_staging VALUES %s",
                rows,
                page_size=2_000,
            )
            cursor.execute("SELECT COUNT(*) FROM d3_shadow_staging")
            if int(cursor.fetchone()[0]) != EXPECTED_SAMPLE_COUNT:
                raise RuntimeError("D3 staging sample 수가 예상과 다릅니다.")
            cursor.execute(
                """
                UPDATE segment_sample_point AS target SET
                    is_shaded_09=source.is_shaded_09, shade_src_09=source.shade_src_09,
                    is_shaded_12=source.is_shaded_12, shade_src_12=source.shade_src_12,
                    is_shaded_15=source.is_shaded_15, shade_src_15=source.shade_src_15,
                    is_shaded_18=source.is_shaded_18, shade_src_18=source.shade_src_18
                FROM d3_shadow_staging AS source
                WHERE target.sample_id=source.sample_id
                  AND (target.is_shaded_09, target.shade_src_09,
                       target.is_shaded_12, target.shade_src_12,
                       target.is_shaded_15, target.shade_src_15,
                       target.is_shaded_18, target.shade_src_18)
                      IS DISTINCT FROM
                      (source.is_shaded_09, source.shade_src_09,
                       source.is_shaded_12, source.shade_src_12,
                       source.is_shaded_15, source.shade_src_15,
                       source.is_shaded_18, source.shade_src_18)
                """
            )
            updated_count = int(cursor.rowcount)
            changed_before_update = updated_count
            cursor.execute(
                "CREATE TEMP TABLE d3_ratio_staging ON COMMIT DROP AS " + RATIO_SELECT_SQL
            )
            cursor.execute(
                """
                UPDATE route_segment AS segment SET
                    tree_shade_ratio_09=tree09, tree_shade_ratio_12=tree12,
                    tree_shade_ratio_15=tree15, tree_shade_ratio_18=tree18,
                    bldg_shade_ratio_09=bldg09, bldg_shade_ratio_12=bldg12,
                    bldg_shade_ratio_15=bldg15, bldg_shade_ratio_18=bldg18,
                    shade_ratio_09=shade09, shade_ratio_12=shade12,
                    shade_ratio_15=shade15, shade_ratio_18=shade18
                FROM d3_ratio_staging AS ratios
                WHERE segment.segment_id=ratios.segment_id
                  AND (segment.tree_shade_ratio_09,segment.tree_shade_ratio_12,
                       segment.tree_shade_ratio_15,segment.tree_shade_ratio_18,
                       segment.bldg_shade_ratio_09,segment.bldg_shade_ratio_12,
                       segment.bldg_shade_ratio_15,segment.bldg_shade_ratio_18,
                       segment.shade_ratio_09,segment.shade_ratio_12,
                       segment.shade_ratio_15,segment.shade_ratio_18)
                      IS DISTINCT FROM
                      (ratios.tree09,ratios.tree12,ratios.tree15,ratios.tree18,
                       ratios.bldg09,ratios.bldg12,ratios.bldg15,ratios.bldg18,
                       ratios.shade09,ratios.shade12,ratios.shade15,ratios.shade18)
                """
            )
            segment_updated_count = int(cursor.rowcount)
            cursor.execute(
                """
                SELECT
                    COUNT(*) FILTER (WHERE (shade_src_09 IS NOT NULL AND shade_src_09 NOT IN ('N','R','B','T'))
                                         OR (shade_src_12 IS NOT NULL AND shade_src_12 NOT IN ('N','R','B','T'))
                                         OR (shade_src_15 IS NOT NULL AND shade_src_15 NOT IN ('N','R','B','T'))
                                         OR (shade_src_18 IS NOT NULL AND shade_src_18 NOT IN ('N','R','B','T'))
                                         OR NUM_NULLS(shade_src_09,shade_src_12,shade_src_15,shade_src_18) NOT IN (0,4)),
                    COUNT(*) FILTER (WHERE is_shaded_09 IS DISTINCT FROM (shade_src_09<>'N')
                                         OR is_shaded_12 IS DISTINCT FROM (shade_src_12<>'N')
                                         OR is_shaded_15 IS DISTINCT FROM (shade_src_15<>'N')
                                         OR is_shaded_18 IS DISTINCT FROM (shade_src_18<>'N'))
                FROM segment_sample_point
                """
            )
            invalid_code_count, boolean_mismatch_count = map(int, cursor.fetchone())
            cursor.execute(
                """
                SELECT COUNT(*) FROM route_segment
                WHERE tree_shade_ratio_09 NOT BETWEEN 0 AND 1
                   OR tree_shade_ratio_12 NOT BETWEEN 0 AND 1
                   OR tree_shade_ratio_15 NOT BETWEEN 0 AND 1
                   OR tree_shade_ratio_18 NOT BETWEEN 0 AND 1
                   OR bldg_shade_ratio_09 NOT BETWEEN 0 AND 1
                   OR bldg_shade_ratio_12 NOT BETWEEN 0 AND 1
                   OR bldg_shade_ratio_15 NOT BETWEEN 0 AND 1
                   OR bldg_shade_ratio_18 NOT BETWEEN 0 AND 1
                   OR shade_ratio_09 NOT BETWEEN 0 AND 1
                   OR shade_ratio_12 NOT BETWEEN 0 AND 1
                   OR shade_ratio_15 NOT BETWEEN 0 AND 1
                   OR shade_ratio_18 NOT BETWEEN 0 AND 1
                """
            )
            invalid_ratio_count = int(cursor.fetchone()[0])
            cursor.execute(
                """
                SELECT
                    COUNT(*) FILTER (
                        WHERE NUM_NULLS(shade_src_09,shade_src_12,shade_src_15,shade_src_18)=4
                    ),
                    COUNT(*) FILTER (
                        WHERE NUM_NULLS(shade_src_09,shade_src_12,shade_src_15,shade_src_18) NOT IN (0,4)
                    )
                FROM segment_sample_point
                """
            )
            uncovered_sample_count, partial_null_sample_count = map(int, cursor.fetchone())
            cursor.execute(
                """
                SELECT
                    COUNT(*) FILTER (WHERE
                        (segment.tree_shade_ratio_09,segment.tree_shade_ratio_12,
                         segment.tree_shade_ratio_15,segment.tree_shade_ratio_18,
                         segment.bldg_shade_ratio_09,segment.bldg_shade_ratio_12,
                         segment.bldg_shade_ratio_15,segment.bldg_shade_ratio_18,
                         segment.shade_ratio_09,segment.shade_ratio_12,
                         segment.shade_ratio_15,segment.shade_ratio_18)
                        IS DISTINCT FROM
                        (expected.tree09,expected.tree12,expected.tree15,expected.tree18,
                         expected.bldg09,expected.bldg12,expected.bldg15,expected.bldg18,
                         expected.shade09,expected.shade12,expected.shade15,expected.shade18)),
                    COUNT(*) FILTER (WHERE expected.shade09 IS NULL)
                FROM route_segment AS segment
                JOIN d3_ratio_staging AS expected USING (segment_id)
                """
            )
            aggregate_mismatch_count, uncovered_segment_count = map(int, cursor.fetchone())
            if (
                invalid_code_count
                or boolean_mismatch_count
                or invalid_ratio_count
                or partial_null_sample_count
                or aggregate_mismatch_count
            ):
                raise RuntimeError(
                    "DB 반영 후 품질검사 실패: "
                    f"code={invalid_code_count}, boolean={boolean_mismatch_count}, "
                    f"ratio={invalid_ratio_count}, partial_null={partial_null_sample_count}, "
                    f"aggregate={aggregate_mismatch_count}"
                )
    return {
        "changed_before_update": changed_before_update,
        "updated_count": updated_count,
        "segment_updated_count": segment_updated_count,
        "invalid_code_count": invalid_code_count,
        "boolean_mismatch_count": boolean_mismatch_count,
        "invalid_ratio_count": invalid_ratio_count,
        "uncovered_sample_count": uncovered_sample_count,
        "partial_null_sample_count": partial_null_sample_count,
        "uncovered_segment_count": uncovered_segment_count,
        "aggregate_mismatch_count": aggregate_mismatch_count,
    }


def summarize(results: list[SampleResult]) -> dict[str, Any]:
    source_counts = {
        time: Counter(result.sources[index] for result in results)
        for index, time in enumerate(TIME_KEYS)
    }
    uncovered_segment_ids = {
        result.segment_id for result in results if result.sources[0] is None
    }
    nearest = [
        result.nearest_distance_m
        for result in results
        if result.nearest_distance_m is not None
    ]
    return {
        "sample_count": len(results),
        "valid_sample_count": sum(result.sources[0] is not None for result in results),
        "uncovered_sample_count": sum(result.sources[0] is None for result in results),
        "uncovered_segment_count": len(uncovered_segment_ids),
        "building_overlap_count": sum(result.overlapped_building for result in results),
        "force_count": sum(result.applied_policy == POLICY_FORCE for result in results),
        "nearest_count": len(nearest),
        "provisional_count": sum(result.provisional for result in results),
        "nearest_max_m": max(nearest, default=0.0),
        "source_counts": source_counts,
    }


def write_report(
    path: Path,
    statistics: dict[str, Any],
    database_statistics: dict[str, int | float] | None,
    maximum_nearest_ground_m: float,
) -> None:
    source_rows = "\n".join(
        f"| {time}:00 | {counts['N']:,} | {counts['R']:,} | {counts['B']:,} | {counts['T']:,} | {counts[None]:,} |"
        for time, counts in statistics["source_counts"].items()
    )
    mode = "DB 적용 완료" if database_statistics else "dry-run 검증"
    db_rows = ""
    if database_statistics:
        db_rows = "\n".join(
            f"| {key} | {value:,} |" for key, value in database_statistics.items()
        )
    else:
        db_rows = "| DB 변경 | 없음 |"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"""# D3 산책로 그늘 샘플링 QA

- 실행 모드: {mode}
- sample point: {statistics['sample_count']:,}
- 유효 래스터 sample: {statistics['valid_sample_count']:,}
- 래스터 범위 밖/NoData sample: {statistics['uncovered_sample_count']:,} ({statistics['uncovered_segment_count']:,}개 segment)
- 래스터 footprint 기준 건물 중첩 sample: {statistics['building_overlap_count']:,}
- `FORCE_BUILDING_SHADE` 적용 sample: {statistics['force_count']:,}
- `SAMPLE_NEAREST_GROUND` 적용 sample: {statistics['nearest_count']:,}
- provisional 판정 적용 sample: {statistics['provisional_count']:,}
- 최근접 지면 최대 이동: {statistics['nearest_max_m']:.2f}m / 허용 {maximum_nearest_ground_m:.2f}m

수동 정책은 해당 segment의 모든 점이 아니라 실제 건물 높이 래스터(`> 0.1m`)와
중첩된 sample에만 적용했다. 최근접 지면은 거리, row, column 순으로 고정 정렬하여
동일 입력에서 항상 같은 셀을 선택한다.

## 시간대별 원인

| 시각 | N | R | B | T | NoData |
|---|---:|---:|---:|---:|---:|
{source_rows}

## DB 트랜잭션 검증

| 지표 | 값 |
|---|---:|
{db_rows}

`is_shaded_*`는 원인이 `R/B/T`일 때만 true이다. 래스터 범위 밖 점은 다른 장소의
값을 강제 대입하지 않고 4개 시간대 모두 NULL로 보존했다. route_segment 비율은
유효 sample count를 분모로 집계한 뒤 마지막 단계에서만 소수 셋째 자리로 반올림했다.
""",
        encoding="utf-8",
    )


def main() -> None:
    args = parse_arguments()
    if args.maximum_nearest_ground_m <= 0:
        raise ValueError("--maximum-nearest-ground-m은 0보다 커야 합니다.")
    db_config = build_db_config(args.target)
    print(f"[DB 연결] {describe_db_target(args.target, db_config)}")
    rasters = load_rasters(args.source_dir, args.building_height)
    decisions = load_decisions(args.decisions)
    samples = fetch_samples(db_config)
    results = build_staging_results(
        samples, rasters, decisions, args.maximum_nearest_ground_m
    )
    statistics = summarize(results)
    database_statistics = apply_results(db_config, results) if args.apply else None
    write_report(args.report, statistics, database_statistics, args.maximum_nearest_ground_m)
    print(
        f"[샘플] {statistics['sample_count']:,}개, 건물 중첩 {statistics['building_overlap_count']:,}개, "
        f"강제 B {statistics['force_count']:,}개, 최근접 지면 {statistics['nearest_count']:,}개"
    )
    print(f"[모드] {'DB 적용' if args.apply else 'dry-run'}")
    print(f"[보고서] {args.report.resolve()}")


if __name__ == "__main__":
    main()
