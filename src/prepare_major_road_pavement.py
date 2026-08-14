from __future__ import annotations

import argparse
import math
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd

from classify_surface import fetch_samples
from d4_common import D4_OUTPUT_DIR, D4_QA_DIR, PIPELINE_ROOT


DEFAULT_OSM_PBF = Path(r"C:\Users\admin\Downloads\south-korea-260812.osm.pbf")
DEFAULT_OSM_REFERENCE = (
    PIPELINE_ROOT / "data" / "raw" / "surface_reference" / "junggu_osm_sidewalk_260812.gpkg"
)
DEFAULT_NETWORK_CSV = PIPELINE_ROOT / "data" / "raw" / "network" / "seoul_walk_network.csv"
DEFAULT_OUTPUT = D4_OUTPUT_DIR / "d4_major_road_pavement_candidates.csv"
DEFAULT_QA = D4_QA_DIR / "d4_major_road_pavement_qa.gpkg"
DEFAULT_REPORT = PIPELINE_ROOT / "reports" / "d4_major_road_pavement_qa.md"

MAJOR_HIGHWAYS = {
    "trunk", "trunk_link",
    "primary", "primary_link",
    "secondary", "secondary_link",
    "tertiary", "tertiary_link",
}
CORRECTION_POLICY = "AUTO_PAVEMENT_MAJOR_ROAD"


def parse_arguments() -> argparse.Namespace:
    """Read major-road matching and QA output options."""
    parser = argparse.ArgumentParser(description="Generate pavement candidates along major roads")
    parser.add_argument("--osm-pbf", type=Path, default=DEFAULT_OSM_PBF)
    parser.add_argument("--osm-reference", type=Path, default=DEFAULT_OSM_REFERENCE)
    parser.add_argument("--network-csv", type=Path, default=DEFAULT_NETWORK_CSV)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--qa-gpkg", type=Path, default=DEFAULT_QA)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--spacing-m", type=float, default=5.0)
    parser.add_argument("--maximum-distance-m", type=float, default=45.0)
    parser.add_argument("--maximum-angle-deg", type=float, default=20.0)
    parser.add_argument("--minimum-support-ratio", type=float, default=0.60)
    parser.add_argument("--overwrite", action="store_true")
    return parser.parse_args()


def _sample_distances(length: float, spacing: float) -> list[float]:
    """Return endpoints and regular interior distances for a line."""
    if length <= 0:
        return [0.0]
    distances = list(np.arange(0.0, length, spacing, dtype=float))
    if not distances or not math.isclose(distances[-1], length):
        distances.append(float(length))
    return distances


def _local_angle(line: object, distance: float, delta: float = 2.5) -> float | None:
    """Measure an undirected local tangent angle in degrees."""
    length = float(line.length)
    if length <= 0:
        return None
    start = max(0.0, float(distance) - delta)
    end = min(length, float(distance) + delta)
    if math.isclose(start, end):
        return None
    first = line.interpolate(start)
    second = line.interpolate(end)
    dx = float(second.x - first.x)
    dy = float(second.y - first.y)
    if math.isclose(dx, 0.0) and math.isclose(dy, 0.0):
        return None
    return math.degrees(math.atan2(dy, dx)) % 180.0


def _angle_difference(first: float, second: float) -> float:
    """Return the smallest difference between undirected line angles."""
    difference = abs(first - second) % 180.0
    return min(difference, 180.0 - difference)


def load_major_road_axes(pbf_path: Path, reference_path: Path) -> gpd.GeoDataFrame:
    """Load and clip OSM major-road centerlines to the Jung-gu boundary."""
    if not pbf_path.is_file():
        raise FileNotFoundError(f"OSM PBF file not found: {pbf_path}")
    if not reference_path.is_file():
        raise FileNotFoundError(f"Jung-gu OSM reference GPKG not found: {reference_path}")

    boundary = gpd.read_file(reference_path, layer="junggu_boundary")
    if boundary.empty:
        raise ValueError("The Jung-gu boundary layer is empty.")
    bounds = tuple(float(value) for value in boundary.to_crs("EPSG:4326").total_bounds)
    highway_values = ",".join(f"'{value}'" for value in sorted(MAJOR_HIGHWAYS))
    roads = gpd.read_file(
        pbf_path,
        layer="lines",
        bbox=bounds,
        where=f"highway IN ({highway_values})",
    )
    if roads.empty:
        raise ValueError("No OSM major-road axes were found within Jung-gu.")
    roads = roads.loc[roads["highway"].isin(MAJOR_HIGHWAYS)].copy()
    roads = roads.to_crs("EPSG:5186").explode(index_parts=False, ignore_index=True)
    roads = roads.loc[
        roads.geometry.notna()
        & ~roads.geometry.is_empty
        & roads.geometry.geom_type.eq("LineString")
    ].copy()
    roads["road_name"] = roads["name"].fillna("").astype(str).str.strip()
    roads["osm_id"] = roads["osm_id"].astype(str)
    return roads[["osm_id", "road_name", "highway", "geometry"]].reset_index(drop=True)


