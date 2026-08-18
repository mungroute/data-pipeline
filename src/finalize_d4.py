from __future__ import annotations

import argparse
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import psycopg2
from psycopg2.extras import execute_values
from shapely import from_wkb

from classify_surface import aggregate_routes as aggregate_surface_routes
from d4_common import (
    D4_OUTPUT_DIR,
    D4_QA_DIR,
    EXPECTED_SAMPLE_COUNT,
    EXPECTED_SEGMENT_COUNT,
    PIPELINE_ROOT,
    SURFACE_PROPERTIES,
    ensure_expected_count,
)
from db_config import add_target_argument, build_db_config, describe_db_target


DEFAULT_SAMPLE_SURFACE = D4_OUTPUT_DIR / "d4_sample_surface.csv"
DEFAULT_ROUTE_SURFACE = D4_OUTPUT_DIR / "d4_route_surface.csv"
DEFAULT_SAMPLE_PARK = D4_OUTPUT_DIR / "d4_sample_park.csv"
DEFAULT_ROUTE_PARK = D4_OUTPUT_DIR / "d4_route_park.csv"
DEFAULT_QA_OUTPUT = D4_QA_DIR / "d4_integrated_qa.gpkg"
DEFAULT_REPORT = PIPELINE_ROOT / "reports" / "d4_integrated_qa.md"


def parse_arguments() -> argparse.Namespace:
    """통합 QA와 선택적 DB 반영 옵션을 읽는다."""
    parser = argparse.ArgumentParser(description="D4-6 통합 QA, 품질등급, DB 반영")
    parser.add_argument("--sample-surface", type=Path, default=DEFAULT_SAMPLE_SURFACE)
    parser.add_argument("--route-surface", type=Path, default=DEFAULT_ROUTE_SURFACE)
    parser.add_argument("--sample-park", type=Path, default=DEFAULT_SAMPLE_PARK)
    parser.add_argument("--route-park", type=Path, default=DEFAULT_ROUTE_PARK)
    parser.add_argument("--qa-gpkg", type=Path, default=DEFAULT_QA_OUTPUT)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--apply", action="store_true", help="모든 QA 통과 후 한 트랜잭션으로 DB에 반영")
    add_target_argument(parser)
    return parser.parse_args()


def load_csv(path: Path, label: str) -> pd.DataFrame:
    """선행 단계 CSV가 존재하는지 확인하고 읽는다."""
    if not path.is_file():
        raise FileNotFoundError(f"{label} 파일이 없습니다: {path}")
    return pd.read_csv(path, encoding="utf-8-sig")


def fetch_existing(
    db_config: dict[str, str | int],
) -> tuple[pd.DataFrame, gpd.GeoDataFrame]:
    """SVF·그림자와 QA 도형을 DB에서 읽기 전용으로 가져온다."""
    with psycopg2.connect(**db_config) as connection:
        sample_sql = """
            SELECT sample_id, segment_id, seq, svf,
                   is_shaded_09, is_shaded_12, is_shaded_15, is_shaded_18,
                   shade_src_09, shade_src_12, shade_src_15, shade_src_18,
                   ST_X(geom) AS x, ST_Y(geom) AS y
            FROM segment_sample_point ORDER BY sample_id
        """
        samples = pd.read_sql_query(sample_sql, connection)
        route_sql = """
            SELECT segment_id, length_m, svf,
                   tree_shade_ratio_09, tree_shade_ratio_12,
                   tree_shade_ratio_15, tree_shade_ratio_18,
                   bldg_shade_ratio_09, bldg_shade_ratio_12,
                   bldg_shade_ratio_15, bldg_shade_ratio_18,
                   shade_ratio_09, shade_ratio_12, shade_ratio_15, shade_ratio_18,
                   ST_AsBinary(geom) AS geom_wkb
            FROM route_segment ORDER BY segment_id
        """
        routes = pd.read_sql_query(route_sql, connection)
    ensure_expected_count(len(samples), EXPECTED_SAMPLE_COUNT, "통합 샘플")
    ensure_expected_count(len(routes), EXPECTED_SEGMENT_COUNT, "통합 링크")
    routes["geometry"] = routes["geom_wkb"].map(lambda value: from_wkb(bytes(value)))
    route_gdf = gpd.GeoDataFrame(routes.drop(columns="geom_wkb"), geometry="geometry", crs="EPSG:5186")
    return samples, route_gdf


