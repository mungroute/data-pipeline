from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass
import math
from pathlib import Path
import sys

import geopandas as gpd
import numpy as np
import pandas as pd
import psycopg2
from osgeo import ogr
from shapely import from_wkb
from shapely.geometry import Point, box
from shapely.strtree import STRtree

from d4_common import (
    D4_OUTPUT_DIR,
    D4_QA_DIR,
    EXPECTED_SAMPLE_COUNT,
    EXPECTED_SEGMENT_COUNT,
    PIPELINE_ROOT,
    SURFACE_PROPERTIES,
    ensure_expected_count,
    sample_support_weights,
    weighted_mean,
    weighted_mode,
)
from load_segments import build_db_config


DEFAULT_LANDCOVER_DIR = PIPELINE_ROOT / "data" / "raw" / "landcover"
DEFAULT_SAMPLE_OUTPUT = D4_OUTPUT_DIR / "d4_sample_surface.csv"
DEFAULT_ROUTE_OUTPUT = D4_OUTPUT_DIR / "d4_route_surface.csv"
DEFAULT_QA_OUTPUT = D4_QA_DIR / "d4_surface_qa.gpkg"
DEFAULT_REPORT = PIPELINE_ROOT / "reports" / "d4_surface_qa.md"
DEFAULT_OVERRIDE_PATH = PIPELINE_ROOT / "config" / "surface_overrides.csv"
DEFAULT_TDATA_SEGMENT_CANDIDATES = (
    PIPELINE_ROOT / "data" / "processed" / "d5" / "tdata_segment_candidates.csv"
)
DEFAULT_SECONDARY_CANDIDATES = D4_OUTPUT_DIR / "d4_major_road_pair_candidates.csv"
DEFAULT_MAJOR_ROAD_PAVEMENT_CANDIDATES = (
    D4_OUTPUT_DIR / "d4_major_road_pavement_candidates.csv"
)

# Land-cover polygons describe surrounding context, not the material underfoot.
# Grass/soil therefore require a field-confirmed manual override.
SOIL_CODES = {"222", "231", "251", "252", "311", "321", "331", "612", "613", "623"}
GRASS_CODES = {"411", "422", "423", "622"}
AMBIGUOUS_URBAN_CODES = {
    "111", "112", "121", "131", "132", "141", "153", "154", "155",
    "161", "162", "163", "511", "711", "712",
}
QUALITY_RANK = {"A": 0, "B": 1, "C": 2, "D": 3}


@dataclass(frozen=True)
class LandcoverPolygon:
    """공간 검색에 필요한 토지피복 속성과 도형을 묶는다."""

    code: str
    name: str
    area: float
    geometry: object


def parse_arguments() -> argparse.Namespace:
    """파일 기반 계산 옵션을 읽는다. 이 스크립트는 DB를 수정하지 않는다."""
    parser = argparse.ArgumentParser(description="D4-3 노면 재질 분류와 D4-4 링크 물성 집계")
    parser.add_argument("--landcover-dir", type=Path, default=DEFAULT_LANDCOVER_DIR)
    parser.add_argument("--sample-output", type=Path, default=DEFAULT_SAMPLE_OUTPUT)
    parser.add_argument("--route-output", type=Path, default=DEFAULT_ROUTE_OUTPUT)
    parser.add_argument("--qa-gpkg", type=Path, default=DEFAULT_QA_OUTPUT)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--overrides", type=Path, default=DEFAULT_OVERRIDE_PATH)
    parser.add_argument(
        "--tdata-segment-candidates",
        type=Path,
        default=DEFAULT_TDATA_SEGMENT_CANDIDATES,
        help="T-DATA 보도면 QA의 링크별 판정 CSV. AUTO_PAVEMENT만 적용합니다.",
    )
    parser.add_argument(
        "--secondary-candidates-output",
        type=Path,
        default=DEFAULT_SECONDARY_CANDIDATES,
        help="T-DATA 인접 평행 대로변 보도 2차 분류 후보 CSV",
    )
    parser.add_argument(
        "--major-road-pavement-candidates",
        type=Path,
        default=DEFAULT_MAJOR_ROAD_PAVEMENT_CANDIDATES,
        help=(
            "OSM major-road-axis matched walk links. Non-crosswalk links marked "
            "AUTO_PAVEMENT_MAJOR_ROAD are classified as pavement."
        ),
    )
    parser.add_argument("--nearest-m", type=float, default=5.0)
    parser.add_argument("--overwrite", action="store_true")
    return parser.parse_args()


