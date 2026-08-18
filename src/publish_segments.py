import argparse
from typing import Any

import psycopg2

from db_config import (
    add_replace_argument,
    add_target_argument,
    build_db_config,
    describe_db_target,
    require_dev_replace_permission,
)


EXPECTED_SEGMENT_COUNT = 7_766
EXPECTED_CLOSED_SEGMENT_COUNT = 9


def publish_route_segments(
    db_config: dict[str, str | int],
    target: str = "local",
    allow_replace: bool = False,
) -> dict[str, Any]:
    """
    검증된 staging LINK를 최종 route_segment 베이스 데이터로 발행한다.

    기존 샘플 포인트나 열환경 분석값이 있으면 후속 작업 손실을 막기 위해
    중단한다. 교체와 검증은 한 transaction에서 수행되어 실패 시 rollback된다.
    """
    with psycopg2.connect(**db_config) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    to_regclass('public.route_segment'),
                    to_regclass('public.route_segment_staging'),
                    to_regclass('public.route_vertex'),
                    to_regclass('public.segment_sample_point')
                """
            )
            table_names = cursor.fetchone()
            if any(table_name is None for table_name in table_names):
                raise RuntimeError(
                    "route topology 발행에 필요한 테이블이 없습니다. "
                    "Flyway V3/V4 적용 상태를 확인하세요."
                )

            cursor.execute("SELECT COUNT(*) FROM route_segment_staging")
            staging_count = int(cursor.fetchone()[0])
            if staging_count != EXPECTED_SEGMENT_COUNT:
                raise RuntimeError(
                    f"staging 행 수가 다릅니다: {staging_count:,} / "
                    f"예상 {EXPECTED_SEGMENT_COUNT:,}"
                )

            cursor.execute(
                """
                SELECT COUNT(*)
                FROM route_segment_staging
                WHERE source IS NULL OR target IS NULL
                """
            )
            staging_unlinked_count = int(cursor.fetchone()[0])
            if staging_unlinked_count:
                raise RuntimeError(
                    f"source/target이 연결되지 않은 staging LINK가 "
                    f"{staging_unlinked_count:,}개 있습니다."
                )

            # 후속 D2~D5 결과가 있으면 자동 교체로 삭제하지 않는다.
            cursor.execute("SELECT COUNT(*) FROM segment_sample_point")
            existing_sample_count = int(cursor.fetchone()[0])

            cursor.execute(
                """
                SELECT COUNT(*)
                FROM route_segment
                WHERE surface_type IS NOT NULL
                   OR svf IS NOT NULL
                   OR albedo IS NOT NULL
                   OR emissivity IS NOT NULL
                   OR ground_flux_ratio IS NOT NULL
                   OR park_proximity_m IS NOT NULL
                   OR tree_shade_ratio_09 IS NOT NULL
                   OR tree_shade_ratio_12 IS NOT NULL
                   OR tree_shade_ratio_15 IS NOT NULL
                   OR tree_shade_ratio_18 IS NOT NULL
                   OR bldg_shade_ratio_09 IS NOT NULL
                   OR bldg_shade_ratio_12 IS NOT NULL
                   OR bldg_shade_ratio_15 IS NOT NULL
                   OR bldg_shade_ratio_18 IS NOT NULL
                   OR shade_ratio_09 IS NOT NULL
                   OR shade_ratio_12 IS NOT NULL
                   OR shade_ratio_15 IS NOT NULL
                   OR shade_ratio_18 IS NOT NULL
                """
            )
            enriched_segment_count = int(cursor.fetchone()[0])

            if existing_sample_count or enriched_segment_count:
                raise RuntimeError(
                    "기존 후속 분석 데이터를 보호하기 위해 발행을 중단합니다: "
                    f"sample_point={existing_sample_count:,}, "
                    f"분석값 보유 segment={enriched_segment_count:,}"
                )

            cursor.execute("SELECT COUNT(*) FROM route_segment")
            previous_segment_count = int(cursor.fetchone()[0])
            require_dev_replace_permission(
                target, previous_segment_count, allow_replace, "route_segment"
            )

            # sample point가 없음을 확인했으므로 기존 D2 베이스 데이터만 교체한다.
            cursor.execute("DELETE FROM route_segment")
            cursor.execute(
                """
                INSERT INTO route_segment (
                    segment_id,
                    source,
                    target,
                    geom,
                    length_m
                )
                SELECT
                    segment_id,
                    source,
                    target,
                    geom,
                    length_m
                FROM route_segment_staging
                ORDER BY segment_id
                """
            )
            inserted_segment_count = cursor.rowcount

            cursor.execute(
                """
                SELECT
                    (SELECT COUNT(*) FROM route_segment) AS segment_count,
                    (SELECT COUNT(*)
                     FROM route_segment
                     WHERE source = target) AS closed_segment_count,
                    (SELECT COUNT(*)
                     FROM route_segment AS segment
                     LEFT JOIN route_vertex AS vertex
                       ON vertex.vertex_id = segment.source
                     WHERE vertex.vertex_id IS NULL) AS missing_source_count,
                    (SELECT COUNT(*)
                     FROM route_segment AS segment
                     LEFT JOIN route_vertex AS vertex
                       ON vertex.vertex_id = segment.target
                     WHERE vertex.vertex_id IS NULL) AS missing_target_count,
                    (SELECT COUNT(*)
                     FROM route_segment
                     WHERE ST_SRID(geom) <> 5186
                        OR GeometryType(geom) <> 'LINESTRING'
                        OR ST_IsEmpty(geom)
                        OR NOT ST_IsValid(geom)
                        OR length_m <= 0) AS unusable_segment_count,
                    (SELECT COUNT(*)
                     FROM route_segment AS final
                     FULL JOIN route_segment_staging AS staging
                       USING (segment_id)
                     WHERE final.segment_id IS NULL
                        OR staging.segment_id IS NULL) AS missing_pair_count,
                    (SELECT COUNT(*)
                     FROM route_segment AS final
                     JOIN route_segment_staging AS staging
                       USING (segment_id)
                     WHERE final.source IS DISTINCT FROM staging.source
                        OR final.target IS DISTINCT FROM staging.target
                        OR final.length_m IS DISTINCT FROM staging.length_m
                        OR NOT ST_Equals(final.geom, staging.geom)
                    ) AS staging_mismatch_count,
                    COALESCE(
                        (SELECT MAX(ABS(length_m - ST_Length(geom)))
                         FROM route_segment),
                        0
                    ) AS maximum_length_difference_m
                """
            )
            result = cursor.fetchone()

            statistics: dict[str, Any] = {
                "previous_segment_count": previous_segment_count,
                "inserted_segment_count": int(inserted_segment_count),
                "segment_count": int(result[0]),
                "closed_segment_count": int(result[1]),
                "missing_source_count": int(result[2]),
                "missing_target_count": int(result[3]),
                "unusable_segment_count": int(result[4]),
                "missing_pair_count": int(result[5]),
                "staging_mismatch_count": int(result[6]),
                "maximum_length_difference_m": float(result[7]),
            }

            if statistics["inserted_segment_count"] != EXPECTED_SEGMENT_COUNT:
                raise RuntimeError("최종 INSERT 행 수가 예상과 다릅니다.")
            if statistics["segment_count"] != EXPECTED_SEGMENT_COUNT:
                raise RuntimeError("최종 route_segment 행 수가 예상과 다릅니다.")

            zero_required_keys = (
                "missing_source_count",
                "missing_target_count",
                "unusable_segment_count",
                "missing_pair_count",
                "staging_mismatch_count",
            )
            failures = {
                key: statistics[key]
                for key in zero_required_keys
                if statistics[key] != 0
            }
            if failures:
                raise RuntimeError(f"최종 route_segment 검증 실패: {failures}")

            if (
                statistics["closed_segment_count"]
                != EXPECTED_CLOSED_SEGMENT_COUNT
            ):
                raise RuntimeError(
                    "최종 폐합 LINK 수가 확인값과 다릅니다: "
                    f"{statistics['closed_segment_count']:,} / "
                    f"예상 {EXPECTED_CLOSED_SEGMENT_COUNT:,}"
                )
            if statistics["maximum_length_difference_m"] > 0.01:
                raise RuntimeError(
                    "최종 length_m과 geometry 길이 차이가 0.01m를 초과했습니다."
                )

    return statistics


def main() -> None:
    """staging을 route_segment에 발행하고 품질검사 결과를 출력한다."""
    parser = argparse.ArgumentParser(description="검증된 도보망을 route_segment에 게시")
    add_target_argument(parser)
    add_replace_argument(parser)
    args = parser.parse_args()
    db_config = build_db_config(args.target)

    print(f"[DB 연결] {describe_db_target(args.target, db_config)}")
    statistics = publish_route_segments(
        db_config, target=args.target, allow_replace=args.allow_replace
    )

    print()
    print("[route_segment 발행 결과]")
    print(f"  기존 행 수: {statistics['previous_segment_count']:,}")
    print(f"  INSERT 행 수: {statistics['inserted_segment_count']:,}")
    print(f"  최종 행 수: {statistics['segment_count']:,}")
    print(f"  폐합 LINK: {statistics['closed_segment_count']:,}")
    print(f"  누락 source 참조: {statistics['missing_source_count']:,}")
    print(f"  누락 target 참조: {statistics['missing_target_count']:,}")
    print(f"  사용 불가 LINK: {statistics['unusable_segment_count']:,}")
    print(f"  staging 불일치: {statistics['staging_mismatch_count']:,}")
    print(
        "  최대 길이 오차: "
        f"{statistics['maximum_length_difference_m']:.6f} m"
    )
    print("\n[D2-5 route_segment 발행 완료]")


if __name__ == "__main__":
    main()
