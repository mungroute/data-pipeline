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


EXPECTED_EDGE_COUNT = 7_766
EXPECTED_CLOSED_EDGE_COUNT = 9


def rebuild_route_topology(
    db_config: dict[str, str | int],
    target: str = "local",
    allow_replace: bool = False,
) -> dict[str, Any]:
    """
    staging geometry에서 route_vertex와 source/target을 다시 구성한다.

    vertex 재생성부터 품질검사까지 하나의 transaction에서 수행하므로
    중간에 실패하면 route_vertex와 staging 변경이 모두 rollback된다.
    """
    with psycopg2.connect(**db_config) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT pgr_version()")
            pgrouting_version = cursor.fetchone()[0]

            cursor.execute(
                """
                SELECT
                    to_regclass('public.route_vertex'),
                    to_regclass('public.route_segment_staging')
                """
            )
            route_vertex_table, staging_table = cursor.fetchone()
            if route_vertex_table is None or staging_table is None:
                raise RuntimeError(
                    "route_vertex 또는 route_segment_staging이 없습니다. "
                    "Flyway V4 적용 상태를 확인하세요."
                )

            cursor.execute("SELECT COUNT(*) FROM route_segment_staging")
            staging_count = int(cursor.fetchone()[0])
            if staging_count != EXPECTED_EDGE_COUNT:
                raise RuntimeError(
                    f"staging LINK 수가 다릅니다: {staging_count:,} / "
                    f"예상 {EXPECTED_EDGE_COUNT:,}"
                )

            cursor.execute("SELECT COUNT(*) FROM route_vertex")
            existing_vertex_count = int(cursor.fetchone()[0])
            require_dev_replace_permission(
                target, existing_vertex_count, allow_replace, "route_vertex"
            )

            # 이전 topology 결과가 남아 있어도 같은 staging으로 재생성한다.
            cursor.execute(
                "UPDATE route_segment_staging SET source = NULL, target = NULL"
            )
            cursor.execute("TRUNCATE TABLE route_vertex")

            # segment_id 순서를 고정해 같은 입력에서 vertex ID가 재현되게 한다.
            cursor.execute(
                """
                INSERT INTO route_vertex (
                    vertex_id,
                    in_edges,
                    out_edges,
                    x,
                    y,
                    geom
                )
                SELECT
                    id,
                    in_edges,
                    out_edges,
                    x,
                    y,
                    geom
                FROM pgr_extractVertices(
                    'SELECT segment_id AS id, geom
                     FROM route_segment_staging
                     ORDER BY segment_id'
                )
                """
            )

            # pgr_extractVertices가 만든 동일 endpoint geometry로 source를 채운다.
            cursor.execute(
                """
                UPDATE route_segment_staging AS segment
                SET source = vertex.vertex_id
                FROM route_vertex AS vertex
                WHERE ST_Equals(ST_StartPoint(segment.geom), vertex.geom)
                """
            )
            updated_source_count = cursor.rowcount

            # LineString 끝점과 같은 vertex를 찾아 target을 채운다.
            cursor.execute(
                """
                UPDATE route_segment_staging AS segment
                SET target = vertex.vertex_id
                FROM route_vertex AS vertex
                WHERE ST_Equals(ST_EndPoint(segment.geom), vertex.geom)
                """
            )
            updated_target_count = cursor.rowcount

            cursor.execute(
                """
                SELECT
                    (SELECT COUNT(*) FROM route_vertex) AS vertex_count,
                    (SELECT COUNT(*)
                     FROM route_segment_staging
                     WHERE source IS NULL) AS null_source_count,
                    (SELECT COUNT(*)
                     FROM route_segment_staging
                     WHERE target IS NULL) AS null_target_count,
                    (SELECT COUNT(*) - COUNT(DISTINCT ST_AsEWKB(geom))
                     FROM route_vertex) AS duplicate_vertex_geometry_count,
                    (SELECT COUNT(*)
                     FROM route_segment_staging AS segment
                     LEFT JOIN route_vertex AS vertex
                       ON vertex.vertex_id = segment.source
                     WHERE vertex.vertex_id IS NULL) AS missing_source_vertex_count,
                    (SELECT COUNT(*)
                     FROM route_segment_staging AS segment
                     LEFT JOIN route_vertex AS vertex
                       ON vertex.vertex_id = segment.target
                     WHERE vertex.vertex_id IS NULL) AS missing_target_vertex_count,
                    (SELECT COUNT(*)
                     FROM route_segment_staging AS segment
                     JOIN route_vertex AS vertex
                       ON vertex.vertex_id = segment.source
                     WHERE NOT ST_Equals(
                         ST_StartPoint(segment.geom), vertex.geom
                     )) AS source_geometry_mismatch_count,
                    (SELECT COUNT(*)
                     FROM route_segment_staging AS segment
                     JOIN route_vertex AS vertex
                       ON vertex.vertex_id = segment.target
                     WHERE NOT ST_Equals(
                         ST_EndPoint(segment.geom), vertex.geom
                     )) AS target_geometry_mismatch_count,
                    (SELECT COUNT(*)
                     FROM route_segment_staging
                     WHERE source = target) AS closed_edge_count,
                    (SELECT COUNT(*)
                     FROM route_vertex AS vertex
                     WHERE NOT EXISTS (
                         SELECT 1
                         FROM route_segment_staging AS segment
                         WHERE segment.source = vertex.vertex_id
                            OR segment.target = vertex.vertex_id
                     )) AS orphan_vertex_count
                """
            )
            result = cursor.fetchone()

            statistics: dict[str, Any] = {
                "pgrouting_version": pgrouting_version,
                "edge_count": staging_count,
                "vertex_count": int(result[0]),
                "updated_source_count": int(updated_source_count),
                "updated_target_count": int(updated_target_count),
                "null_source_count": int(result[1]),
                "null_target_count": int(result[2]),
                "duplicate_vertex_geometry_count": int(result[3]),
                "missing_source_vertex_count": int(result[4]),
                "missing_target_vertex_count": int(result[5]),
                "source_geometry_mismatch_count": int(result[6]),
                "target_geometry_mismatch_count": int(result[7]),
                "closed_edge_count": int(result[8]),
                "orphan_vertex_count": int(result[9]),
            }

            if statistics["updated_source_count"] != EXPECTED_EDGE_COUNT:
                raise RuntimeError("source가 모든 staging LINK에 연결되지 않았습니다.")
            if statistics["updated_target_count"] != EXPECTED_EDGE_COUNT:
                raise RuntimeError("target이 모든 staging LINK에 연결되지 않았습니다.")

            zero_required_keys = (
                "null_source_count",
                "null_target_count",
                "duplicate_vertex_geometry_count",
                "missing_source_vertex_count",
                "missing_target_vertex_count",
                "source_geometry_mismatch_count",
                "target_geometry_mismatch_count",
                "orphan_vertex_count",
            )
            failures = {
                key: statistics[key]
                for key in zero_required_keys
                if statistics[key] != 0
            }
            if failures:
                raise RuntimeError(f"topology 품질검사 실패: {failures}")

            if statistics["closed_edge_count"] != EXPECTED_CLOSED_EDGE_COUNT:
                raise RuntimeError(
                    "폐합 LINK 수가 원본 확인값과 다릅니다: "
                    f"{statistics['closed_edge_count']:,} / "
                    f"예상 {EXPECTED_CLOSED_EDGE_COUNT:,}"
                )

    return statistics