def fetch_samples() -> tuple[pd.DataFrame, gpd.GeoDataFrame]:
    """샘플 좌표·링크 위치·링크 유형과 최종 링크 도형을 읽는다."""
    with psycopg2.connect(**build_db_config()) as connection:
        sample_sql = """
            SELECT p.sample_id, p.segment_id, p.seq,
                   ST_X(p.geom) AS x, ST_Y(p.geom) AS y,
                   ST_LineLocatePoint(r.geom, p.geom) * r.length_m AS chainage_m,
                   r.length_m, s.link_type_code
            FROM segment_sample_point p
            JOIN route_segment r USING (segment_id)
            JOIN route_segment_staging s USING (segment_id)
            ORDER BY p.segment_id, p.seq
        """
        samples = pd.read_sql_query(sample_sql, connection)
        route_sql = """
            SELECT r.segment_id, r.source, r.target, r.length_m, s.link_type_code,
                   ST_AsBinary(r.geom) AS geom_wkb
            FROM route_segment r
            JOIN route_segment_staging s USING (segment_id)
            ORDER BY r.segment_id
        """
        routes = pd.read_sql_query(route_sql, connection)

    ensure_expected_count(len(samples), EXPECTED_SAMPLE_COUNT, "샘플")
    ensure_expected_count(len(routes), EXPECTED_SEGMENT_COUNT, "링크")
    routes["geometry"] = routes["geom_wkb"].map(lambda value: from_wkb(bytes(value)))
    route_gdf = gpd.GeoDataFrame(routes.drop(columns="geom_wkb"), geometry="geometry", crs="EPSG:5186")
    return samples, route_gdf


def load_landcover_polygons(directory: Path, bounds: tuple[float, float, float, float]) -> list[LandcoverPolygon]:
    """압축 세분류 지도를 직접 읽고 분석 범위와 겹치는 도형만 메모리에 적재한다."""
    ogr.UseExceptions()
    analysis_box = box(*bounds)
    polygons: list[LandcoverPolygon] = []
    seen: set[tuple[str, bytes]] = set()
    zip_paths = sorted(directory.rglob("*.zip"))
    if not zip_paths:
        raise FileNotFoundError(f"토지피복 ZIP이 없습니다: {directory}")

    for zip_path in zip_paths:
        dataset = ogr.Open("/vsizip/" + str(zip_path).replace("\\", "/"))
        if dataset is None:
            raise RuntimeError(f"토지피복 ZIP을 열 수 없습니다: {zip_path}")
        layer = dataset.GetLayer(0)
        layer.SetSpatialFilterRect(*bounds)
        for feature in layer:
            geometry = from_wkb(bytes(feature.GetGeometryRef().ExportToWkb()))
            if geometry.is_empty or not geometry.intersects(analysis_box):
                continue
            code = str(feature.GetField("L3_CODE") or "").strip()
            name = str(feature.GetField("L3_NAME") or "").strip()
            key = (code, geometry.wkb)
            if key in seen:
                continue
            seen.add(key)
            polygons.append(LandcoverPolygon(code, name, float(geometry.area), geometry))
    if not polygons:
        raise ValueError("분석 범위의 토지피복 도형이 없습니다.")
    return polygons


def has_vehicle_access(link_type_code: str) -> bool:
    """4자리 링크 코드의 두 번째 비트가 차량 통행 가능 여부인지 판정한다."""
    code = str(link_type_code).strip().zfill(4)
    if len(code) != 4 or set(code) - {"0", "1"}:
        raise ValueError(f"잘못된 링크 유형 코드입니다: {link_type_code}")
    return code[1] == "1"


def classify_material(landcover_code: str | None, link_type_code: str) -> tuple[str, str, str]:
    """토지피복과 통행 주체를 결합해 재질·근거·품질등급을 반환한다."""
    code = (landcover_code or "").strip()
    link_surface = "asphalt" if has_vehicle_access(link_type_code) else "pavement"
    # Land-cover polygons may include a sidewalk beside a planted strip. Treat
    # natural cover as context, not as proof of the material under a pedestrian.
    if code in GRASS_CODES or code in SOIL_CODES:
        return link_surface, "LANDCOVER_CONTEXT_LINKTYPE", "C"
    if code in AMBIGUOUS_URBAN_CODES:
        return link_surface, "LANDCOVER_LINKTYPE", "B"
    return link_surface, "DEFAULT_LINKTYPE", "D"


def load_surface_overrides(path: Path) -> pd.DataFrame:
    """Load strictly validated, field-confirmed segment material overrides."""
    columns = ["segment_id", "surface_type", "reason", "verified_by"]
    if not path.exists():
        return pd.DataFrame(columns=columns)
    overrides = pd.read_csv(path, encoding="utf-8-sig")
    missing = set(columns) - set(overrides.columns)
    if missing:
        raise ValueError("Missing surface override columns: " + ", ".join(sorted(missing)))
    overrides = overrides[columns].dropna(subset=["segment_id", "surface_type"]).copy()
    if overrides.empty:
        return overrides
    overrides["segment_id"] = overrides["segment_id"].astype("int64")
    overrides["surface_type"] = overrides["surface_type"].astype(str).str.strip()
    if overrides["segment_id"].duplicated().any():
        duplicated = overrides.loc[overrides["segment_id"].duplicated(), "segment_id"].tolist()
        raise ValueError(f"Duplicate surface override segment_id: {duplicated}")
    invalid = sorted(set(overrides["surface_type"]) - set(SURFACE_PROPERTIES))
    if invalid:
        raise ValueError("Unsupported override surface: " + ", ".join(invalid))
    return overrides


