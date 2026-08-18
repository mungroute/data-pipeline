import argparse
from typing import Any

import psycopg2

from db_config import add_target_argument, build_db_config, describe_db_target


EDGE_SQL = """
SELECT
    segment_id AS id,
    source,
    target,
    length_m::FLOAT8 AS cost
FROM route_segment
"""
EXPECTED_EDGE_COUNT = 7_766
EXPECTED_VERTEX_COUNT = 6_028
LARGEST_COMPONENT_SANITY_RATIO = 0.99


def parse_version(version: str) -> tuple[int, int, int]:
    """pgRouting 버전 문자열의 숫자 부분을 비교 가능한 튜플로 변환한다."""
    numeric_part = version.split("-")[0]
    parts = numeric_part.split(".")
    padded_parts = (parts + ["0", "0", "0"])[:3]
    return tuple(int(part) for part in padded_parts)


def validate_routing_graph(
    db_config: dict[str, str | int],
) -> dict[str, Any]:
    """
    연결요소, vertex degree와 실제 최단경로를 읽기 전용으로 검사한다.

    pgRouting 결과는 세션 임시 테이블에만 저장하며 route_segment,
    route_vertex 등 프로젝트 영구 테이블은 변경하지 않는다.
    """
    with psycopg2.connect(**db_config) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT pgr_version()")
            pgrouting_version = cursor.fetchone()[0]
            supports_pgr_degree = parse_version(pgrouting_version) >= (3, 8, 0)

            cursor.execute(
                """
                SELECT
                    (SELECT COUNT(*) FROM route_segment),
                    (SELECT COUNT(*) FROM route_vertex)
                """
            )
            edge_count, vertex_count = map(int, cursor.fetchone())
            if edge_count != EXPECTED_EDGE_COUNT:
                raise RuntimeError(
                    f"route_segment 수가 다릅니다: {edge_count:,} / "
                    f"예상 {EXPECTED_EDGE_COUNT:,}"
                )
            if vertex_count != EXPECTED_VERTEX_COUNT:
                raise RuntimeError(
                    f"route_vertex 수가 다릅니다: {vertex_count:,} / "
                    f"예상 {EXPECTED_VERTEX_COUNT:,}"
                )

            # 같은 결과를 여러 품질 지표에 사용하도록 세션 임시 테이블로 만든다.
            cursor.execute(
                """
                CREATE TEMP TABLE qa_components ON COMMIT DROP AS
                SELECT *
                FROM pgr_connectedComponents(%s)
                """,
                (EDGE_SQL,),
            )
            if supports_pgr_degree:
                cursor.execute(
                    """
                    CREATE TEMP TABLE qa_degrees ON COMMIT DROP AS
                    SELECT node, degree
                    FROM pgr_degree(%s)
                    """,
                    (EDGE_SQL,),
                )
                degree_method = "pgr_degree"
            else:
                # Supabase pgRouting 3.4에는 pgr_degree(text)가 없으므로
                # 같은 무방향 degree를 source/target 출현 횟수로 계산한다.
                # self-loop는 양 끝점으로 두 번 출현해 degree 2로 집계된다.
                cursor.execute(
                    """
                    CREATE TEMP TABLE qa_degrees ON COMMIT DROP AS
                    WITH endpoint_counts AS (
                        SELECT node, COUNT(*)::BIGINT AS degree
                        FROM (
                            SELECT source AS node FROM route_segment
                            UNION ALL
                            SELECT target AS node FROM route_segment
                        ) AS endpoints
                        GROUP BY node
                    )
                    SELECT vertex.vertex_id AS node,
                           COALESCE(endpoint.degree, 0)::BIGINT AS degree
                    FROM route_vertex AS vertex
                    LEFT JOIN endpoint_counts AS endpoint
                      ON endpoint.node = vertex.vertex_id
                    """
                )
                degree_method = "source_target_fallback"

            cursor.execute(
                """
                SELECT
                    COUNT(DISTINCT component) AS component_count,
                    COUNT(*) AS membership_count,
                    COUNT(DISTINCT node) AS distinct_node_count
                FROM qa_components
                """
            )
            component_count, membership_count, component_node_count = map(
                int, cursor.fetchone()
            )

            cursor.execute(
                """
                SELECT component, COUNT(*) AS vertex_count
                FROM qa_components
                GROUP BY component
                ORDER BY vertex_count DESC, component
                LIMIT 1
                """
            )
            largest_component_id, largest_component_vertex_count = (
                cursor.fetchone()
            )
            largest_component_id = int(largest_component_id)
            largest_component_vertex_count = int(
                largest_component_vertex_count
            )
            largest_component_ratio = (
                largest_component_vertex_count / vertex_count
            )

            cursor.execute(
                """
                SELECT
                    COUNT(*) AS degree_vertex_count,
                    COUNT(*) FILTER (WHERE degree = 0) AS isolated_count,
                    COUNT(*) FILTER (WHERE degree = 1) AS dead_end_count,
                    MIN(degree) AS minimum_degree,
                    MAX(degree) AS maximum_degree,
                    AVG(degree)::FLOAT8 AS average_degree
                FROM qa_degrees
                """
            )
            degree_result = cursor.fetchone()

            # 최대 component의 서쪽 끝과 동쪽 끝 vertex를 경로 검증점으로 사용한다.
            cursor.execute(
                """
                SELECT component.node
                FROM qa_components AS component
                JOIN route_vertex AS vertex
                  ON vertex.vertex_id = component.node
                WHERE component.component = %s
                ORDER BY vertex.x, vertex.y, vertex.vertex_id
                LIMIT 1
                """,
                (largest_component_id,),
            )
            start_vertex = int(cursor.fetchone()[0])

            cursor.execute(
                """
                SELECT component.node
                FROM qa_components AS component
                JOIN route_vertex AS vertex
                  ON vertex.vertex_id = component.node
                WHERE component.component = %s
                ORDER BY vertex.x DESC, vertex.y DESC, vertex.vertex_id DESC
                LIMIT 1
                """,
                (largest_component_id,),
            )
            end_vertex = int(cursor.fetchone()[0])

            if start_vertex == end_vertex:
                raise RuntimeError("경로 검증 시작점과 종료점이 같습니다.")

            cursor.execute(
                """
                CREATE TEMP TABLE qa_path ON COMMIT DROP AS
                SELECT *
                FROM pgr_dijkstra(
                    %s,
                    %s::BIGINT,
                    %s::BIGINT,
                    directed := false
                )
                """,
                (EDGE_SQL, start_vertex, end_vertex),
            )

            cursor.execute(
                """
                SELECT
                    COUNT(*) AS path_row_count,
                    COUNT(*) FILTER (WHERE edge <> -1) AS path_edge_count,
                    COALESCE(SUM(cost) FILTER (WHERE edge <> -1), 0)::FLOAT8
                        AS path_cost_m,
                    COALESCE(MAX(agg_cost), 0)::FLOAT8 AS final_aggregate_cost_m
                FROM qa_path
                """
            )
            path_result = cursor.fetchone()

            # 각 경로 edge가 현재 node와 다음 node를 실제 양 끝점으로 갖는지 본다.
            cursor.execute(
                """
                WITH path_transitions AS (
                    SELECT
                        path_seq,
                        node,
                        edge,
                        LEAD(node) OVER (ORDER BY path_seq) AS next_node
                    FROM qa_path
                )
                SELECT COUNT(*)
                FROM path_transitions AS path
                JOIN route_segment AS segment
                  ON segment.segment_id = path.edge
                WHERE path.edge <> -1
                  AND NOT (
                      (segment.source = path.node
                       AND segment.target = path.next_node)
                      OR
                      (segment.target = path.node
                       AND segment.source = path.next_node)
                  )
                """
            )
            disconnected_path_transition_count = int(cursor.fetchone()[0])

            statistics: dict[str, Any] = {
                "pgrouting_version": pgrouting_version,
                "degree_method": degree_method,
                "edge_count": edge_count,
                "vertex_count": vertex_count,
                "component_count": component_count,
                "membership_count": membership_count,
                "component_node_count": component_node_count,
                "largest_component_id": largest_component_id,
                "largest_component_vertex_count": (
                    largest_component_vertex_count
                ),
                "largest_component_ratio": largest_component_ratio,
                "degree_vertex_count": int(degree_result[0]),
                "isolated_vertex_count": int(degree_result[1]),
                "dead_end_count": int(degree_result[2]),
                "minimum_degree": int(degree_result[3]),
                "maximum_degree": int(degree_result[4]),
                "average_degree": float(degree_result[5]),
                "start_vertex": start_vertex,
                "end_vertex": end_vertex,
                "path_row_count": int(path_result[0]),
                "path_edge_count": int(path_result[1]),
                "path_cost_m": float(path_result[2]),
                "final_aggregate_cost_m": float(path_result[3]),
                "disconnected_path_transition_count": (
                    disconnected_path_transition_count
                ),
            }

            if membership_count != vertex_count:
                raise RuntimeError("component 멤버 수가 전체 vertex 수와 다릅니다.")
            if component_node_count != vertex_count:
                raise RuntimeError("component에 중복 또는 누락 vertex가 있습니다.")
            if statistics["degree_vertex_count"] != vertex_count:
                raise RuntimeError("degree 결과 vertex 수가 전체 vertex 수와 다릅니다.")
            if statistics["isolated_vertex_count"] != 0:
                raise RuntimeError("degree=0인 고립 vertex가 있습니다.")
            if statistics["path_edge_count"] <= 0:
                raise RuntimeError("pgr_dijkstra가 실제 edge 경로를 반환하지 않았습니다.")
            if disconnected_path_transition_count != 0:
                raise RuntimeError(
                    "pgr_dijkstra 경로에 공간적으로 연결되지 않은 edge가 있습니다."
                )
            if abs(
                statistics["path_cost_m"]
                - statistics["final_aggregate_cost_m"]
            ) > 1e-6:
                raise RuntimeError("경로 cost 합계와 최종 누적 cost가 다릅니다.")

    statistics["largest_component_sanity_passed"] = (
        statistics["largest_component_ratio"]
        >= LARGEST_COMPONENT_SANITY_RATIO
    )
    return statistics


