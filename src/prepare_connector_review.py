import argparse
import csv
from pathlib import Path

import psycopg2
from osgeo import ogr, osr

from load_segments import build_db_config


PIPELINE_ROOT = Path(__file__).resolve().parents[1]
CANDIDATE_PATH = PIPELINE_ROOT / "config" / "manual_connector_candidates.csv"
OUTPUT_DIR = PIPELINE_ROOT / "data" / "interim" / "network"
CONTEXT_RADIUS_M = 100.0
VERTEX_MATCH_TOLERANCE_M = 0.01


def parse_args() -> argparse.Namespace:
    """검토할 connector ID를 명령행에서 받는다."""
    parser = argparse.ArgumentParser(
        description="QGIS 수동 connector 검토용 GeoPackage를 생성합니다."
    )
    parser.add_argument("--candidate", required=True)
    return parser.parse_args()


def read_candidate(candidate_id: str) -> dict[str, str]:
    """수동 connector 후보 CSV에서 지정한 한 건을 읽는다."""
    with CANDIDATE_PATH.open("r", encoding="utf-8-sig", newline="") as file:
        candidates = list(csv.DictReader(file))

    matches = [
        row for row in candidates if row["candidate_id"].strip() == candidate_id
    ]
    if len(matches) != 1:
        raise ValueError(
            f"connector 후보는 정확히 1건이어야 합니다: {candidate_id} "
            f"({len(matches)}건)"
        )
    return matches[0]


def resolve_current_vertices(
    candidate: dict[str, str],
) -> tuple[list[dict[str, int | float | str]], list[tuple]]:
    """좌표를 기준으로 재생성된 현재 vertex ID와 주변 route_segment를 찾는다.

    topology를 다시 만들면 vertex ID가 달라질 수 있으므로 후보 CSV의 과거 ID를
    source/target으로 재사용하지 않는다. EPSG:5186 좌표가 일치하는 현재 vertex를
    DB에서 다시 찾아 사용한다.
    """
    endpoints = [
        {
            "endpoint_role": "component_side",
            "qa_vertex_id": int(candidate["from_vertex_id"]),
            "x": float(candidate["from_x_5186"]),
            "y": float(candidate["from_y_5186"]),
        },
        {
            "endpoint_role": "main_network_side",
            "qa_vertex_id": int(candidate["to_vertex_id"]),
            "x": float(candidate["to_x_5186"]),
            "y": float(candidate["to_y_5186"]),
        },
    ]

    with psycopg2.connect(**build_db_config()) as connection:
        with connection.cursor() as cursor:
            for endpoint in endpoints:
                cursor.execute(
                    """
                    SELECT
                        vertex_id,
                        ST_Distance(
                            geom,
                            ST_SetSRID(ST_MakePoint(%s, %s), 5186)
                        )::FLOAT8 AS distance_m
                    FROM route_vertex
                    ORDER BY geom <-> ST_SetSRID(ST_MakePoint(%s, %s), 5186)
                    LIMIT 1
                    """,
                    (endpoint["x"], endpoint["y"], endpoint["x"], endpoint["y"]),
                )
                current_vertex_id, distance_m = cursor.fetchone()
                if float(distance_m) > VERTEX_MATCH_TOLERANCE_M:
                    raise RuntimeError(
                        f"{endpoint['endpoint_role']} 좌표와 현재 vertex가 "
                        f"일치하지 않습니다: {distance_m:.6f}m"
                    )
                endpoint["current_vertex_id"] = int(current_vertex_id)
                endpoint["match_distance_m"] = float(distance_m)

            xs = [float(endpoint["x"]) for endpoint in endpoints]
            ys = [float(endpoint["y"]) for endpoint in endpoints]
            cursor.execute(
                """
                SELECT
                    segment_id,
                    source,
                    target,
                    length_m::FLOAT8,
                    ST_AsText(geom)
                FROM route_segment
                WHERE geom && ST_MakeEnvelope(%s, %s, %s, %s, 5186)
                ORDER BY segment_id
                """,
                (
                    min(xs) - CONTEXT_RADIUS_M,
                    min(ys) - CONTEXT_RADIUS_M,
                    max(xs) + CONTEXT_RADIUS_M,
                    max(ys) + CONTEXT_RADIUS_M,
                ),
            )
            context_segments = cursor.fetchall()

    return endpoints, context_segments


def add_field(layer: ogr.Layer, name: str, field_type: int) -> None:
    """OGR 레이어에 필드를 추가하고 실패 시 즉시 중단한다."""
    if layer.CreateField(ogr.FieldDefn(name, field_type)) != ogr.OGRERR_NONE:
        raise RuntimeError(f"GeoPackage 필드 생성 실패: {name}")


