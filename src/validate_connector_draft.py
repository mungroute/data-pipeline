import argparse
from pathlib import Path

from osgeo import ogr


PIPELINE_ROOT = Path(__file__).resolve().parents[1]
CONNECTOR_DIR = PIPELINE_ROOT / "data" / "interim" / "network"
ENDPOINT_TOLERANCE_M = 1.0
APPROVED_STATUS = "APPROVED"


def parse_args() -> argparse.Namespace:
    """검증할 수동 connector 후보 ID를 받는다."""
    parser = argparse.ArgumentParser(
        description="QGIS에서 그린 수동 connector geometry를 검증합니다."
    )
    parser.add_argument("--candidate", required=True)
    return parser.parse_args()


def distance(point_a: ogr.Geometry, point_b: ogr.Geometry) -> float:
    """두 OGR geometry 사이의 EPSG:5186 평면거리를 반환한다."""
    return float(point_a.Distance(point_b))


def validate_and_update(candidate_id: str) -> dict[str, int | float | str]:
    """승인된 draft 한 건의 geometry·접근성·endpoint를 검사하고 ID를 채운다."""
    package_path = CONNECTOR_DIR / f"manual_connector_{candidate_id}_qa.gpkg"
    if not package_path.exists():
        raise FileNotFoundError(f"connector QA 파일이 없습니다: {package_path}")

    data_source = ogr.Open(str(package_path), update=1)
    if data_source is None:
        raise RuntimeError(f"GeoPackage를 열 수 없습니다: {package_path}")

    endpoint_layer = data_source.GetLayerByName("connector_endpoints")
    draft_layer = data_source.GetLayerByName("manual_connectors_draft")
    if endpoint_layer is None or draft_layer is None:
        raise RuntimeError("connector_endpoints 또는 manual_connectors_draft가 없습니다.")
    if endpoint_layer.GetFeatureCount() != 2:
        raise RuntimeError("connector endpoint는 정확히 2개여야 합니다.")
    if draft_layer.GetFeatureCount() != 1:
        raise RuntimeError(
            "manual_connectors_draft에 검토 대상 LineString을 정확히 1개 그려야 합니다."
        )

    endpoints: dict[str, tuple[int, ogr.Geometry]] = {}
    endpoint_layer.ResetReading()
    for feature in endpoint_layer:
        role = feature.GetField("role")
        endpoints[role] = (
            int(feature.GetField("vertex_id")),
            feature.GetGeometryRef().Clone(),
        )
    required_roles = {"component_side", "main_network_side"}
    if set(endpoints) != required_roles:
        raise RuntimeError(f"endpoint role이 올바르지 않습니다: {set(endpoints)}")

    draft_layer.ResetReading()
    draft = next(iter(draft_layer))
    if draft.GetField("candidate_id") != candidate_id:
        raise RuntimeError("draft candidate_id가 파일의 후보 ID와 다릅니다.")
    if (draft.GetField("review_status") or "").strip().upper() != APPROVED_STATUS:
        raise RuntimeError("review_status를 APPROVED로 확정해야 합니다.")
    if not (draft.GetField("access_note") or "").strip():
        raise RuntimeError("출입 시간·차단문 확인 내용을 access_note에 기록해야 합니다.")
    if int(draft.GetField("pet_allowed") or 0) != 1:
        raise RuntimeError("반려견 통행 가능 확인 전에는 connector를 승인할 수 없습니다.")

    geometry = draft.GetGeometryRef()
    if geometry is None or geometry.IsEmpty() or not geometry.IsValid():
        raise RuntimeError("draft geometry가 비어 있거나 유효하지 않습니다.")
    if ogr.GT_Flatten(geometry.GetGeometryType()) != ogr.wkbLineString:
        raise RuntimeError("draft geometry는 LineString이어야 합니다.")
    if geometry.GetPointCount() < 2 or geometry.Length() <= 0:
        raise RuntimeError("draft LineString의 좌표나 길이가 올바르지 않습니다.")

    start = ogr.Geometry(ogr.wkbPoint)
    start.AddPoint_2D(geometry.GetX(0), geometry.GetY(0))
    last_index = geometry.GetPointCount() - 1
    end = ogr.Geometry(ogr.wkbPoint)
    end.AddPoint_2D(geometry.GetX(last_index), geometry.GetY(last_index))

    component_vertex, component_point = endpoints["component_side"]
    main_vertex, main_point = endpoints["main_network_side"]
    forward_distance = distance(start, component_point) + distance(end, main_point)
    reverse_distance = distance(start, main_point) + distance(end, component_point)

    if forward_distance <= reverse_distance:
        source, target = component_vertex, main_vertex
        start_distance = distance(start, component_point)
        end_distance = distance(end, main_point)
    else:
        source, target = main_vertex, component_vertex
        start_distance = distance(start, main_point)
        end_distance = distance(end, component_point)

    if start_distance > ENDPOINT_TOLERANCE_M or end_distance > ENDPOINT_TOLERANCE_M:
        raise RuntimeError(
            "connector 양 끝이 endpoint 1m 허용오차 안에 붙지 않았습니다: "
            f"start={start_distance:.3f}m, end={end_distance:.3f}m"
        )

    geometry_length_m = float(geometry.Length())
    draft.SetField("source", source)
    draft.SetField("target", target)
    draft.SetField("length_m", geometry_length_m)
    if draft_layer.SetFeature(draft) != ogr.OGRERR_NONE:
        raise RuntimeError("검증된 source/target/length_m 저장에 실패했습니다.")
    data_source = None

    return {
        "candidate_id": candidate_id,
        "source": source,
        "target": target,
        "length_m": geometry_length_m,
        "start_distance_m": start_distance,
        "end_distance_m": end_distance,
    }


def main() -> None:
    """수동 digitizing 결과를 검증하고 승인 결과를 출력한다."""
    args = parse_args()
    result = validate_and_update(args.candidate)
    print(f"[connector 승인] {result['candidate_id']}")
    print(f"  source/target: {result['source']} -> {result['target']}")
    print(f"  geometry 길이: {result['length_m']:.3f}m")
    print(
        "  endpoint 오차: "
        f"{result['start_distance_m']:.3f}m / {result['end_distance_m']:.3f}m"
    )


if __name__ == "__main__":
    main()