def apply_surface_overrides(samples: pd.DataFrame, overrides: pd.DataFrame) -> pd.DataFrame:
    """Apply a verified material and its properties to every sample on a segment."""
    if overrides.empty:
        return samples
    missing_ids = sorted(set(overrides["segment_id"]) - set(samples["segment_id"]))
    if missing_ids:
        raise ValueError(f"Override segment_id has no DB samples: {missing_ids}")

    result = samples.copy()
    for row in overrides.itertuples(index=False):
        mask = result["segment_id"] == int(row.segment_id)
        properties = SURFACE_PROPERTIES[str(row.surface_type)]
        result.loc[mask, "surface_type"] = str(row.surface_type)
        result.loc[mask, "albedo"] = properties.albedo
        result.loc[mask, "emissivity"] = properties.emissivity
        result.loc[mask, "ground_flux_ratio"] = properties.ground_flux_ratio
        result.loc[mask, "classification_basis"] = "MANUAL_VERIFIED"
        result.loc[mask, "surface_quality"] = "A"
    return result


def load_tdata_surface_overrides(path: Path) -> pd.DataFrame:
    """검증된 T-DATA 보도면 후보 중 안전한 자동 보정 링크만 읽는다."""
    columns = [
        "segment_id", "safe_candidate_surface", "safe_sample_count",
        "safe_coverage_ratio", "safe_candidate_agreement_ratio", "correction_policy",
    ]
    if not path.exists():
        return pd.DataFrame(columns=columns)

    candidates = pd.read_csv(path, encoding="utf-8-sig")
    missing = set(columns) - set(candidates.columns)
    if missing:
        raise ValueError("Missing T-DATA candidate columns: " + ", ".join(sorted(missing)))
    candidates = candidates.loc[
        candidates["correction_policy"].eq("AUTO_PAVEMENT"), columns
    ].copy()
    if candidates.empty:
        return candidates

    candidates["segment_id"] = candidates["segment_id"].astype("int64")
    if candidates["segment_id"].duplicated().any():
        raise ValueError("T-DATA AUTO_PAVEMENT segment_id가 중복되었습니다.")
    invalid = candidates.loc[
        ~candidates["safe_candidate_surface"].eq("pavement")
        | candidates["safe_sample_count"].lt(2)
        | candidates["safe_coverage_ratio"].lt(0.60)
        | candidates["safe_candidate_agreement_ratio"].lt(1.00)
    ]
    if not invalid.empty:
        raise ValueError("AUTO_PAVEMENT 안전 기준을 충족하지 않는 T-DATA 후보가 있습니다.")
    return candidates


def apply_tdata_surface_overrides(samples: pd.DataFrame, candidates: pd.DataFrame) -> pd.DataFrame:
    """공식 보도면의 안전 판정 링크를 pavement로 보정하되 현장 확정보다 낮게 평가한다."""
    if candidates.empty:
        return samples
    missing_ids = sorted(set(candidates["segment_id"]) - set(samples["segment_id"]))
    if missing_ids:
        raise ValueError(f"T-DATA segment_id has no DB samples: {missing_ids}")

    result = samples.copy()
    segment_ids = set(candidates["segment_id"].astype(int))
    mask = result["segment_id"].isin(segment_ids)
    properties = SURFACE_PROPERTIES["pavement"]
    result.loc[mask, "surface_type"] = "pavement"
    result.loc[mask, "albedo"] = properties.albedo
    result.loc[mask, "emissivity"] = properties.emissivity
    result.loc[mask, "ground_flux_ratio"] = properties.ground_flux_ratio
    result.loc[mask, "classification_basis"] = "T_DATA_SAFE_SIDEWALK"
    result.loc[mask, "surface_quality"] = "B"
    return result


def load_major_road_pavement_candidates(path: Path) -> pd.DataFrame:
    """Load locally aligned, non-crosswalk walk links beside major-road axes."""
    columns = [
        "segment_id", "road_osm_ids", "road_names", "road_classes",
        "route_sample_count", "supported_sample_count", "support_ratio",
        "median_distance_m", "p90_distance_m", "p90_angle_deg",
        "raw_is_crosswalk", "correction_policy",
    ]
    if not path.exists():
        raise FileNotFoundError(
            "Major-road pavement candidates do not exist. Run "
            "prepare_major_road_pavement.py first: " + str(path)
        )

    candidates = pd.read_csv(path, encoding="utf-8-sig")
    missing = set(columns) - set(candidates.columns)
    if missing:
        raise ValueError(
            "Missing major-road pavement candidate columns: "
            + ", ".join(sorted(missing))
        )
    candidates = candidates.loc[
        candidates["correction_policy"].eq("AUTO_PAVEMENT_MAJOR_ROAD"), columns
    ].copy()
    if candidates.empty:
        return candidates

    candidates["segment_id"] = candidates["segment_id"].astype("int64")
    if candidates["segment_id"].duplicated().any():
        raise ValueError("Major-road pavement candidate segment_id is duplicated.")

    raw_crosswalk = candidates["raw_is_crosswalk"].astype(str).str.lower()
    invalid = candidates.loc[
        raw_crosswalk.isin({"true", "1", "yes"})
        | candidates["route_sample_count"].lt(2)
        | candidates["supported_sample_count"].lt(2)
        | candidates["support_ratio"].lt(0.60)
        | candidates["p90_angle_deg"].gt(20.0)
        | candidates["p90_distance_m"].gt(45.0)
    ]
    if not invalid.empty:
        raise ValueError(
            "Major-road pavement candidates contain links that fail the gates: "
            + ", ".join(invalid["segment_id"].astype(str).head(20))
        )
    return candidates


