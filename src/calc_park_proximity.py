from __future__ import annotations

import argparse
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import psycopg2
from shapely import from_wkb
from shapely.geometry import Point, box
from shapely.strtree import STRtree

from d4_common import (
    D4_OUTPUT_DIR,
    D4_QA_DIR,
    EXPECTED_SAMPLE_COUNT,
    EXPECTED_SEGMENT_COUNT,
    PIPELINE_ROOT,
    ensure_expected_count,
    sample_support_weights,
    weighted_mean,
)
from load_segments import build_db_config


DEFAULT_PARK_PATH = next((PIPELINE_ROOT / "data" / "raw" / "park").rglob("UPIS_C_UQ153.shp"))
DEFAULT_SAMPLE_OUTPUT = D4_OUTPUT_DIR / "d4_sample_park.csv"
DEFAULT_ROUTE_OUTPUT = D4_OUTPUT_DIR / "d4_route_park.csv"
DEFAULT_QA_OUTPUT = D4_QA_DIR / "d4_park_qa.gpkg"
DEFAULT_REPORT = PIPELINE_ROOT / "reports" / "d4_park_qa.md"


def parse_arguments() -> argparse.Namespace:
    """D4-5 파일 기반 계산 옵션을 읽는다."""
    parser = argparse.ArgumentParser(description="D4-5 샘플·링크별 최근접 공원 거리 계산")
    parser.add_argument("--park", type=Path, default=DEFAULT_PARK_PATH)
    parser.add_argument("--sample-output", type=Path, default=DEFAULT_SAMPLE_OUTPUT)
    parser.add_argument("--route-output", type=Path, default=DEFAULT_ROUTE_OUTPUT)
    parser.add_argument("--qa-gpkg", type=Path, default=DEFAULT_QA_OUTPUT)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--overwrite", action="store_true")
    return parser.parse_args()


def fetch_samples() -> tuple[pd.DataFrame, gpd.GeoDataFrame]:
    """DB를 읽기 전용으로 조회해 샘플 위치와 링크 도형을 반환한다."""
    with psycopg2.connect(**build_db_config()) as connection:
        samples = pd.read_sql_query(
            """
            SELECT p.sample_id, p.segment_id, p.seq,
                   ST_X(p.geom) AS x, ST_Y(p.geom) AS y,
                   ST_LineLocatePoint(r.geom, p.geom) * r.length_m AS chainage_m,
                   r.length_m
            FROM segment_sample_point p
            JOIN route_segment r USING (segment_id)
            ORDER BY p.segment_id, p.seq
            """,
            connection,
        )
        routes = pd.read_sql_query(
            "SELECT segment_id, ST_AsBinary(geom) AS geom_wkb FROM route_segment ORDER BY segment_id",
            connection,
        )
    ensure_expected_count(len(samples), EXPECTED_SAMPLE_COUNT, "공원거리 샘플")
    ensure_expected_count(len(routes), EXPECTED_SEGMENT_COUNT, "공원거리 링크")
    routes["geometry"] = routes["geom_wkb"].map(lambda value: from_wkb(bytes(value)))
    return samples, gpd.GeoDataFrame(routes.drop(columns="geom_wkb"), geometry="geometry", crs="EPSG:5186")


def load_parks(path: Path) -> gpd.GeoDataFrame:
    """도시계획시설 중 UQT2xx 공원만 선별하고 EPSG:5186으로 변환한다."""
    if not path.is_file():
        raise FileNotFoundError(f"공원 SHP가 없습니다: {path}")
    parks = gpd.read_file(path, encoding="cp949")
    if parks.crs is None:
        raise ValueError("공원 데이터 CRS가 없습니다.")
    parks["ATRB_SE"] = parks["ATRB_SE"].fillna("").astype(str).str.strip()
    parks = parks[parks["ATRB_SE"].str.startswith("UQT2")].copy()
    if parks.empty:
        raise ValueError("UQT2xx 공원 도형이 없습니다.")
    parks = parks.to_crs(epsg=5186)
    parks["geometry"] = parks.geometry.make_valid()
    parks = parks[~parks.geometry.is_empty & parks.geometry.notna()].copy()
    return parks[["ATRB_SE", "DGM_NM", "geometry"]].reset_index(drop=True)


