from __future__ import annotations

import argparse
import math
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
from shapely.geometry import LineString

from d4_common import D4_QA_DIR, PIPELINE_ROOT


DEFAULT_OSM_INPUT = (
    PIPELINE_ROOT
    / "data"
    / "raw"
    / "surface_reference"
    / "junggu_osm_sidewalk_260812.gpkg"
)
DEFAULT_ROUTE_INPUT = D4_QA_DIR / "d4_surface_qa.gpkg"
DEFAULT_QA_OUTPUT = D4_QA_DIR / "d5_osm_sidewalk_surface_qa.gpkg"
DEFAULT_CSV_OUTPUT = (
    PIPELINE_ROOT / "data" / "processed" / "d5" / "osm_sidewalk_surface_matches.csv"
)
DEFAULT_REPORT = PIPELINE_ROOT / "reports" / "d5_osm_sidewalk_surface_qa.md"

OSM_SURFACE_MAP = {
    "paving_stones": "pavement",
    "sett": "pavement",
    "asphalt": "asphalt",
}


def parse_arguments() -> argparse.Namespace:
    """Read paths and conservative spatial matching thresholds."""
    parser = argparse.ArgumentParser(
        description="Match explicit OSM sidewalk surfaces to MungRoute links for QA"
    )
    parser.add_argument("--osm-input", type=Path, default=DEFAULT_OSM_INPUT)
    parser.add_argument("--route-input", type=Path, default=DEFAULT_ROUTE_INPUT)
    parser.add_argument("--qa-output", type=Path, default=DEFAULT_QA_OUTPUT)
    parser.add_argument("--csv-output", type=Path, default=DEFAULT_CSV_OUTPUT)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--maximum-distance-m", type=float, default=3.0)
    parser.add_argument("--maximum-angle-deg", type=float, default=20.0)
    parser.add_argument("--minimum-support-ratio", type=float, default=0.70)
    parser.add_argument("--minimum-overlap-ratio", type=float, default=0.70)
    parser.add_argument("--overwrite", action="store_true")
    return parser.parse_args()


def _line_angle(line: LineString, distance: float, delta: float = 2.5) -> float:
    """Return the undirected local tangent angle at a distance along a line."""
    start = line.interpolate(max(0.0, distance - delta))
    end = line.interpolate(min(float(line.length), distance + delta))
    if start.equals(end):
        return 0.0
    return math.degrees(math.atan2(end.y - start.y, end.x - start.x)) % 180.0


def _angle_difference(first: float, second: float) -> float:
    """Return the smallest difference between two undirected line angles."""
    difference = abs(first - second) % 180.0
    return min(difference, 180.0 - difference)


def _sample_distances(length: float, spacing: float = 5.0) -> np.ndarray:
    """Create stable endpoints plus roughly five-metre interior samples."""
    if length <= 0:
        return np.asarray([0.0])
    values = list(np.arange(0.0, length, spacing))
    if not values or not math.isclose(values[-1], length):
        values.append(length)
    return np.asarray(values, dtype=float)


def _pair_metrics(
    route: LineString,
    sidewalk: LineString,
    maximum_distance_m: float,
    maximum_angle_deg: float,
) -> dict[str, float | int]:
    """Measure local distance/direction support for one route/OSM line pair."""
    distances: list[float] = []
    angles: list[float] = []
    supported = 0
    route_distances = _sample_distances(float(route.length))
    for route_distance in route_distances:
        point = route.interpolate(float(route_distance))
        osm_distance = float(sidewalk.project(point))
        separation = float(point.distance(sidewalk.interpolate(osm_distance)))
        angle = _angle_difference(
            _line_angle(route, float(route_distance)),
            _line_angle(sidewalk, osm_distance),
        )
        distances.append(separation)
        angles.append(angle)
        if separation <= maximum_distance_m and angle <= maximum_angle_deg:
            supported += 1

    overlap = float(route.intersection(sidewalk.buffer(maximum_distance_m)).length)
    return {
        "sample_count": len(route_distances),
        "supported_sample_count": supported,
        "support_ratio": supported / len(route_distances),
        "overlap_ratio": min(1.0, overlap / max(float(route.length), 0.001)),
        "median_distance_m": float(np.median(distances)),
        "p90_distance_m": float(np.percentile(distances, 90)),
        "p90_angle_deg": float(np.percentile(angles, 90)),
    }


def load_explicit_osm_sidewalks(path: Path) -> gpd.GeoDataFrame:
    """Load only separately mapped sidewalks with an explicit usable surface."""
    osm = gpd.read_file(path, layer="sidewalk_surface_known").to_crs("EPSG:5186")
    surface = osm["surface"].fillna("").str.strip().str.lower()
    mask = (
        osm["highway"].fillna("").eq("footway")
        & osm["footway"].fillna("").eq("sidewalk")
        & surface.isin(OSM_SURFACE_MAP)
    )
    result = osm.loc[mask].copy()
    result["osm_surface"] = surface.loc[mask]
    result["mapped_surface"] = result["osm_surface"].map(OSM_SURFACE_MAP)
    return result.reset_index(drop=True)