def apply_major_road_pavement_overrides(
    samples: pd.DataFrame,
    candidates: pd.DataFrame,
) -> pd.DataFrame:
    """Classify every aligned non-crosswalk walking link on a major road as pavement."""
    if candidates.empty:
        return samples
    missing_ids = sorted(set(candidates["segment_id"]) - set(samples["segment_id"]))
    if missing_ids:
        raise ValueError(f"Major-road segment_id has no DB samples: {missing_ids}")

    result = samples.copy()
    lookup = candidates.set_index("segment_id")
    mask = result["segment_id"].isin(lookup.index)
    properties = SURFACE_PROPERTIES["pavement"]
    result.loc[mask, "surface_type"] = "pavement"
    result.loc[mask, "albedo"] = properties.albedo
    result.loc[mask, "emissivity"] = properties.emissivity
    result.loc[mask, "ground_flux_ratio"] = properties.ground_flux_ratio
    result.loc[mask, "classification_basis"] = "OSM_MAJOR_ROAD_SIDEWALK_POLICY"
    result.loc[mask, "surface_quality"] = "B"
    result.loc[mask, "major_road_name"] = result.loc[mask, "segment_id"].map(
        lookup["road_names"]
    )
    result.loc[mask, "major_road_class"] = result.loc[mask, "segment_id"].map(
        lookup["road_classes"]
    )
    result.loc[mask, "major_road_support_ratio"] = result.loc[mask, "segment_id"].map(
        lookup["support_ratio"]
    )
    result.loc[mask, "major_road_distance_m"] = result.loc[mask, "segment_id"].map(
        lookup["median_distance_m"]
    )
    result.loc[mask, "major_road_angle_deg"] = result.loc[mask, "segment_id"].map(
        lookup["p90_angle_deg"]
    )
    return result


def _line_axis(geometry: object) -> tuple[float, float, float] | None:
    """Return a straight segment's unit axis and endpoint span for corridor matching."""
    if geometry is None or geometry.is_empty or geometry.geom_type != "LineString":
        return None
    coordinates = list(geometry.coords)
    if len(coordinates) < 2 or float(geometry.length) <= 0:
        return None
    dx = float(coordinates[-1][0] - coordinates[0][0])
    dy = float(coordinates[-1][1] - coordinates[0][1])
    span = math.hypot(dx, dy)
    # Very curved links do not provide a reliable road-axis direction.
    if span <= 0 or span / float(geometry.length) < 0.75:
        return None
    return dx / span, dy / span, span


def _parallel_match_metrics(
    target_geometry: object,
    peer_geometry: object,
) -> tuple[float, float] | None:
    """Return angle difference and target-axis overlap ratio for two line strings."""
    target_axis = _line_axis(target_geometry)
    peer_axis = _line_axis(peer_geometry)
    if target_axis is None or peer_axis is None:
        return None
    ux, uy, target_span = target_axis
    vx, vy, _ = peer_axis
    cosine = min(1.0, max(-1.0, abs(ux * vx + uy * vy)))
    angle_difference = math.degrees(math.acos(cosine))

    def interval(geometry: object) -> tuple[float, float]:
        projections = [float(x) * ux + float(y) * uy for x, y, *_ in geometry.coords]
        return min(projections), max(projections)

    target_min, target_max = interval(target_geometry)
    peer_min, peer_max = interval(peer_geometry)
    overlap_m = max(0.0, min(target_max, peer_max) - max(target_min, peer_min))
    return angle_difference, overlap_m / target_span