def load_crosswalk_flags(path: Path, route_ids: set[int]) -> dict[int, bool]:
    """Read raw crosswalk flags by positional CSV columns to avoid locale header issues."""
    if not path.is_file():
        raise FileNotFoundError(f"Raw walking-network CSV not found: {path}")
    header = pd.read_csv(path, encoding="cp949", nrows=0).columns.tolist()
    if len(header) < 20:
        raise ValueError("The raw walking-network CSV must contain at least 20 columns.")

    flags: dict[int, bool] = {}
    use_columns = [header[0], header[5], header[19]]
    for chunk in pd.read_csv(
        path,
        encoding="cp949",
        usecols=use_columns,
        chunksize=100_000,
        low_memory=False,
    ):
        chunk.columns = ["record_type", "segment_id", "crosswalk"]
        identifiers = pd.to_numeric(chunk["segment_id"], errors="coerce")
        selected = chunk.loc[identifiers.isin(route_ids)].copy()
        selected["segment_id"] = identifiers.loc[selected.index].astype("int64")
        normalized = selected["crosswalk"].astype(str).str.strip().str.upper()
        selected["is_crosswalk"] = normalized.isin({"1", "Y", "YES", "TRUE", "T"})
        flags.update(dict(zip(selected["segment_id"], selected["is_crosswalk"])))
    return flags


def match_route_to_major_roads(
    route: object,
    roads: gpd.GeoDataFrame,
    road_index: object,
    spacing_m: float,
    maximum_distance_m: float,
    maximum_angle_deg: float,
) -> list[dict[str, object]]:
    """Match regular route samples to the closest locally parallel major-road axis."""
    matches: list[dict[str, object]] = []
    for distance in _sample_distances(float(route.length), spacing_m):
        point = route.interpolate(distance)
        route_angle = _local_angle(route, distance)
        if route_angle is None:
            continue
        positions = road_index.query(point.buffer(maximum_distance_m), predicate="intersects")
        best: dict[str, object] | None = None
        for position in positions:
            road = roads.iloc[int(position)]
            axis = road.geometry
            separation = float(point.distance(axis))
            if separation > maximum_distance_m:
                continue
            road_angle = _local_angle(axis, float(axis.project(point)))
            if road_angle is None:
                continue
            angle = _angle_difference(route_angle, road_angle)
            if angle > maximum_angle_deg:
                continue
            current = {
                "distance_m": separation,
                "angle_deg": angle,
                "osm_id": str(road.osm_id),
                "road_name": str(road.road_name),
                "highway": str(road.highway),
            }
            if best is None or (separation, angle) < (best["distance_m"], best["angle_deg"]):
                best = current
        if best is not None:
            matches.append(best)
    return matches