def match_routes_to_osm(
    routes: gpd.GeoDataFrame,
    sidewalks: gpd.GeoDataFrame,
    maximum_distance_m: float = 3.0,
    maximum_angle_deg: float = 20.0,
    minimum_support_ratio: float = 0.70,
    minimum_overlap_ratio: float = 0.70,
) -> gpd.GeoDataFrame:
    """Build review-only matches between route links and explicit OSM sidewalks."""
    if routes.crs != sidewalks.crs:
        sidewalks = sidewalks.to_crs(routes.crs)
    spatial_index = sidewalks.sindex
    records: list[dict[str, object]] = []

    for route in routes.itertuples():
        positions = spatial_index.query(
            route.geometry.buffer(maximum_distance_m), predicate="intersects"
        )
        accepted: list[dict[str, object]] = []
        for position in positions:
            sidewalk = sidewalks.iloc[int(position)]
            metrics = _pair_metrics(
                route.geometry,
                sidewalk.geometry,
                maximum_distance_m,
                maximum_angle_deg,
            )
            if (
                metrics["supported_sample_count"] < 2
                or metrics["support_ratio"] < minimum_support_ratio
                or metrics["overlap_ratio"] < minimum_overlap_ratio
                or metrics["median_distance_m"] > maximum_distance_m
                or metrics["p90_angle_deg"] > maximum_angle_deg
            ):
                continue
            accepted.append(
                {
                    "osm_id": str(sidewalk.osm_id),
                    "osm_surface": str(sidewalk.osm_surface),
                    "mapped_surface": str(sidewalk.mapped_surface),
                    **metrics,
                }
            )

        if not accepted:
            continue
        accepted.sort(
            key=lambda item: (
                -float(item["support_ratio"]),
                -float(item["overlap_ratio"]),
                float(item["median_distance_m"]),
            )
        )
        best = accepted[0]
        mapped = sorted({str(item["mapped_surface"]) for item in accepted})
        current_surface = str(route.surface_type)
        if len(mapped) > 1:
            decision = "REVIEW_OSM_AMBIGUOUS"
            proposed_surface = ""
        else:
            proposed_surface = mapped[0]
            decision = (
                "REVIEW_OSM_CONFIRM"
                if current_surface == proposed_surface
                else "REVIEW_OSM_CONFLICT"
            )
        records.append(
            {
                "segment_id": int(route.segment_id),
                "current_surface": current_surface,
                "current_basis": str(getattr(route, "classification_basis", "")),
                "osm_ids": ",".join(sorted({str(item["osm_id"]) for item in accepted})),
                "osm_surface_values": ",".join(
                    sorted({str(item["osm_surface"]) for item in accepted})
                ),
                "proposed_surface": proposed_surface,
                "match_count": len(accepted),
                "sample_count": int(best["sample_count"]),
                "supported_sample_count": int(best["supported_sample_count"]),
                "support_ratio": round(float(best["support_ratio"]), 4),
                "overlap_ratio": round(float(best["overlap_ratio"]), 4),
                "median_distance_m": round(float(best["median_distance_m"]), 3),
                "p90_distance_m": round(float(best["p90_distance_m"]), 3),
                "p90_angle_deg": round(float(best["p90_angle_deg"]), 3),
                "decision": decision,
                "geometry": route.geometry,
            }
        )

    columns = [
        "segment_id",
        "current_surface",
        "current_basis",
        "osm_ids",
        "osm_surface_values",
        "proposed_surface",
        "match_count",
        "sample_count",
        "supported_sample_count",
        "support_ratio",
        "overlap_ratio",
        "median_distance_m",
        "p90_distance_m",
        "p90_angle_deg",
        "decision",
        "geometry",
    ]
    if not records:
        return gpd.GeoDataFrame(columns=columns, geometry="geometry", crs=routes.crs)
    return gpd.GeoDataFrame(records, columns=columns, geometry="geometry", crs=routes.crs)