def identify_major_road_sidewalk_candidates(
    samples: pd.DataFrame,
    route_geometry: gpd.GeoDataFrame,
    minimum_length_m: float = 20.0,
    minimum_separation_m: float = 14.0,
    maximum_separation_m: float = 40.0,
    maximum_angle_difference_deg: float = 10.0,
    minimum_overlap_ratio: float = 0.45,
    maximum_tdata_seed_distance_m: float = 25.0,
) -> pd.DataFrame:
    """Find likely sidewalks paired across a wide road using conservative geometry tests.

    A candidate must currently be asphalt, be long and reasonably straight, and have a
    non-connected walk link running parallel 14--40 m away.  The peer must overlap at
    least 45% of the target's endpoint span, and the target must be within 25 m of a
    sidewalk already confirmed by T-DATA.  This intentionally leaves uncertain
    fragments unchanged instead of turning every vehicle-accessible walk link into
    pavement.
    """
    columns = [
        "segment_id", "peer_segment_id", "separation_m",
        "angle_difference_deg", "overlap_ratio", "tdata_seed_distance_m",
        "correction_policy",
    ]
    current = samples[["segment_id", "surface_type", "classification_basis"]].drop_duplicates(
        "segment_id"
    )
    routes = route_geometry.merge(current, on="segment_id", how="left", validate="one_to_one")
    routes = routes.set_index("segment_id", drop=False)
    spatial_index = routes.sindex
    seed_routes = routes[routes["classification_basis"].eq("T_DATA_SAFE_SIDEWALK")]
    seed_index = seed_routes.sindex
    records: list[dict[str, object]] = []

    for target in routes.itertuples():
        if (
            str(target.surface_type) != "asphalt"
            or str(target.classification_basis) in {"T_DATA_SAFE_SIDEWALK", "MANUAL_VERIFIED"}
            or float(target.length_m) < minimum_length_m
            or not has_vehicle_access(str(target.link_type_code))
            or _line_axis(target.geometry) is None
        ):
            continue

        # T-DATA에서 안전하게 확인된 보도면 주변으로만 규칙을 확장한다.
        # 평행 링크만으로 판정하면 골목이나 단지 내부 링크까지 과대 보정될 수 있다.
        seed_positions = seed_index.query(
            target.geometry.buffer(maximum_tdata_seed_distance_m), predicate="intersects"
        )
        if len(seed_positions) == 0:
            continue
        seed_distance_m = min(
            float(target.geometry.distance(seed_routes.iloc[int(position)].geometry))
            for position in seed_positions
        )

        best: tuple[float, float, float, int] | None = None
        possible_positions = spatial_index.query(
            target.geometry.buffer(maximum_separation_m), predicate="intersects"
        )
        target_vertices = {int(target.source), int(target.target)}
        for position in possible_positions:
            peer = routes.iloc[int(position)]
            peer_id = int(peer.segment_id)
            # The opposite sidewalk may be split into a shorter fragment and may
            # correctly carry a pedestrian-only link code.  It is evidence for the
            # target, not itself a target of this correction.
            if peer_id == int(target.segment_id) or float(peer.length_m) < 10.0:
                continue
            if target_vertices & {int(peer.source), int(peer.target)}:
                continue
            separation_m = float(target.geometry.distance(peer.geometry))
            if not minimum_separation_m <= separation_m <= maximum_separation_m:
                continue
            metrics = _parallel_match_metrics(target.geometry, peer.geometry)
            if metrics is None:
                continue
            angle_difference, overlap_ratio = metrics
            if (
                angle_difference > maximum_angle_difference_deg
                or overlap_ratio < minimum_overlap_ratio
                or overlap_ratio * _line_axis(target.geometry)[2] < 10.0
            ):
                continue
            score = (overlap_ratio, -angle_difference, -separation_m, peer_id)
            if best is None or score > best:
                best = score

        if best is not None:
            overlap_ratio, negative_angle, negative_separation, peer_id = best
            records.append(
                {
                    "segment_id": int(target.segment_id),
                    "peer_segment_id": peer_id,
                    "separation_m": round(-negative_separation, 3),
                    "angle_difference_deg": round(-negative_angle, 3),
                    "overlap_ratio": round(overlap_ratio, 4),
                    "tdata_seed_distance_m": round(seed_distance_m, 3),
                    # Geometry alone cannot prove the underfoot material. These
                    # pairs stay as QA candidates until independent evidence or
                    # a manual review confirms the sidewalk surface.
                    "correction_policy": "REVIEW_MAJOR_ROAD_CORRIDOR",
                }
            )
    return pd.DataFrame.from_records(records, columns=columns)


def apply_major_road_sidewalk_overrides(
    samples: pd.DataFrame,
    candidates: pd.DataFrame,
) -> pd.DataFrame:
    """Attach corridor evidence without changing the classified material.

    Parallel links can be alleys on opposite sides of a building. The geometry
    result is therefore review-only metadata, not an automatic override.
    """
    result = samples.copy()
    result["secondary_decision"] = pd.Series(pd.NA, index=result.index, dtype="string")
    result["secondary_peer_segment_id"] = pd.Series(pd.NA, index=result.index, dtype="Int64")
    result["secondary_separation_m"] = np.nan
    result["secondary_angle_difference_deg"] = np.nan
    result["secondary_overlap_ratio"] = np.nan
    result["secondary_tdata_seed_distance_m"] = np.nan
    if candidates.empty:
        return result

    lookup = candidates.set_index("segment_id")
    mask = result["segment_id"].isin(lookup.index)
    result.loc[mask, "secondary_decision"] = result.loc[mask, "segment_id"].map(
        lookup["correction_policy"]
    )
    for column, source_column in (
        ("secondary_peer_segment_id", "peer_segment_id"),
        ("secondary_separation_m", "separation_m"),
        ("secondary_angle_difference_deg", "angle_difference_deg"),
        ("secondary_overlap_ratio", "overlap_ratio"),
        ("secondary_tdata_seed_distance_m", "tdata_seed_distance_m"),
    ):
        result.loc[mask, column] = result.loc[mask, "segment_id"].map(lookup[source_column])
    return result


