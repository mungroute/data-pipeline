import argparse
from pathlib import Path

import pandas as pd
import psycopg2
from pyproj import Transformer
from shapely import wkt
from shapely.geometry import Point
from shapely.ops import transform as transform_geometry

from load_segments import build_db_config


PIPELINE_ROOT = Path(__file__).resolve().parents[1]
RAW_NETWORK_PATH = (
    PIPELINE_ROOT / "data" / "raw" / "network" / "seoul_walk_network.csv"
)
REPORT_DIR = PIPELINE_ROOT / "reports"

EDGE_SQL = """
SELECT
    segment_id AS id,
    source,
    target,
    length_m::FLOAT8 AS cost
FROM route_segment
"""

RAW_COLUMNS = [
    "노드링크 유형",
    "링크 WKT",
    "링크 ID",
    "링크 유형 코드",
    "시작노드 ID",
    "종료노드 ID",
    "링크 길이",
    "시군구코드",
    "시군구명",
    "읍면동코드",
    "읍면동명",
    "고가도로",
    "지하철네트워크",
    "교량",
    "터널",
    "육교",
    "횡단보도",
    "공원,녹지",
    "건물내",
]


def parse_args() -> argparse.Namespace:
    """검사할 component와 원본 endpoint 검색 반경을 명령행에서 받는다."""
    parser = argparse.ArgumentParser(
        description="분리 component 끝점 주변의 서울 전체 원본 LINK를 검색합니다."
    )
    parser.add_argument("--component", type=int, required=True)
    parser.add_argument("--radius-m", type=float, default=20.0)
    return parser.parse_args()