def shade_grade(row: pd.Series) -> str:
    """SVF와 네 시간대 그림자가 모두 실계산됐을 때 A, 아니면 D로 구분한다."""
    fields = ["svf_original", "shade_ratio_09", "shade_ratio_12", "shade_ratio_15", "shade_ratio_18"]
    return "A" if all(pd.notna(row[field]) for field in fields) else "D"


def temperature_grade(surface_quality: str, current_shade_grade: str, park_distance: float) -> str:
    """온도 입력의 최저 품질을 링크 temp_grade로 전파한다."""
    if current_shade_grade == "D" or pd.isna(park_distance):
        return "D"
    return {"A": "A", "B": "B", "C": "C", "D": "D"}.get(surface_quality, "D")


def build_integrated(
    db_config: dict[str, str | int] | None = None,
) -> tuple[pd.DataFrame, gpd.GeoDataFrame, dict[str, float]]:
    """D3/D4 결과를 ID로 결합하고 대체값·등급·민감도 지표를 계산한다."""
    if db_config is None:
        # 기존 계산 스크립트 호환성과 local 기본 정책을 유지한다.
        db_config = build_db_config("local")
    sample_surface = load_csv(DEFAULT_SAMPLE_SURFACE, "샘플 재질")
    route_surface = load_csv(DEFAULT_ROUTE_SURFACE, "링크 재질")
    sample_park = load_csv(DEFAULT_SAMPLE_PARK, "샘플 공원")
    route_park = load_csv(DEFAULT_ROUTE_PARK, "링크 공원")
    existing_samples, existing_routes = fetch_existing(db_config)

    sample_columns = [
        "sample_id", "surface_type", "albedo", "emissivity", "ground_flux_ratio",
        "surface_quality", "landcover_code", "landcover_name", "match_source",
        "classification_basis", "link_type_code", "chainage_m", "length_m",
    ]
    samples = existing_samples.merge(sample_surface[sample_columns], on="sample_id", validate="one_to_one")
    samples = samples.merge(
        sample_park[["sample_id", "park_proximity_m", "park_code", "park_name", "inside_park"]],
        on="sample_id", validate="one_to_one",
    )
    samples = samples.rename(columns={"svf": "svf_original"})
    samples["svf_fallback"] = samples["svf_original"].isna()
    samples["svf_effective"] = samples["svf_original"].fillna(1.0)

    route_columns = [
        "segment_id", "surface_type", "albedo", "emissivity", "ground_flux_ratio",
        "surface_quality", "sample_count", "simple_albedo",
    ]
    routes = existing_routes.merge(route_surface[route_columns], on="segment_id", validate="one_to_one")
    routes = routes.merge(
        route_park[["segment_id", "park_proximity_m", "park_min_m", "park_max_m", "simple_park_m"]],
        on="segment_id", validate="one_to_one",
    )
    routes = routes.rename(columns={"svf": "svf_original"})
    routes["svf_fallback"] = routes["svf_original"].isna()
    routes["svf_effective"] = routes["svf_original"].fillna(1.0)
    routes["shade_grade_new"] = routes.apply(shade_grade, axis=1)
    routes["temp_grade_new"] = routes.apply(
        lambda row: temperature_grade(
            str(row["surface_quality"]), str(row["shade_grade_new"]), float(row["park_proximity_m"])
        ), axis=1,
    )

    # 토지피복도는 실제 발밑 재질도가 아니므로 도시피복뿐 아니라
    # 자연피복 경계와 겹친 링크까지 asphalt/pavement 가정 민감도에 포함한다.
    ambiguous = sample_surface["classification_basis"].isin(
        ["LANDCOVER_LINKTYPE", "LANDCOVER_CONTEXT_LINKTYPE"]
    )
    all_asphalt = sample_surface.copy()
    all_pavement = sample_surface.copy()
    for frame, material in ((all_asphalt, "asphalt"), (all_pavement, "pavement")):
        prop = SURFACE_PROPERTIES[material]
        frame.loc[ambiguous, ["surface_type", "albedo", "emissivity", "ground_flux_ratio"]] = [
            material, prop.albedo, prop.emissivity, prop.ground_flux_ratio
        ]
    asphalt_routes = aggregate_surface_routes(all_asphalt)
    pavement_routes = aggregate_surface_routes(all_pavement)
    sensitivity = {
        "ambiguous_samples": float(ambiguous.sum()),
        "baseline_mean_albedo": float(routes["albedo"].mean()),
        "all_asphalt_mean_albedo": float(asphalt_routes["albedo"].mean()),
        "all_pavement_mean_albedo": float(pavement_routes["albedo"].mean()),
        "svf_sample_fallback": float(samples["svf_fallback"].sum()),
        "svf_route_fallback": float(routes["svf_fallback"].sum()),
    }
    return samples, routes, sensitivity