def assign_landcover(samples: pd.DataFrame, polygons: list[LandcoverPolygon], nearest_m: float) -> pd.DataFrame:
    """각 샘플점에 직접 교차, 5m 최근접, 기본값 순으로 피복을 연결한다."""
    geometries = [item.geometry for item in polygons]
    tree = STRtree(geometries)
    records: list[dict[str, object]] = []
    for row in samples.itertuples(index=False):
        point = Point(float(row.x), float(row.y))
        indexes = tree.query(point, predicate="intersects")
        source = "DIRECT"
        distance = 0.0
        if len(indexes):
            # 겹침 도형이 있으면 더 세밀한 작은 폴리곤을 선택한다.
            selected = min((int(index) for index in indexes), key=lambda index: polygons[index].area)
        else:
            nearest_indexes, distances = tree.query_nearest(
                point, max_distance=float(nearest_m), return_distance=True
            )
            if len(np.atleast_1d(nearest_indexes)):
                selected = int(np.atleast_1d(nearest_indexes)[0])
                distance = float(np.atleast_1d(distances)[0])
                source = "NEAREST_5M"
            else:
                selected = -1
                source = "DEFAULT"
                distance = np.nan

        item = polygons[selected] if selected >= 0 else None
        surface, basis, quality = classify_material(
            None if item is None else item.code, str(row.link_type_code)
        )
        if source == "NEAREST_5M" and quality != "D":
            quality = "C"
        properties = SURFACE_PROPERTIES[surface]
        records.append(
            {
                **row._asdict(),
                "landcover_code": None if item is None else item.code,
                "landcover_name": None if item is None else item.name,
                "match_source": source,
                "match_m": distance,
                "classification_basis": basis,
                "surface_type": surface,
                "albedo": properties.albedo,
                "emissivity": properties.emissivity,
                "ground_flux_ratio": properties.ground_flux_ratio,
                "surface_quality": quality,
            }
        )
    return pd.DataFrame.from_records(records)


def aggregate_routes(samples: pd.DataFrame) -> pd.DataFrame:
    """샘플 재질과 물성을 실제 대표 길이로 가중해 링크 값으로 집계한다."""
    records: list[dict[str, object]] = []
    for segment_id, group in samples.groupby("segment_id", sort=True):
        # seq는 원본 생성 순서를 보존하지만 한 링크(168057)는 선 방향과 불일치한다.
        # 링크 길이 적분에는 geometry상의 위치가 기준이므로 chainage로 정렬한다.
        ordered = group.sort_values(["chainage_m", "seq"])
        length_m = float(ordered["length_m"].iloc[0])
        weights = sample_support_weights(ordered["chainage_m"], length_m)
        qualities = ordered["surface_quality"].astype(str).tolist()
        worst_quality = max(qualities, key=lambda value: QUALITY_RANK[value])
        records.append(
            {
                "segment_id": int(segment_id),
                "surface_type": weighted_mode(ordered["surface_type"].astype(str), weights),
                "albedo": round(weighted_mean(ordered["albedo"], weights), 3),
                "emissivity": round(weighted_mean(ordered["emissivity"], weights), 3),
                "ground_flux_ratio": round(weighted_mean(ordered["ground_flux_ratio"], weights), 3),
                "surface_quality": worst_quality,
                "sample_count": len(ordered),
                "weighted_length_m": round(float(weights.sum()), 3),
                "simple_albedo": round(float(ordered["albedo"].mean()), 3),
            }
        )
    result = pd.DataFrame.from_records(records)
    ensure_expected_count(len(result), EXPECTED_SEGMENT_COUNT, "집계 링크")
    return result


def validate_results(samples: pd.DataFrame, routes: pd.DataFrame) -> None:
    """허용 재질, 물성 범위, ID 유일성, 누락과 가중 길이를 검사한다."""
    ensure_expected_count(len(samples), EXPECTED_SAMPLE_COUNT, "분류 샘플")
    ensure_expected_count(len(routes), EXPECTED_SEGMENT_COUNT, "분류 링크")
    if not samples["sample_id"].is_unique or not routes["segment_id"].is_unique:
        raise ValueError("샘플 또는 링크 ID가 중복되었습니다.")
    required = ["surface_type", "albedo", "emissivity", "ground_flux_ratio", "surface_quality"]
    if samples[required].isna().any().any() or routes[required].isna().any().any():
        raise ValueError("재질 결과에 NULL이 있습니다.")
    if not samples["surface_type"].isin(SURFACE_PROPERTIES).all():
        raise ValueError("지원하지 않는 재질이 있습니다.")
    if not samples["albedo"].between(0, 1).all() or not samples["emissivity"].between(0, 1).all():
        raise ValueError("알베도/방사율 범위를 벗어났습니다.")
    if not np.allclose(routes["weighted_length_m"], routes.merge(
        samples[["segment_id", "length_m"]].drop_duplicates(), on="segment_id"
    )["length_m"], atol=0.01):
        raise ValueError("링크 가중 길이가 원래 길이를 보존하지 못했습니다.")


