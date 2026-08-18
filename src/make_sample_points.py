import argparse
from pathlib import Path
from typing import Any

import psycopg2

from db_config import (
    add_replace_argument,
    add_target_argument,
    build_db_config,
    describe_db_target,
    require_dev_replace_permission,
)


PIPELINE_ROOT = Path(__file__).resolve().parents[1]
REPORT_PATH = PIPELINE_ROOT / "reports" / "d2_sample_points_qa.md"
SAMPLE_INTERVAL_M = 10.0
EXPECTED_SEGMENT_COUNT = 7_766
EXPECTED_SAMPLE_COUNT = 31_167


def rebuild_sample_points(
    db_config: dict[str, str | int],
    target: str = "local",
    allow_replace: bool = False,
) -> dict[str, Any]:
    """route_segment를 약 10m 구간으로 나누고 각 구간 중심점을 적재한다.

    segment별 점 개수는 `round(length / 10m)`, 최소 1개로 정한다. 각 점은
    동일 길이 구간의 중심에 배치하므로 junction에서 중복되는 endpoint 점을
    만들지 않으며, 전체 개수는 총연장/10m 추정치와 가깝게 유지된다.
    """
    with psycopg2.connect(**db_config) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    to_regclass('public.route_segment'),
                    to_regclass('public.segment_sample_point')
                """
            )
            if any(table_name is None for table_name in cursor.fetchone()):
                raise RuntimeError(
                    "route_segment 또는 segment_sample_point 테이블이 없습니다."
                )

            cursor.execute("SELECT COUNT(*) FROM route_segment")
            segment_count = int(cursor.fetchone()[0])
            if segment_count != EXPECTED_SEGMENT_COUNT:
                raise RuntimeError(
                    f"route_segment 수가 다릅니다: {segment_count:,} / "
                    f"예상 {EXPECTED_SEGMENT_COUNT:,}"
                )

            # D3/D4 분석값이 이미 채워진 경우 재생성으로 지우지 않도록 보호한다.
            cursor.execute(
                """
                SELECT
                    COUNT(*) AS existing_count,
                    COUNT(*) FILTER (
                        WHERE svf IS NOT NULL
                           OR albedo IS NOT NULL
                           OR emissivity IS NOT NULL
                           OR ground_flux_ratio IS NOT NULL
                           OR is_shaded_09 IS NOT NULL
                           OR is_shaded_12 IS NOT NULL
                           OR is_shaded_15 IS NOT NULL
                           OR is_shaded_18 IS NOT NULL
                           OR shade_src_09 IS NOT NULL
                           OR shade_src_12 IS NOT NULL
                           OR shade_src_15 IS NOT NULL
                           OR shade_src_18 IS NOT NULL
                    ) AS enriched_count
                FROM segment_sample_point
                """
            )
            existing_count, enriched_count = map(int, cursor.fetchone())
            if enriched_count:
                raise RuntimeError(
                    "기존 D3/D4 분석값을 보호하기 위해 분할점 재생성을 중단합니다: "
                    f"분석값 보유 sample={enriched_count:,}"
                )

            require_dev_replace_permission(
                target, existing_count, allow_replace, "segment_sample_point"
            )

            cursor.execute(
                "TRUNCATE TABLE segment_sample_point RESTART IDENTITY"
            )
            cursor.execute(
                """
                WITH segment_bins AS (
                    SELECT
                        segment_id,
                        geom,
                        GREATEST(
                            1,
                            ROUND(length_m::NUMERIC / %s)::INTEGER
                        ) AS sample_count
                    FROM route_segment
                )
                INSERT INTO segment_sample_point (segment_id, seq, geom)
                SELECT
                    segment.segment_id,
                    series.seq::SMALLINT,
                    ST_LineInterpolatePoint(
                        segment.geom,
                        (series.seq + 0.5)::DOUBLE PRECISION
                            / segment.sample_count
                    )
                FROM segment_bins AS segment
                CROSS JOIN LATERAL generate_series(
                    0,
                    segment.sample_count - 1
                ) AS series(seq)
                ORDER BY segment.segment_id, series.seq
                """,
                (SAMPLE_INTERVAL_M,),
            )
            inserted_count = int(cursor.rowcount)

            cursor.execute(
                """
                WITH per_segment AS (
                    SELECT
                        segment.segment_id,
                        segment.length_m::DOUBLE PRECISION AS length_m,
                        COUNT(sample.sample_id)::INTEGER AS sample_count,
                        MIN(sample.seq)::INTEGER AS minimum_seq,
                        MAX(sample.seq)::INTEGER AS maximum_seq
                    FROM route_segment AS segment
                    LEFT JOIN segment_sample_point AS sample
                      ON sample.segment_id = segment.segment_id
                    GROUP BY segment.segment_id, segment.length_m
                )
                SELECT
                    (SELECT COUNT(*) FROM segment_sample_point),
                    COUNT(*) FILTER (WHERE sample_count = 0),
                    MIN(sample_count),
                    MAX(sample_count),
                    COUNT(*) FILTER (
                        WHERE minimum_seq <> 0
                           OR maximum_seq <> sample_count - 1
                    ),
                    MIN(length_m / sample_count),
                    AVG(length_m / sample_count),
                    MAX(length_m / sample_count)
                FROM per_segment
                """
            )
            aggregate_result = cursor.fetchone()

            cursor.execute(
                """
                SELECT
                    COUNT(*) FILTER (WHERE ST_SRID(sample.geom) <> 5186),
                    COUNT(*) FILTER (
                        WHERE GeometryType(sample.geom) <> 'POINT'
                           OR ST_IsEmpty(sample.geom)
                           OR NOT ST_IsValid(sample.geom)
                    ),
                    COUNT(*) FILTER (
                        WHERE ST_Distance(sample.geom, segment.geom) > 0.000001
                    ),
                    COUNT(*) - COUNT(DISTINCT (sample.segment_id, sample.seq)),
                    COUNT(*) FILTER (
                        WHERE sample.svf IS NOT NULL
                           OR sample.albedo IS NOT NULL
                           OR sample.emissivity IS NOT NULL
                           OR sample.ground_flux_ratio IS NOT NULL
                           OR sample.is_shaded_09 IS NOT NULL
                           OR sample.is_shaded_12 IS NOT NULL
                           OR sample.is_shaded_15 IS NOT NULL
                           OR sample.is_shaded_18 IS NOT NULL
                           OR sample.shade_src_09 IS NOT NULL
                           OR sample.shade_src_12 IS NOT NULL
                           OR sample.shade_src_15 IS NOT NULL
                           OR sample.shade_src_18 IS NOT NULL
                    )
                FROM segment_sample_point AS sample
                JOIN route_segment AS segment
                  ON segment.segment_id = sample.segment_id
                """
            )
            quality_result = cursor.fetchone()

            statistics: dict[str, Any] = {
                "segment_count": segment_count,
                "previous_sample_count": existing_count,
                "inserted_count": inserted_count,
                "sample_count": int(aggregate_result[0]),
                "segment_without_sample_count": int(aggregate_result[1]),
                "minimum_samples_per_segment": int(aggregate_result[2]),
                "maximum_samples_per_segment": int(aggregate_result[3]),
                "bad_sequence_segment_count": int(aggregate_result[4]),
                "minimum_bin_length_m": float(aggregate_result[5]),
                "average_bin_length_m": float(aggregate_result[6]),
                "maximum_bin_length_m": float(aggregate_result[7]),
                "bad_srid_count": int(quality_result[0]),
                "bad_geometry_count": int(quality_result[1]),
                "off_segment_count": int(quality_result[2]),
                "duplicate_segment_seq_count": int(quality_result[3]),
                "unexpected_enriched_count": int(quality_result[4]),
            }

            if statistics["inserted_count"] != EXPECTED_SAMPLE_COUNT:
                raise RuntimeError(
                    f"INSERT 분할점 수가 다릅니다: "
                    f"{statistics['inserted_count']:,} / "
                    f"예상 {EXPECTED_SAMPLE_COUNT:,}"
                )
            if statistics["sample_count"] != EXPECTED_SAMPLE_COUNT:
                raise RuntimeError(
                    f"최종 분할점 수가 다릅니다: "
                    f"{statistics['sample_count']:,} / "
                    f"예상 {EXPECTED_SAMPLE_COUNT:,}"
                )

            zero_required_keys = (
                "segment_without_sample_count",
                "bad_sequence_segment_count",
                "bad_srid_count",
                "bad_geometry_count",
                "off_segment_count",
                "duplicate_segment_seq_count",
                "unexpected_enriched_count",
            )
            failures = {
                key: statistics[key]
                for key in zero_required_keys
                if statistics[key] != 0
            }
            if failures:
                raise RuntimeError(f"분할점 품질검사 실패: {failures}")

    return statistics


def write_report(statistics: dict[str, Any]) -> None:
    """D2 분할점 생성 규칙과 실제 품질 지표를 Markdown으로 기록한다."""
    report = f"""# D2 segment sample point QA