def validate_integrated(samples: pd.DataFrame, routes: pd.DataFrame) -> None:
    """DB 반영 전 모든 완결조건과 범위·등급 규칙을 강제한다."""
    ensure_expected_count(len(samples), EXPECTED_SAMPLE_COUNT, "통합 결과 샘플")
    ensure_expected_count(len(routes), EXPECTED_SEGMENT_COUNT, "통합 결과 링크")
    if not samples["sample_id"].is_unique or not routes["segment_id"].is_unique:
        raise ValueError("통합 결과 ID가 중복되었습니다.")
    sample_required = ["svf_effective", "albedo", "emissivity", "ground_flux_ratio", "park_proximity_m"]
    route_required = sample_required + ["surface_type", "shade_grade_new", "temp_grade_new"]
    if samples[sample_required].isna().any().any() or routes[route_required].isna().any().any():
        raise ValueError("D4 필수 결과에 NULL이 있습니다.")
    for frame in (samples, routes):
        if not frame["svf_effective"].between(0, 1).all():
            raise ValueError("SVF 범위를 벗어난 값이 있습니다.")
        if not frame["albedo"].between(0, 1).all() or not frame["emissivity"].between(0, 1).all():
            raise ValueError("물성 범위를 벗어난 값이 있습니다.")
        if (frame["park_proximity_m"] < 0).any():
            raise ValueError("공원 거리에 음수가 있습니다.")
    if not routes["shade_grade_new"].isin(list("ABCD")).all() or not routes["temp_grade_new"].isin(list("ABCD")).all():
        raise ValueError("잘못된 품질등급이 있습니다.")
    if not np.allclose(samples.loc[samples["svf_fallback"], "svf_effective"], 1.0):
        raise ValueError("SVF 개활지 대체값이 1.0이 아닙니다.")
    if not (routes.loc[routes["svf_fallback"], "temp_grade_new"] == "D").all():
        raise ValueError("SVF 대체 링크가 온도 D등급이 아닙니다.")


