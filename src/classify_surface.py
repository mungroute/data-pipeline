from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass
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

# 환경부 세분류는 노면 재질도가 아니다. 자연 피복만 직접 재질로 보고,
# 시가화·교통 피복은 보행 링크의 차량 통행 가능 여부로 포장 종류를 보완한다.
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
            SELECT r.segment_id, r.length_m, s.link_type_code,
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
    if code in GRASS_CODES:
        return "grass", "LANDCOVER_NATURAL", "A"
    if code in SOIL_CODES:
        return "soil", "LANDCOVER_NATURAL", "A"
    if code in AMBIGUOUS_URBAN_CODES:
        if has_vehicle_access(link_type_code):
            return "asphalt", "LANDCOVER_LINKTYPE", "B"
        return "pavement", "LANDCOVER_LINKTYPE", "B"
    return "asphalt", "DEFAULT_ASPHALT", "D"


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
                  sample_path: Path, route_path: Path, qa_path: Path, overwrite: bool) -> None:
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
    routes = aggregate_routes(classified)
    validate_results(classified, routes)
    write_outputs(classified, routes, route_geometry, args.sample_output.resolve(),
                  args.route_output.resolve(), args.qa_gpkg.resolve(), args.overwrite)
    write_report(classified, routes, args.report.resolve())
    print(f"[완료] 샘플 {len(classified):,}, 링크 {len(routes):,}")
    print(f"[QA] {args.qa_gpkg.resolve()}")
    print("[안내] DB는 수정하지 않았습니다.")


if __name__ == "__main__":
    main()