- 생성일: 2026-08-10
- 좌표계: EPSG:5186
- 목표 간격: 약 {SAMPLE_INTERVAL_M:.0f}m
- 규칙: `max(1, round(length_m / 10m))`개 등분 구간의 중심점
- junction endpoint 중복 생성: 없음

## 적재 결과

| 지표 | 결과 |
| --- | ---: |
| route_segment | {statistics['segment_count']:,} |
| 기존 sample | {statistics['previous_sample_count']:,} |
| 생성 sample | {statistics['sample_count']:,} |
| segment당 최소/최대 sample | {statistics['minimum_samples_per_segment']:,} / {statistics['maximum_samples_per_segment']:,} |
| 등분 구간 길이 최소/평균/최대 | {statistics['minimum_bin_length_m']:.3f} / {statistics['average_bin_length_m']:.3f} / {statistics['maximum_bin_length_m']:.3f} m |
| sample 없는 segment | {statistics['segment_without_sample_count']:,} |
| 잘못된 seq | {statistics['bad_sequence_segment_count']:,} |
| 잘못된 SRID | {statistics['bad_srid_count']:,} |
| 잘못된 geometry | {statistics['bad_geometry_count']:,} |
| 원래 segment를 벗어난 점 | {statistics['off_segment_count']:,} |
| 중복 `(segment_id, seq)` | {statistics['duplicate_segment_seq_count']:,} |
| D3/D4 분석값 선입력 | {statistics['unexpected_enriched_count']:,} |