def create_review_package(
    candidate: dict[str, str],
    endpoints: list[dict[str, int | float | str]],
    context_segments: list[tuple],
) -> Path:
    """endpoint, 주변망, 검토영역 및 빈 수동 digitizing 레이어를 생성한다."""
    output_path = OUTPUT_DIR / f"manual_connector_{candidate['candidate_id']}_qa.gpkg"
    if output_path.exists():
        raise FileExistsError(
            "기존 수동 작업을 보호하기 위해 덮어쓰지 않습니다. "
            f"파일을 확인하세요: {output_path}"
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    driver = ogr.GetDriverByName("GPKG")
    data_source = driver.CreateDataSource(str(output_path))
    if data_source is None:
        raise RuntimeError(f"GeoPackage 생성 실패: {output_path}")

    spatial_reference = osr.SpatialReference()
    spatial_reference.ImportFromEPSG(5186)

    endpoint_layer = data_source.CreateLayer(
        "connector_endpoints", spatial_reference, ogr.wkbPoint
    )
    for name, field_type in (
        ("candidate_id", ogr.OFTString),
        ("role", ogr.OFTString),
        ("qa_vertex", ogr.OFTInteger64),
        ("vertex_id", ogr.OFTInteger64),
        ("match_m", ogr.OFTReal),
        ("status", ogr.OFTString),
    ):
        add_field(endpoint_layer, name, field_type)

    for endpoint in endpoints:
        feature = ogr.Feature(endpoint_layer.GetLayerDefn())
        feature.SetField("candidate_id", candidate["candidate_id"])
        feature.SetField("role", endpoint["endpoint_role"])
        feature.SetField("qa_vertex", endpoint["qa_vertex_id"])
        feature.SetField("vertex_id", endpoint["current_vertex_id"])
        feature.SetField("match_m", endpoint["match_distance_m"])
        feature.SetField("status", candidate["status"])
        point = ogr.Geometry(ogr.wkbPoint)
        point.AddPoint_2D(float(endpoint["x"]), float(endpoint["y"]))
        feature.SetGeometry(point)
        endpoint_layer.CreateFeature(feature)

    context_layer = data_source.CreateLayer(
        "route_segment_context", spatial_reference, ogr.wkbLineString
    )
    for name, field_type in (
        ("segment_id", ogr.OFTInteger64),
        ("source", ogr.OFTInteger64),
        ("target", ogr.OFTInteger64),
        ("length_m", ogr.OFTReal),
    ):
        add_field(context_layer, name, field_type)

    for segment_id, source, target, length_m, geometry_wkt in context_segments:
        feature = ogr.Feature(context_layer.GetLayerDefn())
        feature.SetField("segment_id", int(segment_id))
        feature.SetField("source", int(source))
        feature.SetField("target", int(target))
        feature.SetField("length_m", float(length_m))
        feature.SetGeometry(ogr.CreateGeometryFromWkt(geometry_wkt))
        context_layer.CreateFeature(feature)

    review_layer = data_source.CreateLayer(
        "connector_review_area", spatial_reference, ogr.wkbPolygon
    )
    add_field(review_layer, "candidate_id", ogr.OFTString)
    add_field(review_layer, "radius_m", ogr.OFTReal)
    review_line = ogr.Geometry(ogr.wkbLineString)
    for endpoint in endpoints:
        review_line.AddPoint_2D(float(endpoint["x"]), float(endpoint["y"]))
    review_feature = ogr.Feature(review_layer.GetLayerDefn())
    review_feature.SetField("candidate_id", candidate["candidate_id"])
    review_feature.SetField("radius_m", CONTEXT_RADIUS_M)
    review_feature.SetGeometry(review_line.Buffer(CONTEXT_RADIUS_M))
    review_layer.CreateFeature(review_feature)

    draft_layer = data_source.CreateLayer(
        "manual_connectors_draft", spatial_reference, ogr.wkbLineString
    )
    for name, field_type in (
        ("candidate_id", ogr.OFTString),
        ("review_status", ogr.OFTString),
        ("access_note", ogr.OFTString),
        ("pet_allowed", ogr.OFTInteger),
        ("source", ogr.OFTInteger64),
        ("target", ogr.OFTInteger64),
        ("length_m", ogr.OFTReal),
    ):
        add_field(draft_layer, name, field_type)

    data_source = None
    return output_path


def main() -> None:
    """후보 좌표를 현재 DB vertex에 맞춘 뒤 QGIS 검토 파일을 만든다."""
    args = parse_args()
    candidate = read_candidate(args.candidate)
    endpoints, context_segments = resolve_current_vertices(candidate)
    output_path = create_review_package(candidate, endpoints, context_segments)

    print(f"[candidate] {candidate['candidate_id']}")
    for endpoint in endpoints:
        print(
            f"  {endpoint['endpoint_role']}: QA vertex "
            f"{endpoint['qa_vertex_id']} -> 현재 vertex "
            f"{endpoint['current_vertex_id']} "
            f"({endpoint['match_distance_m']:.6f}m)"
        )
    print(f"[주변 route_segment] {len(context_segments):,}")
    print(f"[QGIS 검토 파일] {output_path}")
    print("[주의] manual_connectors_draft는 빈 레이어이며 직선이 자동 생성되지 않았습니다.")


if __name__ == "__main__":
    main()