def main() -> None:
    """D2 routing graph의 품질 지표와 실제 경로 검증 결과를 출력한다."""
    parser = argparse.ArgumentParser(description="pgRouting graph 읽기 전용 검증")
    add_target_argument(parser)
    args = parser.parse_args()
    db_config = build_db_config(args.target)

    print(f"[DB 연결] {describe_db_target(args.target, db_config)}")
    statistics = validate_routing_graph(db_config)

    print()
    print("[routing graph 품질검사]")
    print(f"  pgRouting: {statistics['pgrouting_version']}")
    print(f"  degree 계산: {statistics['degree_method']}")
    print(f"  LINK: {statistics['edge_count']:,}")
    print(f"  vertex: {statistics['vertex_count']:,}")
    print(f"  connected component: {statistics['component_count']:,}")
    print(
        "  최대 component vertex: "
        f"{statistics['largest_component_vertex_count']:,} "
        f"({statistics['largest_component_ratio'] * 100:.3f}%)"
    )
    print(f"  dead-end(degree=1): {statistics['dead_end_count']:,}")
    print(f"  고립 vertex(degree=0): {statistics['isolated_vertex_count']:,}")
    print(
        "  degree 최소/평균/최대: "
        f"{statistics['minimum_degree']} / "
        f"{statistics['average_degree']:.3f} / "
        f"{statistics['maximum_degree']}"
    )

    print()
    print("[pgr_dijkstra 실제 경로]")
    print(
        f"  시작/종료 vertex: {statistics['start_vertex']} → "
        f"{statistics['end_vertex']}"
    )
    print(f"  경로 edge: {statistics['path_edge_count']:,}")
    print(f"  경로 비용(길이): {statistics['path_cost_m']:,.2f} m")
    print(
        "  연결되지 않은 edge 전이: "
        f"{statistics['disconnected_path_transition_count']:,}"
    )

    if statistics["largest_component_sanity_passed"]:
        print("\n[최대 component 99% sanity check 통과]")
    else:
        print("\n[주의] 최대 component 비율이 99% 미만입니다.")

    print("[D2-6 routing graph 품질검사 완료]")


if __name__ == "__main__":
    main()