def write_report(
    sidewalks: gpd.GeoDataFrame, matches: gpd.GeoDataFrame, path: Path
) -> None:
    """Write reproducible coverage and decision counts for the QA handoff."""
    surface_counts = sidewalks["mapped_surface"].value_counts().to_dict()
    decision_counts = (
        {} if matches.empty else matches["decision"].value_counts().to_dict()
    )
    to_pavement = matches.loc[
        matches["current_surface"].eq("asphalt")
        & matches["proposed_surface"].eq("pavement")
    ].sort_values("segment_id")
    to_asphalt = matches.loc[
        matches["current_surface"].eq("pavement")
        & matches["proposed_surface"].eq("asphalt")
    ].sort_values("segment_id")
    lines = [
        "# D5 OSM 명시 보도 재질 QA",
        "",
        "## 정책",
        "",
        "- highway=footway, footway=sidewalk, surface가 명시된 선만 사용한다.",
        "- 도로 자체의 surface 태그는 보도 재질로 사용하지 않는다.",
        "- 거리 3m, 국소 방향차 20도, 링크 지지율/중첩률 70%를 모두 통과해야 한다.",
        "- OSM 결과는 REVIEW 증거이며 이 단계에서는 재질과 DB를 변경하지 않는다.",
        "",
        "## 결과",
        "",
        f"- 사용한 OSM 명시 보도: {len(sidewalks):,}개",
        f"- 정밀 매칭된 멍루트 링크: {len(matches):,}개",
    ]
    lines.extend(f"- OSM {key}: {value:,}개" for key, value in sorted(surface_counts.items()))
    lines.extend(f"- {key}: {value:,}개" for key, value in sorted(decision_counts.items()))
    lines.extend(
        [
            f"- asphalt → pavement 검토 후보: {len(to_pavement):,}개",
            f"- pavement → asphalt 역방향 충돌 후보: {len(to_asphalt):,}개",
            "",
            "## asphalt → pavement 검토 후보",
            "",
            "아래 링크는 OSM에 `footway=sidewalk`와 `surface=paving_stones`가 직접 명시되어 있지만,",
            "OSM만으로 자동 보정하지 않고 QGIS 확인 후 승인 목록에 넣는다.",
            "",
            "| segment_id | OSM surface | support | overlap | median distance(m) | p90 angle(°) |",
            "|---:|---|---:|---:|---:|---:|",
        ]
    )
    for row in to_pavement.itertuples():
        lines.append(
            f"| {int(row.segment_id)} | {row.osm_surface_values} | "
            f"{float(row.support_ratio):.3f} | {float(row.overlap_ratio):.3f} | "
            f"{float(row.median_distance_m):.2f} | {float(row.p90_angle_deg):.2f} |"
        )
    lines.extend(
        [
            "",
            "## QGIS 레이어",
            "",
            "- `review_to_pavement`: 현재 asphalt이지만 OSM 명시 보도는 pavement인 링크",
            "- `review_to_asphalt`: 현재 pavement이지만 OSM 명시 보도는 asphalt인 링크",
            "- `route_osm_surface_matches`: 확인·충돌을 모두 포함한 전체 정밀 매칭",
            "- `osm_sidewalk_surface_known`: 매칭에 사용한 OSM 원본 보도선",
            "",
            "## 적용 제한",
            "",
            "- 이 산출물은 검토 목록이며 `d4_sample_surface.csv`, `d4_route_surface.csv`, DB를 변경하지 않는다.",
            f"- 대로변 전체 보정은 OSM 미기재 구간이 많으므로 이 {len(to_pavement):,}개만으로 끝나지 않는다.",
            "- 기하 기반 대로 후보는 `d4_surface_qa.gpkg`의 `secondary_major_road_candidates`에서 별도로 검토한다.",
        ]
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    """Create OSM surface evidence layers and leave production data unchanged."""
    args = parse_arguments()
    for path in (args.qa_output, args.csv_output, args.report):
        path = path.resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and not args.overwrite:
            raise FileExistsError(f"기존 산출물이 있습니다. --overwrite를 사용하세요: {path}")
        if path.exists():
            path.unlink()

    sidewalks = load_explicit_osm_sidewalks(args.osm_input.resolve())
    routes = gpd.read_file(args.route_input.resolve(), layer="route_surface").to_crs(
        "EPSG:5186"
    )
    matches = match_routes_to_osm(
        routes,
        sidewalks,
        args.maximum_distance_m,
        args.maximum_angle_deg,
        args.minimum_support_ratio,
        args.minimum_overlap_ratio,
    )
    sidewalks.to_file(args.qa_output.resolve(), layer="osm_sidewalk_surface_known", driver="GPKG")
    if not matches.empty:
        matches.to_file(
            args.qa_output.resolve(), layer="route_osm_surface_matches", driver="GPKG", mode="a"
        )
        to_pavement = matches.loc[
            matches["current_surface"].eq("asphalt")
            & matches["proposed_surface"].eq("pavement")
        ].copy()
        to_asphalt = matches.loc[
            matches["current_surface"].eq("pavement")
            & matches["proposed_surface"].eq("asphalt")
        ].copy()
        if not to_pavement.empty:
            to_pavement.to_file(
                args.qa_output.resolve(), layer="review_to_pavement", driver="GPKG", mode="a"
            )
        if not to_asphalt.empty:
            to_asphalt.to_file(
                args.qa_output.resolve(), layer="review_to_asphalt", driver="GPKG", mode="a"
            )
    matches.drop(columns="geometry").to_csv(
        args.csv_output.resolve(), index=False, encoding="utf-8-sig"
    )
    write_report(sidewalks, matches, args.report.resolve())
    print(f"[OSM 명시 보도] {len(sidewalks):,}개")
    print(f"[정밀 매칭 링크] {len(matches):,}개")
    print(f"[QA] {args.qa_output.resolve()}")
    print("[안내] 재질 CSV와 DB는 수정하지 않았습니다.")


if __name__ == "__main__":
    main()