def calculate_distances(samples: pd.DataFrame, parks: gpd.GeoDataFrame) -> pd.DataFrame:
    """공원 폴리곤까지의 최단거리를 계산한다. 공원 내부 점은 0m다."""
    geometries = list(parks.geometry)
    tree = STRtree(geometries)
    records: list[dict[str, object]] = []
    for row in samples.itertuples(index=False):
        point = Point(float(row.x), float(row.y))
        indexes, distances = tree.query_nearest(point, return_distance=True)
        indexes = np.atleast_1d(indexes)
        distances = np.atleast_1d(distances)
        if len(indexes) == 0:
            raise ValueError(f"최근접 공원을 찾지 못했습니다: sample_id={row.sample_id}")
        minimum_position = int(np.argmin(distances))
        park_index = int(indexes[minimum_position])
        distance = max(0.0, float(distances[minimum_position]))
        park = parks.iloc[park_index]
        boundary_distance = float(point.distance(park.geometry.boundary))
        records.append(
            {
                **row._asdict(),
                "park_proximity_m": distance,
                "park_boundary_m": boundary_distance,
                "park_code": str(park["ATRB_SE"]),
                "park_name": str(park["DGM_NM"] or ""),
                "inside_park": distance <= 1e-8,
            }
        )
    return pd.DataFrame.from_records(records)


def aggregate_routes(samples: pd.DataFrame) -> pd.DataFrame:
    """샘플 공원거리를 실제 대표 길이로 가중해 링크 평균으로 만든다."""
    records: list[dict[str, object]] = []
    for segment_id, group in samples.groupby("segment_id", sort=True):
        # 링크 위 실제 위치 순서로 적분한다. seq 방향 불일치가 있어도 결과가 안정적이다.
        ordered = group.sort_values(["chainage_m", "seq"])
        length_m = float(ordered["length_m"].iloc[0])
        weights = sample_support_weights(ordered["chainage_m"], length_m)
        values = ordered["park_proximity_m"].astype(float)
        records.append(
            {
                "segment_id": int(segment_id),
                "park_proximity_m": round(weighted_mean(values, weights), 1),
                "park_min_m": round(float(values.min()), 1),
                "park_max_m": round(float(values.max()), 1),
                "simple_park_m": round(float(values.mean()), 1),
                "sample_count": len(ordered),
                "weighted_length_m": round(float(weights.sum()), 3),
            }
        )
    result = pd.DataFrame.from_records(records)
    ensure_expected_count(len(result), EXPECTED_SEGMENT_COUNT, "공원거리 집계 링크")
    return result


def validate_results(samples: pd.DataFrame, routes: pd.DataFrame, parks: gpd.GeoDataFrame) -> None:
    """거리 범위, 공원 내부 0m 규칙, 누락과 행 수를 검증한다."""
    ensure_expected_count(len(samples), EXPECTED_SAMPLE_COUNT, "공원거리 결과 샘플")
    ensure_expected_count(len(routes), EXPECTED_SEGMENT_COUNT, "공원거리 결과 링크")
    if samples["park_proximity_m"].isna().any() or routes["park_proximity_m"].isna().any():
        raise ValueError("공원거리에 NULL이 있습니다.")
    if (samples["park_proximity_m"] < 0).any() or (routes["park_proximity_m"] < 0).any():
        raise ValueError("음수 공원거리가 있습니다.")
    # 폴리곤 내부를 경계선 거리로 잘못 계산하는 회귀를 실제 결과에서도 차단한다.
    inside = samples[samples["inside_park"]]
    if not inside.empty and not np.allclose(inside["park_proximity_m"], 0.0):
        raise ValueError("공원 내부 샘플의 거리가 0m가 아닙니다.")
    if parks.empty:
        raise ValueError("공원 입력이 비었습니다.")