def load_component_terminals(
    component_id: int,
) -> tuple[pd.DataFrame, set[int]]:
    """현재 DB topology에서 component의 degree=1 끝점과 기존 LINK ID를 읽는다."""
    with psycopg2.connect(**build_db_config()) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                CREATE TEMP TABLE qa_boundary_components ON COMMIT DROP AS
                SELECT *
                FROM pgr_connectedComponents(%s)
                """,
                (EDGE_SQL,),
            )
            cursor.execute(
                """
                CREATE TEMP TABLE qa_boundary_degrees ON COMMIT DROP AS
                SELECT *
                FROM pgr_degree(%s)
                """,
                (EDGE_SQL,),
            )
            cursor.execute(
                """
                SELECT
                    vertex.vertex_id,
                    vertex.x::FLOAT8,
                    vertex.y::FLOAT8,
                    degree.degree
                FROM qa_boundary_components AS component
                JOIN qa_boundary_degrees AS degree
                  ON degree.node = component.node
                JOIN route_vertex AS vertex
                  ON vertex.vertex_id = component.node
                WHERE component.component = %s
                  AND degree.degree = 1
                ORDER BY vertex.vertex_id
                """,
                (component_id,),
            )
            terminal_rows = cursor.fetchall()

            cursor.execute(
                """
                SELECT segment.segment_id
                FROM route_segment AS segment
                WHERE segment.source IN (
                    SELECT node
                    FROM qa_boundary_components
                    WHERE component = %s
                )
                   OR segment.target IN (
                    SELECT node
                    FROM qa_boundary_components
                    WHERE component = %s
                )
                """,
                (component_id, component_id),
            )
            loaded_segment_ids = {int(row[0]) for row in cursor.fetchall()}

    if not terminal_rows:
        raise ValueError(
            f"component {component_id}에 degree=1 endpoint가 없습니다."
        )

    terminals = pd.DataFrame(
        terminal_rows,
        columns=["component_vertex_id", "component_x", "component_y", "degree"],
    )
    return terminals, loaded_segment_ids


def classify_distance(distance_m: float) -> str:
    """거리별로 자동 스냅 범위와 수동 검토 범위를 구분한다."""
    if distance_m <= 1.0:
        return "WITHIN_SNAP_TOLERANCE"
    if distance_m <= 5.0:
        return "NEAR_ENDPOINT"
    return "MANUAL_REVIEW"


def search_raw_link_endpoints(
    terminals: pd.DataFrame,
    loaded_segment_ids: set[int],
    radius_m: float,
) -> pd.DataFrame:
    """서울 전체 CSV에서 component 끝점 반경 안의 미적재 원본 LINK를 찾는다.

    원본 LINK의 endpoint만 비교하면 긴 LINK 중간에 닿는 경우를 놓칠 수 있으므로
    선 전체의 최근접 거리도 계산한다. 먼저 경위도 bounding box로 후보를 줄인 뒤
    EPSG:5186 geometry를 생성한다.
    """
    transformer = Transformer.from_crs(
        "EPSG:4326", "EPSG:5186", always_xy=True
    )
    inverse_transformer = Transformer.from_crs(
        "EPSG:5186", "EPSG:4326", always_xy=True
    )
    terminal_coordinates = terminals[["component_x", "component_y"]].to_numpy()
    terminal_points = [Point(x, y) for x, y in terminal_coordinates]

    terminal_longitudes, terminal_latitudes = inverse_transformer.transform(
        terminal_coordinates[:, 0], terminal_coordinates[:, 1]
    )
    # 서울 위도에서 1도는 80km보다 크므로 넉넉한 사전 필터 여유를 둔다.
    bbox_margin_degrees = radius_m / 80_000.0
    search_bounds = (
        min(terminal_longitudes) - bbox_margin_degrees,
        min(terminal_latitudes) - bbox_margin_degrees,
        max(terminal_longitudes) + bbox_margin_degrees,
        max(terminal_latitudes) + bbox_margin_degrees,
    )
    result_rows: list[dict] = []

    for chunk in pd.read_csv(
        RAW_NETWORK_PATH,
        encoding="cp949",
        dtype=str,
        usecols=RAW_COLUMNS,
        chunksize=100_000,
        keep_default_na=False,
    ):
        row_types = chunk["노드링크 유형"].str.strip().str.upper()
        link_types = chunk["링크 유형 코드"].str.strip()
        link_ids = pd.to_numeric(chunk["링크 ID"], errors="coerce")

        # 중구에서 이미 적재된 LINK와 보행 불가 유형은 후보에서 제외한다.
        candidates = chunk.loc[
            (row_types == "LINK")
            & link_types.str.startswith("1")
            & link_ids.notna()
            & ~link_ids.isin(loaded_segment_ids)
        ]

        for _, row in candidates.iterrows():
            try:
                geometry = wkt.loads(row["링크 WKT"].strip())
                if geometry.is_empty or geometry.geom_type != "LineString":
                    continue
            except (ValueError, TypeError):
                continue

            min_lon, min_lat, max_lon, max_lat = geometry.bounds
            search_min_lon, search_min_lat, search_max_lon, search_max_lat = (
                search_bounds
            )
            if (
                max_lon < search_min_lon
                or min_lon > search_max_lon
                or max_lat < search_min_lat
                or min_lat > search_max_lat
            ):
                continue

            projected_geometry = transform_geometry(
                transformer.transform, geometry
            )
            start_point = Point(projected_geometry.coords[0])
            end_point = Point(projected_geometry.coords[-1])

            for terminal_index, terminal_point in enumerate(terminal_points):
                distance_m = terminal_point.distance(projected_geometry)
                if distance_m > radius_m:
                    continue

                projected_distance = projected_geometry.project(terminal_point)
                nearest_point = projected_geometry.interpolate(projected_distance)
                if nearest_point.distance(start_point) <= 0.001:
                    candidate_position = "start"
                elif nearest_point.distance(end_point) <= 0.001:
                    candidate_position = "end"
                else:
                    candidate_position = "line_interior"

                longitude, latitude = inverse_transformer.transform(
                    nearest_point.x, nearest_point.y
                )
                terminal = terminals.iloc[terminal_index]
                result_rows.append(
                    {
                        "component_vertex_id": int(
                            terminal["component_vertex_id"]
                        ),
                        "component_x_5186": float(terminal["component_x"]),
                        "component_y_5186": float(terminal["component_y"]),
                        "candidate_link_id": int(row["링크 ID"]),
                        "candidate_endpoint": candidate_position,
                        "distance_m": round(float(distance_m), 3),
                        "distance_class": classify_distance(float(distance_m)),
                        "district_code": row["시군구코드"].strip(),
                        "district_name": row["시군구명"].strip(),
                        "neighborhood_code": row["읍면동코드"].strip(),
                        "neighborhood_name": row["읍면동명"].strip(),
                        "link_type_code": row["링크 유형 코드"].strip(),
                        "original_start_node_id": row["시작노드 ID"].strip(),
                        "original_end_node_id": row["종료노드 ID"].strip(),
                        "original_length_m": row["링크 길이"].strip(),
                        "candidate_x_5186": round(float(nearest_point.x), 3),
                        "candidate_y_5186": round(float(nearest_point.y), 3),
                        "candidate_lon": float(longitude),
                        "candidate_lat": float(latitude),
                        "raw_is_elevated": row["고가도로"].strip(),
                        "raw_is_subway": row["지하철네트워크"].strip(),
                        "raw_is_bridge": row["교량"].strip(),
                        "raw_is_tunnel": row["터널"].strip(),
                        "raw_is_overpass": row["육교"].strip(),
                        "raw_is_crosswalk": row["횡단보도"].strip(),
                        "raw_is_park_green": row["공원,녹지"].strip(),
                        "raw_is_indoor": row["건물내"].strip(),
                    }
                )

    results = pd.DataFrame(result_rows)
    if results.empty:
        return results

    return results.sort_values(
        ["distance_m", "component_vertex_id", "candidate_link_id"]
    ).drop_duplicates(
        ["component_vertex_id", "candidate_link_id", "candidate_endpoint"]
    )


def write_outputs(
    component_id: int,
    radius_m: float,
    terminals: pd.DataFrame,
    candidates: pd.DataFrame,
) -> tuple[Path, Path]:
    """후보 원본 행과 재현 조건을 CSV 및 Markdown 보고서로 저장한다."""
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    csv_path = REPORT_DIR / f"d2_c{component_id}_boundary_candidates.csv"
    report_path = REPORT_DIR / f"d2_c{component_id}_boundary_check.md"
    candidates.to_csv(csv_path, index=False, encoding="utf-8-sig")

    within_one_metre = (
        0
        if candidates.empty
        else int((candidates["distance_m"] <= 1.0).sum())
    )
    district_counts = (
        "없음"
        if candidates.empty
        else ", ".join(
            f"{name or '(공란)'} {count}건"
            for name, count in candidates["district_name"].value_counts().items()
        )
    )
    if candidates.empty:
        nearest_table = "후보 없음"
        conclusion = (
            "검색 반경 안에 이어지는 원본 LINK가 없다. C{0}을 기존 상태로 "
            "유지하고 자동 연결하지 않는다."
        ).format(component_id)
    else:
        nearest_candidates = (
            candidates.sort_values("distance_m")
            .groupby("component_vertex_id", as_index=False)
            .first()
            .sort_values("component_vertex_id")
        )
        nearest_lines = [
            "| C{0} vertex | 후보 LINK | 접점 | 거리 | 시군구 | 행정동 | 판정 |".format(
                component_id
            ),
            "| ---: | ---: | --- | ---: | --- | --- | --- |",
        ]
        for row in nearest_candidates.itertuples(index=False):
            nearest_lines.append(
                "| {0} | {1} | `{2}` | {3:.3f} m | {4} | {5} | `{6}` |".format(
                    row.component_vertex_id,
                    row.candidate_link_id,
                    row.candidate_endpoint,
                    row.distance_m,
                    row.district_name,
                    row.neighborhood_name,
                    row.distance_class,
                )
            )
        nearest_table = "\n".join(nearest_lines)

        minimum_distance = float(candidates["distance_m"].min())
        if minimum_distance <= 1.0:
            conclusion = (
                "1m 이내 원본 LINK가 있으므로 원본 geometry와 보행 가능 여부를 "
                "QGIS에서 확인한 뒤 복구 대상으로 검토한다."
            )
        else:
            conclusion = (
                f"가장 가까운 미적재 원본 LINK도 {minimum_distance:.3f}m 떨어져 "
                "있다. 행정경계 필터로 같은 endpoint가 빠진 단순 누락은 아니며, "
                "근거 없이 직선 connector를 만들거나 자동 스냅하지 않는다. "
                f"C{component_id}은 분리 component로 유지한다."
            )
    report = f"""# D2 C{component_id} 행정경계 인접 LINK 검사