def write_outputs(samples: pd.DataFrame, routes: pd.DataFrame, route_geometry: gpd.GeoDataFrame,
                  sample_path: Path, route_path: Path, qa_path: Path, overwrite: bool,
                  secondary_candidates: pd.DataFrame | None = None,
                  major_road_candidates: pd.DataFrame | None = None) -> None:
    """DB 반영 전 검토용 CSV와 QGIS GeoPackage를 생성한다."""
    for path in (sample_path, route_path, qa_path):
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and not overwrite:
            raise FileExistsError(f"기존 산출물이 있습니다. --overwrite를 사용하세요: {path}")
        if path.exists():
            path.unlink()
    samples.to_csv(sample_path, index=False, encoding="utf-8-sig")
    routes.to_csv(route_path, index=False, encoding="utf-8-sig")
    sample_gdf = gpd.GeoDataFrame(
        samples.drop(columns=["x", "y"]),
        geometry=gpd.points_from_xy(samples["x"], samples["y"], crs="EPSG:5186"),
        crs="EPSG:5186",
    )
    route_gdf = route_geometry[["segment_id", "geometry"]].merge(routes, on="segment_id")
    sample_gdf.to_file(qa_path, layer="sample_surface", driver="GPKG")
    route_gdf.to_file(qa_path, layer="route_surface", driver="GPKG", mode="a")
    if secondary_candidates is not None and not secondary_candidates.empty:
        candidate_gdf = route_geometry[["segment_id", "geometry"]].merge(
            secondary_candidates, on="segment_id", how="inner", validate="one_to_one"
        )
        candidate_gdf.to_file(
            qa_path, layer="secondary_major_road_candidates", driver="GPKG", mode="a"
        )
    if major_road_candidates is not None and not major_road_candidates.empty:
        major_road_gdf = route_geometry[["segment_id", "geometry"]].merge(
            major_road_candidates,
            on="segment_id",
            how="inner",
            validate="one_to_one",
        )
        major_road_gdf.to_file(
            qa_path,
            layer="major_road_pavement",
            driver="GPKG",
            mode="a",
        )