def write_outputs(samples: pd.DataFrame, routes: pd.DataFrame, parks: gpd.GeoDataFrame,
                  route_geometry: gpd.GeoDataFrame, sample_path: Path, route_path: Path,
                  qa_path: Path, overwrite: bool) -> None:
    """CSV와 QGIS 검토용 GeoPackage를 생성한다."""
    for path in (sample_path, route_path, qa_path):
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and not overwrite:
            raise FileExistsError(f"기존 산출물이 있습니다: {path}")
        if path.exists():
            path.unlink()
    samples.to_csv(sample_path, index=False, encoding="utf-8-sig")
    routes.to_csv(route_path, index=False, encoding="utf-8-sig")
    sample_gdf = gpd.GeoDataFrame(
        samples.drop(columns=["x", "y"]),
        geometry=gpd.points_from_xy(samples["x"], samples["y"], crs="EPSG:5186"),
        crs="EPSG:5186",
    )
    route_gdf = route_geometry.merge(routes, on="segment_id")
    sample_bounds = box(*sample_gdf.total_bounds).buffer(500.0)
    nearby_parks = parks[parks.intersects(sample_bounds)].copy()
    sample_gdf.to_file(qa_path, layer="sample_park_distance", driver="GPKG")
    route_gdf.to_file(qa_path, layer="route_park_distance", driver="GPKG", mode="a")
    nearby_parks.to_file(qa_path, layer="park_uqt2", driver="GPKG", mode="a")


def write_report(samples: pd.DataFrame, routes: pd.DataFrame, parks: gpd.GeoDataFrame, path: Path) -> None:
    """거리 분포와 집계식 비교를 기록한다."""
    differences = (routes["park_proximity_m"] - routes["simple_park_m"]).abs()
    quantiles = samples["park_proximity_m"].quantile([0, .25, .5, .75, .9, 1]).to_dict()
    inside = samples[samples["inside_park"]]
    lines = [
        "# D4-5 공원 거리 QA", "", "## 계산 정책", "",
        "- 도시계획시설 중 `ATRB_SE LIKE 'UQT2%'`인 공원만 사용한다.",
        "- UQT3xx 녹지와 UQT5xx 공공공지는 공원 정의에서 제외한다.",
        "- 공원 경계선이 아니라 공원 폴리곤까지 거리를 재므로 내부 샘플은 0m다.",
        "- 중구로 도형을 잘라내지 않아 행정경계 바깥 인접 공원도 거리 후보에 포함한다.",
        "- 링크 값은 각 10m 샘플의 실제 대표 길이로 가중한다.", "",
        "## 결과", "",
        f"- 공원 도형: {len(parks):,}", f"- 샘플: {len(samples):,}",
        f"- 링크: {len(routes):,}", f"- 공원 내부 샘플: {int(samples['inside_park'].sum()):,}",
        f"- 최소/중앙/평균/최대: {quantiles[0]:.1f} / {quantiles[.5]:.1f} / {samples['park_proximity_m'].mean():.1f} / {quantiles[1]:.1f}m",
        f"- 공원 내부를 경계선식으로 잘못 계산할 때 평균/최대: {inside['park_boundary_m'].mean():.1f} / {inside['park_boundary_m'].max():.1f}m",
        "", "## 집계식 재검토", "",
        f"- 길이가중 평균과 단순평균의 평균 절대차: {differences.mean():.3f}m",
        f"- 길이가중 평균과 단순평균의 최대 절대차: {differences.max():.3f}m",
        "- D5의 200m 냉각 보정은 별도 모델 단계이며 D4에서는 관측 가능한 거리만 저장한다.",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    """D4-5 파일 기반 공원 거리 계산을 실행한다."""
    args = parse_arguments()
    samples, route_geometry = fetch_samples()
    parks = load_parks(args.park.resolve())
    sample_results = calculate_distances(samples, parks)
    route_results = aggregate_routes(sample_results)
    validate_results(sample_results, route_results, parks)
    write_outputs(sample_results, route_results, parks, route_geometry,
                  args.sample_output.resolve(), args.route_output.resolve(),
                  args.qa_gpkg.resolve(), args.overwrite)
    write_report(sample_results, route_results, parks, args.report.resolve())
    print(f"[완료] 공원 {len(parks):,}, 샘플 {len(sample_results):,}, 링크 {len(route_results):,}")
    print(f"[QA] {args.qa_gpkg.resolve()}")
    print("[안내] DB는 수정하지 않았습니다.")


if __name__ == "__main__":
    main()