def add_qa_reasons(routes: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """시각 검토 우선순위가 높은 링크만 별도 후보 레이어로 만든다."""
    reasons: list[str] = []
    for row in routes.itertuples(index=False):
        current: list[str] = []
        if bool(row.svf_fallback):
            current.append("SVF_OPEN_SKY_FALLBACK")
        if str(row.surface_quality) in {"C", "D"}:
            current.append("LOW_SURFACE_QUALITY")
        if float(row.park_proximity_m) > 500.0:
            current.append("PARK_DISTANCE_GT_500M")
        if abs(float(row.albedo) - float(row.simple_albedo)) >= 0.001:
            current.append("AGGREGATION_SENSITIVE")
        reasons.append(";".join(current))
    result = routes.copy()
    result["qa_reason"] = reasons
    return result[result["qa_reason"] != ""].copy()


def write_qa(samples: pd.DataFrame, routes: gpd.GeoDataFrame, qa_path: Path, overwrite: bool) -> None:
    """전체 샘플·링크와 이상후보를 하나의 GeoPackage로 생성한다."""
    qa_path.parent.mkdir(parents=True, exist_ok=True)
    if qa_path.exists() and not overwrite:
        raise FileExistsError(f"기존 통합 QA가 있습니다: {qa_path}")
    if qa_path.exists():
        qa_path.unlink()
    sample_gdf = gpd.GeoDataFrame(
        samples.drop(columns=["x", "y"]),
        geometry=gpd.points_from_xy(samples["x"], samples["y"], crs="EPSG:5186"),
        crs="EPSG:5186",
    )
    candidates = add_qa_reasons(routes)
    sample_gdf.to_file(qa_path, layer="sample_d4", driver="GPKG")
    routes.to_file(qa_path, layer="route_d4", driver="GPKG", mode="a")
    candidates.to_file(qa_path, layer="qa_candidates", driver="GPKG", mode="a")


def write_report(samples: pd.DataFrame, routes: pd.DataFrame, sensitivity: dict[str, float], path: Path,
                 applied: bool) -> None:
    """통합 커버리지, 수식 재검토, 제한과 DB 상태를 기록한다."""
    surface_distribution = routes["surface_type"].value_counts().to_dict()
    shade_distribution = routes["shade_grade_new"].value_counts().to_dict()
    temp_distribution = routes["temp_grade_new"].value_counts().to_dict()
    lines = [
        "# D4-3~D4-6 통합 QA 및 계산식 재검토", "", "## 완료 범위", "",
        "- D4-3 토지피복은 주변 맥락으로만 사용하고 보행 링크 유형으로 기본 노면 재질 분류",
        "- D4-4 샘플 물성 부여 및 링크 길이가중 집계",
        "- D4-5 UQT2xx 공원 폴리곤까지 거리 계산",
        "- D4-6 품질등급, 전체 커버리지, 민감도·반례 검증", "",
        "## 최종 커버리지", "",
        f"- 샘플: {len(samples):,} / {EXPECTED_SAMPLE_COUNT:,}",
        f"- 링크: {len(routes):,} / {EXPECTED_SEGMENT_COUNT:,}",
        f"- SVF 개활지 대체 샘플/링크: {int(sensitivity['svf_sample_fallback']):,} / {int(sensitivity['svf_route_fallback']):,}",
        "- SVF·재질·알베도·방사율·지중열비·공원거리 링크 NULL: 0", "",
        "## 링크 재질 분포", "",
    ]
    lines.extend(f"- {key}: {value:,}" for key, value in sorted(surface_distribution.items()))
    lines += ["", "## 품질등급", "", f"- shade_grade: {shade_distribution}", f"- temp_grade: {temp_distribution}", ""]
    lines += [
        "## 계산식 재검토 결론", "",
        "1. **SVF**: 건물 DSM만 사용하고 수목 원기둥 CDSM은 제외한다. Alpha 범위 밖 NULL은 명세의 개활지 가정 `1.0`으로 채우고 D등급으로 표시한다.",
        "2. **링크 집계**: 단순 점 평균 대신 샘플 좌우 중간점까지의 대표 길이를 가중치로 사용한다. 짧은 마지막 구간과 끝점 중복 편향을 막는다.",
        "3. **공원 거리**: 경계선까지가 아니라 공원 폴리곤까지 잰다. 따라서 공원 내부는 0m이며, 행정경계 바깥 인접 공원도 후보에 남긴다.",
        "4. **물성값**: 명세 기본값은 D5용 사전값이다. 현장 적외선 실측으로 직접 알베도를 추정할 수는 없으며, D5에서 재질별 온도 오차를 이용해 민감도·보정을 수행한다.",
        "5. **지중열비**: 고정비는 시간·수분·재질 상태를 생략한 근사다. FAO도 G/Rn 관계가 시간과 토양 상태에 민감한 근사임을 명시하므로 D5 검증 대상이다.", "",
        "## 재질 가정 민감도", "",
        f"- 토지피복과 링크 유형을 결합한 불확실 분류 샘플: {int(sensitivity['ambiguous_samples']):,}",
        f"- 현재 링크 평균 알베도: {sensitivity['baseline_mean_albedo']:.3f}",
        f"- 전부 asphalt 가정: {sensitivity['all_asphalt_mean_albedo']:.3f}",
        f"- 전부 pavement 가정: {sensitivity['all_pavement_mean_albedo']:.3f}",
        "- 원본은 실제 노면 재질도가 아니므로 차량 통행 비트로 asphalt/pavement를 추정한다. grass/soil은 현장 확인 수동 교정만 허용한다.", "",
        "## 문헌 대조", "",
        "- 미국 EPA는 새 아스팔트 반사율을 대략 0.05~0.10, 새 콘크리트를 0.35~0.40으로 제시한다. 명세의 0.12/0.28은 노후·오염·블록 차이를 감안한 중간 사전값으로 보고 실측 보정한다.",
        "- USGS Spectral Library는 토양·식생·아스팔트·콘크리트의 실측 분광 자료를 제공하며 단일 상수보다 재료별 변동이 존재함을 전제로 한다.",
        "- FAO-56은 시간별 지중열류가 중요하고 G/Rn 근사가 식생·토양색·수분·태양각 등을 생략한다고 경고한다.", "",
        "출처: [EPA Cool Pavements](https://nepis.epa.gov/Exe/ZyPURL.cgi?Dockey=2000TYST.TXT), "
        "[USGS Spectral Library v7](https://www.usgs.gov/publications/usgs-spectral-library-version-7), "
        "[FAO-56 Annex 5](https://www.fao.org/4/x0490e/x0490e0m.htm)", "",
        "## DB 반영", "",
        f"- 상태: {'반영 및 사후 검증 완료' if applied else '미반영(dry-run)'}",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def apply_to_database(
    samples: pd.DataFrame,
    routes: pd.DataFrame,
    db_config: dict[str, str | int],
) -> dict[str, object]:
    """검증 결과를 잠금과 사후 검사를 포함한 단일 트랜잭션으로 반영한다."""
    sample_rows = [
        (int(row.sample_id), round(float(row.svf_effective), 3), round(float(row.albedo), 3),
         round(float(row.emissivity), 3), round(float(row.ground_flux_ratio), 3))
        for row in samples.itertuples(index=False)
    ]
    route_rows = [
        (int(row.segment_id), str(row.surface_type), round(float(row.svf_effective), 3),
         round(float(row.albedo), 3), round(float(row.emissivity), 3),
         round(float(row.ground_flux_ratio), 3), round(float(row.park_proximity_m), 1),
         str(row.shade_grade_new), str(row.temp_grade_new))
        for row in routes.itertuples(index=False)
    ]
    with psycopg2.connect(**db_config) as connection:
        with connection.cursor() as cursor:
            cursor.execute("LOCK TABLE segment_sample_point, route_segment IN SHARE ROW EXCLUSIVE MODE")
            cursor.execute(
                """
                CREATE TEMP TABLE d4_sample_apply (
                    sample_id BIGINT PRIMARY KEY,
                    svf NUMERIC(4,3) NOT NULL,
                    albedo NUMERIC(4,3) NOT NULL,
                    emissivity NUMERIC(4,3) NOT NULL,
                    ground_flux NUMERIC(4,3) NOT NULL
                ) ON COMMIT DROP
                """
            )
            execute_values(
                cursor,
                """
                INSERT INTO d4_sample_apply (sample_id, svf, albedo, emissivity, ground_flux)
                VALUES %s
                """,
                sample_rows,
                template="(%s::bigint,%s::numeric,%s::numeric,%s::numeric,%s::numeric)",
                page_size=2000,
            )
            cursor.execute("SELECT COUNT(*) FROM d4_sample_apply")
            if int(cursor.fetchone()[0]) != len(sample_rows):
                raise RuntimeError("샘플 임시 staging 행 수가 다릅니다.")
            cursor.execute(
                """
                UPDATE segment_sample_point AS p SET
                    svf = v.svf, albedo = v.albedo, emissivity = v.emissivity,
                    ground_flux_ratio = v.ground_flux
                FROM d4_sample_apply v WHERE p.sample_id = v.sample_id
                """
            )
            sample_updated = cursor.rowcount
            if sample_updated != len(sample_rows):
                raise RuntimeError(f"샘플 UPDATE 수 불일치: {sample_updated:,} / {len(sample_rows):,}")
            cursor.execute(
                """
                CREATE TEMP TABLE d4_route_apply (
                    segment_id BIGINT PRIMARY KEY,
                    surface_type VARCHAR(20) NOT NULL,
                    svf NUMERIC(4,3) NOT NULL,
                    albedo NUMERIC(4,3) NOT NULL,
                    emissivity NUMERIC(4,3) NOT NULL,
                    ground_flux NUMERIC(4,3) NOT NULL,
                    park_distance NUMERIC(7,1) NOT NULL,
                    shade_grade CHAR(1) NOT NULL,
                    temp_grade CHAR(1) NOT NULL
                ) ON COMMIT DROP
                """
            )
            execute_values(
                cursor,
                """
                INSERT INTO d4_route_apply (
                    segment_id, surface_type, svf, albedo, emissivity,
                    ground_flux, park_distance, shade_grade, temp_grade
                ) VALUES %s
                """,
                route_rows,
                template="(%s::bigint,%s::varchar,%s::numeric,%s::numeric,%s::numeric,%s::numeric,%s::numeric,%s::char,%s::char)",
                page_size=1000,
            )
            cursor.execute("SELECT COUNT(*) FROM d4_route_apply")
            if int(cursor.fetchone()[0]) != len(route_rows):
                raise RuntimeError("링크 임시 staging 행 수가 다릅니다.")
            cursor.execute(
                """
                UPDATE route_segment AS r SET
                    surface_type = v.surface_type, svf = v.svf,
                    albedo = v.albedo, emissivity = v.emissivity,
                    ground_flux_ratio = v.ground_flux,
                    park_proximity_m = v.park_distance,
                    shade_grade = v.shade_grade, temp_grade = v.temp_grade
                FROM d4_route_apply v WHERE r.segment_id = v.segment_id
                """
            )
            route_updated = cursor.rowcount
            if route_updated != len(route_rows):
                raise RuntimeError(f"링크 UPDATE 수 불일치: {route_updated:,} / {len(route_rows):,}")
            cursor.execute(
                """
                SELECT COUNT(*) FROM segment_sample_point p
                JOIN d4_sample_apply v USING (sample_id)
                WHERE p.svf IS DISTINCT FROM v.svf
                   OR p.albedo IS DISTINCT FROM v.albedo
                   OR p.emissivity IS DISTINCT FROM v.emissivity
                   OR p.ground_flux_ratio IS DISTINCT FROM v.ground_flux
                """
            )
            if int(cursor.fetchone()[0]):
                raise RuntimeError("샘플 staging과 반영값이 일치하지 않습니다.")
            cursor.execute(
                """
                SELECT COUNT(*) FROM route_segment r
                JOIN d4_route_apply v USING (segment_id)
                WHERE r.surface_type IS DISTINCT FROM v.surface_type
                   OR r.svf IS DISTINCT FROM v.svf
                   OR r.albedo IS DISTINCT FROM v.albedo
                   OR r.emissivity IS DISTINCT FROM v.emissivity
                   OR r.ground_flux_ratio IS DISTINCT FROM v.ground_flux
                   OR r.park_proximity_m IS DISTINCT FROM v.park_distance
                   OR r.shade_grade IS DISTINCT FROM v.shade_grade
                   OR r.temp_grade IS DISTINCT FROM v.temp_grade
                """
            )
            if int(cursor.fetchone()[0]):
                raise RuntimeError("링크 staging과 반영값이 일치하지 않습니다.")
            cursor.execute(
                """
                SELECT COUNT(*) FILTER (WHERE surface_type IS NULL),
                       COUNT(*) FILTER (WHERE svf IS NULL),
                       COUNT(*) FILTER (WHERE albedo IS NULL),
                       COUNT(*) FILTER (WHERE emissivity IS NULL),
                       COUNT(*) FILTER (WHERE ground_flux_ratio IS NULL),
                       COUNT(*) FILTER (WHERE park_proximity_m IS NULL),
                       MIN(svf), MAX(svf), MIN(park_proximity_m), MAX(park_proximity_m)
                FROM route_segment
                """
            )
            route_check = cursor.fetchone()
            if any(int(value) != 0 for value in route_check[:6]):
                raise RuntimeError(f"DB 반영 후 링크 NULL 검사 실패: {route_check}")
            cursor.execute(
                """
                SELECT COUNT(*) FILTER (WHERE svf IS NULL),
                       COUNT(*) FILTER (WHERE albedo IS NULL),
                       COUNT(*) FILTER (WHERE emissivity IS NULL),
                       COUNT(*) FILTER (WHERE ground_flux_ratio IS NULL),
                       MIN(svf), MAX(svf)
                FROM segment_sample_point
                """
            )
            sample_check = cursor.fetchone()
            if any(int(value) != 0 for value in sample_check[:4]):
                raise RuntimeError(f"DB 반영 후 샘플 NULL 검사 실패: {sample_check}")
            cursor.execute("SELECT shade_grade, temp_grade, COUNT(*) FROM route_segment GROUP BY 1,2 ORDER BY 1,2")
            grades = cursor.fetchall()
    return {"route_check": route_check, "sample_check": sample_check, "grades": grades}


def main() -> None:
    """통합 QA를 먼저 만들고 요청된 경우에만 마지막 단계로 DB를 반영한다."""
    args = parse_arguments()
    # 경로 인자를 기본 상수 대신 사용할 수 있도록 검증 후 기본 위치로 정규화한다.
    global DEFAULT_SAMPLE_SURFACE, DEFAULT_ROUTE_SURFACE, DEFAULT_SAMPLE_PARK, DEFAULT_ROUTE_PARK
    DEFAULT_SAMPLE_SURFACE = args.sample_surface.resolve()
    DEFAULT_ROUTE_SURFACE = args.route_surface.resolve()
    DEFAULT_SAMPLE_PARK = args.sample_park.resolve()
    DEFAULT_ROUTE_PARK = args.route_park.resolve()

    db_config = build_db_config(args.target)
    print(f"[DB 연결] {describe_db_target(args.target, db_config)}")
    samples, routes, sensitivity = build_integrated(db_config)
    validate_integrated(samples, routes)
    write_qa(samples, routes, args.qa_gpkg.resolve(), args.overwrite)
    write_report(samples, routes, sensitivity, args.report.resolve(), applied=False)
    print(f"[통합 QA 통과] 샘플 {len(samples):,}, 링크 {len(routes):,}")
    print(f"[QA] {args.qa_gpkg.resolve()}")
    if args.apply:
        checks = apply_to_database(samples, routes, db_config)
        write_report(samples, routes, sensitivity, args.report.resolve(), applied=True)
        print(f"[DB 반영 완료] {checks}")
    else:
        print("[안내] dry-run입니다. 계산식 재검토 후 --apply로 반영하세요.")


if __name__ == "__main__":
    main()