def write_report(samples: pd.DataFrame, routes: pd.DataFrame, path: Path) -> None:
    """분류 커버리지와 가정 민감도를 Markdown 보고서에 기록한다."""
    source_counts = samples["match_source"].value_counts().to_dict()
    material_counts = samples["surface_type"].value_counts().to_dict()
    quality_counts = routes["surface_quality"].value_counts().to_dict()
    ambiguous_count = int((samples["classification_basis"] == "LANDCOVER_LINKTYPE").sum())
    mean_difference = float((routes["albedo"] - routes["simple_albedo"]).abs().mean())
    max_difference = float((routes["albedo"] - routes["simple_albedo"]).abs().max())
    lines = [
        "# D4-3·D4-4 노면 재질 및 물성 QA", "",
        "## 분류 정책", "",
        "- 환경부 세분류의 자연 피복은 grass/soil로 직접 판정한다.",
        "- 도로·시가화 피복은 재질도가 아니므로 링크 유형의 차량 통행 비트를 결합한다.",
        "- 차량 통행 링크는 asphalt, 보행·자전거 전용 링크는 pavement로 추정한다.",
        "- 5m 최근접도 없을 때만 asphalt 기본값을 사용한다.",
        "- 링크 물성은 10m 점 단순평균이 아니라 각 점의 실제 대표 길이로 가중한다.", "",
        "## 커버리지", "",
        f"- 샘플: {len(samples):,}", f"- 링크: {len(routes):,}",
        f"- 직접 교차: {source_counts.get('DIRECT', 0):,}",
        f"- 5m 최근접: {source_counts.get('NEAREST_5M', 0):,}",
        f"- 기본값: {source_counts.get('DEFAULT', 0):,}",
        f"- 도시/도로 피복으로 링크유형 보완: {ambiguous_count:,}", "",
        "## 샘플 재질 분포", "",
    ]
    lines.extend(f"- {key}: {value:,}" for key, value in sorted(material_counts.items()))
    lines += ["", "## 링크 품질 분포", ""]
    lines.extend(f"- {key}: {value:,}" for key, value in sorted(quality_counts.items()))
    lines += [
        "", "## 집계식 재검토", "",
        f"- 길이가중 알베도와 단순평균의 평균 절대차: {mean_difference:.6f}",
        f"- 길이가중 알베도와 단순평균의 최대 절대차: {max_difference:.6f}",
        "- 토지피복 154(도로)는 차도/보도를 직접 구분하지 못하므로 결과를 실측과 D5 민감도 분석에서 재검증한다.",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_surface_policy_report(
    samples: pd.DataFrame,
    routes: pd.DataFrame,
    path: Path,
    secondary_candidates: pd.DataFrame | None = None,
) -> None:
    """Write the revised classification policy and its QA statistics."""
    source_counts = samples["match_source"].value_counts().to_dict()
    material_counts = samples["surface_type"].value_counts().to_dict()
    quality_counts = routes["surface_quality"].value_counts().to_dict()
    basis_counts = samples["classification_basis"].value_counts().to_dict()
    tdata_samples = int(basis_counts.get("T_DATA_SAFE_SIDEWALK", 0))
    tdata_segments = int(
        samples.loc[
            samples["classification_basis"].eq("T_DATA_SAFE_SIDEWALK"), "segment_id"
        ].nunique()
    )
    secondary_segments = 0 if secondary_candidates is None else len(secondary_candidates)
    mean_difference = float((routes["albedo"] - routes["simple_albedo"]).abs().mean())
    max_difference = float((routes["albedo"] - routes["simple_albedo"]).abs().max())
    lines = [
        "# D4-3·D4-4 노면 재질 및 물성 QA", "",
        "## 수정된 분류 정책", "",
        "- 토지피복도는 주변 환경의 맥락이며 실제 발밑 재질로 직접 확정하지 않는다.",
        "- 차량 통행 링크는 asphalt, 보행 전용 링크는 pavement를 기본 통행 표면으로 판정한다.",
        "- 자연피복과 겹친 링크는 경계 오차 가능성이 있으므로 C등급으로 낮춘다.",
        "- T-DATA 보도면과 안전하게 교차한 AUTO_PAVEMENT 링크만 pavement로 보정한다.",
        "- 일부만 교차한 REVIEW_PARTIAL 링크는 골목 오분류를 막기 위해 자동 보정하지 않는다.",
        "- 2차 분류는 T-DATA 보도면 주변의 평행 링크쌍을 REVIEW 후보로만 기록한다.",
        "- 평행 구조만으로 발밑 재질을 확정하지 않으며, OSM 명시 보도 재질 또는 수동 QA가 있어야 pavement 보정 후보가 된다.",
        "- 현장에서 확인한 비포장 구간만 config/surface_overrides.csv로 grass 또는 soil로 교정한다.",
        "- 링크 물성은 각 샘플의 실제 대표 길이를 이용해 길이가중 집계한다.", "",
        "## 커버리지", "",
        f"- 샘플: {len(samples):,}",
        f"- 링크: {len(routes):,}",
        f"- 토지피복 직접 교차: {source_counts.get('DIRECT', 0):,}",
        f"- 5m 최근접: {source_counts.get('NEAREST_5M', 0):,}",
        f"- 기본값: {source_counts.get('DEFAULT', 0):,}",
        f"- 자연피복 경계와 겹쳐 링크 유형으로 판정: {basis_counts.get('LANDCOVER_CONTEXT_LINKTYPE', 0):,}",
        f"- T-DATA 안전 보도면 자동 보정: {tdata_samples:,}개 샘플 / {tdata_segments:,}개 링크",
        f"- 넓은 도로 평행 링크 REVIEW 후보: {secondary_segments:,}개 링크 (자동 보정 0개)",
        f"- 현장 확인 수동 교정: {basis_counts.get('MANUAL_VERIFIED', 0):,}", "",
        "## 샘플 재질 분포", "",
    ]
    lines.extend(f"- {key}: {value:,}" for key, value in sorted(material_counts.items()))
    lines += ["", "## 링크 품질 분포", ""]
    lines.extend(f"- {key}: {value:,}" for key, value in sorted(quality_counts.items()))
    lines += [
        "", "## 집계 검증", "",
        f"- 길이가중 알베도와 단순평균의 평균 절대차: {mean_difference:.6f}",
        f"- 길이가중 알베도와 단순평균의 최대 절대차: {max_difference:.6f}",
        "- sample_id 1237 / segment_id 10940은 기타초지 경계와 겹치지만 현장상 보도블록이므로 pavement·C등급이다.",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    """D4-3/4 파일 기반 계산을 실행한다."""
    args = parse_arguments()
    samples, route_geometry = fetch_samples()
    bounds = (
        float(samples["x"].min() - args.nearest_m), float(samples["y"].min() - args.nearest_m),
        float(samples["x"].max() + args.nearest_m), float(samples["y"].max() + args.nearest_m),
    )
    polygons = load_landcover_polygons(args.landcover_dir.resolve(), bounds)
    classified = assign_landcover(samples, polygons, args.nearest_m)
    classified = apply_tdata_surface_overrides(
        classified,
        load_tdata_surface_overrides(args.tdata_segment_candidates.resolve()),
    )
    major_road_candidates = load_major_road_pavement_candidates(
        args.major_road_pavement_candidates.resolve()
    )
    classified = apply_major_road_pavement_overrides(
        classified, major_road_candidates
    )
    secondary_candidates = identify_major_road_sidewalk_candidates(
        classified, route_geometry
    )
    classified = apply_major_road_sidewalk_overrides(
        classified, secondary_candidates
    )
    # 현장 확인 수동 보정은 T-DATA 자동 보정보다 우선한다.
    classified = apply_surface_overrides(
        classified, load_surface_overrides(args.overrides.resolve())
    )
    routes = aggregate_routes(classified)
    validate_results(classified, routes)
    secondary_output = args.secondary_candidates_output.resolve()
    secondary_output.parent.mkdir(parents=True, exist_ok=True)
    if secondary_output.exists() and not args.overwrite:
        raise FileExistsError(
            f"기존 산출물이 있습니다. --overwrite를 사용하세요: {secondary_output}"
        )
    secondary_candidates.to_csv(secondary_output, index=False, encoding="utf-8-sig")
    write_outputs(classified, routes, route_geometry, args.sample_output.resolve(),
                  args.route_output.resolve(), args.qa_gpkg.resolve(), args.overwrite,
                  secondary_candidates, major_road_candidates)
    write_surface_policy_report(
        classified,
        routes,
        args.report.resolve(),
        secondary_candidates,
    )
    print(f"[완료] 샘플 {len(classified):,}, 링크 {len(routes):,}")
    print(f"[QA] {args.qa_gpkg.resolve()}")
    print(f"[2차 분류] {len(secondary_candidates):,}개 링크: {secondary_output}")
    print("[안내] DB는 수정하지 않았습니다.")


if __name__ == "__main__":
    main()