def main() -> None:
    """route_vertex 생성과 staging source/target 연결 결과를 출력한다."""
    parser = argparse.ArgumentParser(description="D2 pgRouting topology 생성")
    add_target_argument(parser)
    add_replace_argument(parser)
    args = parser.parse_args()
    db_config = build_db_config(args.target)

    print(f"[DB 연결] {describe_db_target(args.target, db_config)}")
    statistics = rebuild_route_topology(
        db_config, target=args.target, allow_replace=args.allow_replace
    )

    print()
    print("[route topology 생성 결과]")
    print(f"  pgRouting: {statistics['pgrouting_version']}")
    print(f"  staging LINK: {statistics['edge_count']:,}")
    print(f"  생성 vertex: {statistics['vertex_count']:,}")
    print(f"  source 연결: {statistics['updated_source_count']:,}")
    print(f"  target 연결: {statistics['updated_target_count']:,}")
    print(f"  source NULL: {statistics['null_source_count']:,}")
    print(f"  target NULL: {statistics['null_target_count']:,}")
    print(
        "  중복 vertex 좌표: "
        f"{statistics['duplicate_vertex_geometry_count']:,}"
    )
    print(
        "  endpoint-vertex 불일치: "
        f"{statistics['source_geometry_mismatch_count'] + statistics['target_geometry_mismatch_count']:,}"
    )
    print(f"  폐합 LINK: {statistics['closed_edge_count']:,}")
    print(f"  미참조 vertex: {statistics['orphan_vertex_count']:,}")
    print("\n[D2-4 vertex 생성 및 source/target 연결 완료]")


if __name__ == "__main__":
    main()