- 원본: `data/raw/network/seoul_walk_network.csv`
- 검색 기준: 현재 topology의 C{component_id} degree=1 endpoint
- 검색 반경: {radius_m:.1f} m
- component 끝점: {len(terminals):,}개
- 발견 후보 LINK 접점: {len(candidates):,}개
- 1m 스냅 허용오차 이내: {within_one_metre:,}개
- 후보 시군구: {district_counts}

## 판정 원칙

- `WITHIN_SNAP_TOLERANCE`: 좌표상 1m 이내지만 원본 속성과 실제 보행 가능 여부 확인 후 복구한다.
- `NEAR_ENDPOINT`: 1m 초과 5m 이하이므로 자동 스냅하지 않고 영상·지도 검토가 필요하다.
- `MANUAL_REVIEW`: 5m 초과이므로 연결 근거로 사용하지 않고 주변 데이터 탐색 참고값으로만 쓴다.
- 이 검사는 후보를 찾기만 하며 `route_segment`나 topology를 변경하지 않는다.

## 끝점별 가장 가까운 원본 LINK

{nearest_table}

## 결론

{conclusion}

## 결과 파일

- `reports/{csv_path.name}`
"""
    report_path.write_text(report, encoding="utf-8")
    return csv_path, report_path


def main() -> None:
    """DB 끝점 조회, 서울 전체 원본 검색, QA 보고서 저장을 순서대로 실행한다."""
    args = parse_args()
    if args.radius_m <= 0:
        raise ValueError("검색 반경은 0보다 커야 합니다.")

    terminals, loaded_segment_ids = load_component_terminals(args.component)
    candidates = search_raw_link_endpoints(
        terminals, loaded_segment_ids, args.radius_m
    )
    csv_path, report_path = write_outputs(
        args.component, args.radius_m, terminals, candidates
    )

    print(f"[component] C{args.component}")
    print(f"[degree=1 endpoint] {len(terminals):,}")
    print(f"[검색 반경] {args.radius_m:.1f} m")
    print(f"[후보 endpoint] {len(candidates):,}")
    print(f"[CSV] {csv_path}")
    print(f"[보고서] {report_path}")


if __name__ == "__main__":
    main()