def identify_candidates(
    routes: gpd.GeoDataFrame,
    roads: gpd.GeoDataFrame,
    crosswalk_flags: dict[int, bool] | None = None,
    spacing_m: float = 5.0,
    maximum_distance_m: float = 45.0,
    maximum_angle_deg: float = 20.0,
    minimum_support_ratio: float = 0.60,
) -> pd.DataFrame:
    """Select non-crosswalk walking links that consistently follow a major road."""
    columns = [
        "segment_id", "road_osm_ids", "road_names", "road_classes",
        "route_sample_count", "supported_sample_count", "support_ratio",
        "median_distance_m", "p90_distance_m", "p90_angle_deg",
        "raw_is_crosswalk", "correction_policy",
    ]
    crosswalk_flags = crosswalk_flags or {}
    road_index = roads.sindex
    records: list[dict[str, object]] = []
    for row in routes.itertuples(index=False):
        segment_id = int(row.segment_id)
        if bool(crosswalk_flags.get(segment_id, False)):
            continue
        geometry = row.geometry
        if geometry is None or geometry.is_empty or geometry.geom_type != "LineString":
            continue
        route_samples = _sample_distances(float(geometry.length), spacing_m)
        matches = match_route_to_major_roads(
            geometry, roads, road_index, spacing_m, maximum_distance_m, maximum_angle_deg
        )
        minimum_supported = min(2, len(route_samples))
        support_ratio = len(matches) / len(route_samples)
        if len(matches) < minimum_supported or support_ratio < minimum_support_ratio:
            continue

        distances = [float(item["distance_m"]) for item in matches]
        angles = [float(item["angle_deg"]) for item in matches]
        names = sorted({str(item["road_name"]) for item in matches if item["road_name"]})
        classes = sorted({str(item["highway"]) for item in matches})
        osm_ids = sorted({str(item["osm_id"]) for item in matches})
        records.append(
            {
                "segment_id": segment_id,
                "road_osm_ids": ";".join(osm_ids),
                "road_names": ";".join(names),
                "road_classes": ";".join(classes),
                "route_sample_count": len(route_samples),
                "supported_sample_count": len(matches),
                "support_ratio": round(support_ratio, 4),
                "median_distance_m": round(float(np.median(distances)), 3),
                "p90_distance_m": round(float(np.percentile(distances, 90)), 3),
                "p90_angle_deg": round(float(np.percentile(angles, 90)), 3),
                "raw_is_crosswalk": False,
                "correction_policy": CORRECTION_POLICY,
            }
        )
    return pd.DataFrame.from_records(records, columns=columns)


def write_outputs(
    candidates: pd.DataFrame,
    routes: gpd.GeoDataFrame,
    roads: gpd.GeoDataFrame,
    output_path: Path,
    qa_path: Path,
    report_path: Path,
    overwrite: bool,
) -> None:
    """Write classifier input, QGIS layers, and an audit report."""
    for path in (output_path, qa_path, report_path):
        path.parent.mkdir(parents=True, exist_ok=True)
    for path in (output_path, qa_path):
        if path.exists() and not overwrite:
            raise FileExistsError(f"Output already exists; use --overwrite: {path}")
        if path.exists():
            path.unlink()

    candidates.to_csv(output_path, index=False, encoding="utf-8-sig")
    candidate_gdf = routes[["segment_id", "geometry"]].merge(
        candidates, on="segment_id", how="inner", validate="one_to_one"
    )
    roads.to_file(qa_path, layer="major_road_axes", driver="GPKG")
    candidate_gdf.to_file(qa_path, layer="pavement_candidates", driver="GPKG", mode="a")

    road_names = (
        candidates.assign(road_name=candidates["road_names"].replace("", "(unnamed)"))
        .groupby("road_name").size().sort_values(ascending=False)
    )
    lines = [
        "# D4 major-road pavement QA", "",
        "## Policy", "",
        "- OSM trunk/primary/secondary/tertiary roads and link classes define major roads.",
        "- Walking links are sampled every 5 m and compared with the local road-axis direction.",
        "- A non-crosswalk link is pavement when at least 60% of samples are within 45 m and 20 degrees of an axis.",
        "- OSM roadway surface tags are never used as sidewalk surface labels.", "",
        "## Result", "",
        f"- Major-road axis parts: {len(roads):,}",
        f"- Pavement walking links: {len(candidates):,}", "",
        "## Candidate count by road name", "",
    ]
    lines.extend(f"- {name}: {count:,}" for name, count in road_names.head(30).items())
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    """Generate major-road pavement candidates without modifying the database."""
    args = parse_arguments()
    _, routes = fetch_samples()
    roads = load_major_road_axes(args.osm_pbf.resolve(), args.osm_reference.resolve())
    route_ids = set(routes["segment_id"].astype(int))
    crosswalk_flags = load_crosswalk_flags(args.network_csv.resolve(), route_ids)
    candidates = identify_candidates(
        routes,
        roads,
        crosswalk_flags,
        spacing_m=args.spacing_m,
        maximum_distance_m=args.maximum_distance_m,
        maximum_angle_deg=args.maximum_angle_deg,
        minimum_support_ratio=args.minimum_support_ratio,
    )
    write_outputs(
        candidates,
        routes,
        roads,
        args.output.resolve(),
        args.qa_gpkg.resolve(),
        args.report.resolve(),
        args.overwrite,
    )
    print(f"[done] major-road axes={len(roads):,}, pavement links={len(candidates):,}")
    print(f"[CSV] {args.output.resolve()}")
    print(f"[QGIS] {args.qa_gpkg.resolve()}")


if __name__ == "__main__":
    main()