## 판정

- 모든 route_segment에 최소 1개 이상의 공통 분석점이 있다.
- 모든 점은 원래 LineString 위에 있으며 `segment_id, seq`가 연속적이다.
- SVF·재질·그늘 필드는 D3/D4 입력 전이므로 모두 NULL이다.
- 이후 분석값이 존재하면 스크립트는 자동 재생성을 거부하여 결과를 보호한다.
"""
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(report, encoding="utf-8")


def main() -> None:
    """10m 공통 분할점을 재생성하고 DB 및 보고서 품질검사를 수행한다."""
    parser = argparse.ArgumentParser(description="D2 공통 segment sample point 생성")
    add_target_argument(parser)
    add_replace_argument(parser)
    args = parser.parse_args()
    db_config = build_db_config(args.target)
    print(f"[DB 연결] {describe_db_target(args.target, db_config)}")
    statistics = rebuild_sample_points(
        db_config, target=args.target, allow_replace=args.allow_replace
    )
    write_report(statistics)

    print("\n[segment_sample_point 생성 결과]")
    print(f"  route_segment: {statistics['segment_count']:,}")
    print(f"  기존 sample: {statistics['previous_sample_count']:,}")
    print(f"  생성 sample: {statistics['sample_count']:,}")
    print(
        "  segment당 sample 최소/최대: "
        f"{statistics['minimum_samples_per_segment']:,} / "
        f"{statistics['maximum_samples_per_segment']:,}"
    )
    print(
        "  등분 길이 최소/평균/최대: "
        f"{statistics['minimum_bin_length_m']:.3f} / "
        f"{statistics['average_bin_length_m']:.3f} / "
        f"{statistics['maximum_bin_length_m']:.3f} m"
    )
    print(f"  sample 없는 segment: {statistics['segment_without_sample_count']:,}")
    print(f"  잘못된 SRID: {statistics['bad_srid_count']:,}")
    print(f"  segment 밖 점: {statistics['off_segment_count']:,}")
    print(f"  중복 segment/seq: {statistics['duplicate_segment_seq_count']:,}")
    print(f"[QA 보고서] {REPORT_PATH}")
    print("\n[D2-7 segment_sample_point 적재 완료]")


if __name__ == "__main__":
    main()
