import csv
import os
from pathlib import Path

import geopandas as gpd
import psycopg2
from psycopg2.extras import execute_values


PIPELINE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PIPELINE_ROOT.parent
BACKEND_ENV_PATH = PROJECT_ROOT / "backend" / ".env"
SNAPPED_NETWORK_PATH = (
    PIPELINE_ROOT
    / "data"
    / "processed"
    / "network"
    / "junggu_walk_network_snapped.gpkg"
)
SNAPPED_LAYER = "snapped_links"
EXCLUSION_PATH = PIPELINE_ROOT / "config" / "network_exclusions.csv"
EXPECTED_INPUT_LINK_COUNT = 7_771
EXPECTED_ROUTABLE_LINK_COUNT = 7_766
ACTIVE_EXCLUSION_STATUSES = {"EXCLUDE", "EXCLUDE_TEMPORARY"}


def read_env_file(path: Path) -> dict[str, str]:
    """간단한 KEY=VALUE 형식의 backend .env를 읽어 사전으로 반환한다."""
    if not path.exists():
        raise FileNotFoundError(f"DB 환경설정 파일이 없습니다: {path}")

    settings: dict[str, str] = {}

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        settings[key.strip()] = value.strip().strip('"').strip("'")

    return settings


def build_db_config() -> dict[str, str | int]:
    """환경변수를 우선하고, 없으면 backend .env에서 DB 설정을 가져온다."""
    env_settings = read_env_file(BACKEND_ENV_PATH)

    config: dict[str, str | int] = {
        "host": os.getenv("POSTGRES_HOST", "localhost"),
        "port": int(
            os.getenv(
                "POSTGRES_PORT",
                env_settings.get("POSTGRES_PORT", "15432"),
            )
        ),
        "dbname": os.getenv(
            "POSTGRES_DB",
            env_settings.get("POSTGRES_DB", ""),
        ),
        "user": os.getenv(
            "POSTGRES_USER",
            env_settings.get("POSTGRES_USER", ""),
        ),
        "password": os.getenv(
            "POSTGRES_PASSWORD",
            env_settings.get("POSTGRES_PASSWORD", ""),
        ),
    }

    missing = [
        key
        for key in ("dbname", "user", "password")
        if not config[key]
    ]
    if missing:
        raise ValueError(f"DB 설정값이 없습니다: {', '.join(missing)}")

    return config


def load_and_validate_snapped_links() -> gpd.GeoDataFrame:
    """processed GeoPackage를 읽어 staging 적재 전 필수 품질을 검사한다."""
    if not SNAPPED_NETWORK_PATH.exists():
        raise FileNotFoundError(
            f"스냅 도보망 파일이 없습니다: {SNAPPED_NETWORK_PATH}"
        )

    links = gpd.read_file(
        SNAPPED_NETWORK_PATH,
        layer=SNAPPED_LAYER,
    )

    required_columns = {
        "segment_id",
        "link_type_code",
        "original_start_node_id",
        "original_end_node_id",
        "length_m",
        "geometry",
    }
    missing_columns = required_columns - set(links.columns)

    if missing_columns:
        raise ValueError(
            "적재 파일에 필수 컬럼이 없습니다: "
            + ", ".join(sorted(missing_columns))
        )
    if len(links) != EXPECTED_INPUT_LINK_COUNT:
        raise ValueError(
            f"적재 LINK 수가 다릅니다: {len(links):,} / "
            f"예상 {EXPECTED_INPUT_LINK_COUNT:,}"
        )
    if not links["segment_id"].is_unique:
        raise ValueError("segment_id가 중복되었습니다.")
    if links.crs is None or links.crs.to_epsg() != 5186:
        raise ValueError(f"적재 파일 CRS가 EPSG:5186이 아닙니다: {links.crs}")
    if not links.geometry.geom_type.eq("LineString").all():
        raise ValueError("LineString이 아닌 geometry가 있습니다.")
    if links.geometry.is_empty.any() or links.geometry.isna().any():
        raise ValueError("비어 있거나 누락된 geometry가 있습니다.")
    if not links.geometry.is_valid.all():
        raise ValueError("유효하지 않은 geometry가 있습니다.")
    if (links["length_m"] <= 0).any():
        raise ValueError("길이가 0 이하인 LINK가 있습니다.")

    return links


def apply_network_exclusions(
    links: gpd.GeoDataFrame,
) -> tuple[gpd.GeoDataFrame, dict[int, str]]:
    """QA 제외 설정을 읽어 staging에 넣지 않을 LINK를 제거한다.

    원본 processed GeoPackage는 수정하지 않는다. `EXCLUDE`와
    `EXCLUDE_TEMPORARY` 상태만 활성 제외로 취급하며, 설정의 모든 segment가
    현재 입력에 실제로 존재하는지 확인한다.
    """
    if not EXCLUSION_PATH.exists():
        raise FileNotFoundError(f"네트워크 제외 설정이 없습니다: {EXCLUSION_PATH}")

    with EXCLUSION_PATH.open("r", encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file)
        required_columns = {"segment_id", "status", "exclusion_code"}
        missing_columns = required_columns - set(reader.fieldnames or [])
        if missing_columns:
            raise ValueError(
                "제외 설정에 필수 컬럼이 없습니다: "
                + ", ".join(sorted(missing_columns))
            )

        exclusions: dict[int, str] = {}
        for row in reader:
            status = row["status"].strip().upper()
            if status not in ACTIVE_EXCLUSION_STATUSES:
                continue

            segment_id = int(row["segment_id"])
            if segment_id in exclusions:
                raise ValueError(
                    f"제외 설정에 segment_id가 중복되었습니다: {segment_id}"
                )
            exclusions[segment_id] = row["exclusion_code"].strip().upper()

    input_segment_ids = set(links["segment_id"].astype("int64"))
    missing_segment_ids = set(exclusions) - input_segment_ids
    if missing_segment_ids:
        missing = ", ".join(str(value) for value in sorted(missing_segment_ids))
        raise ValueError(f"입력 도보망에 없는 제외 segment_id가 있습니다: {missing}")

    routable_links = links.loc[
        ~links["segment_id"].astype("int64").isin(exclusions)
    ].copy()
    if len(routable_links) != EXPECTED_ROUTABLE_LINK_COUNT:
        raise ValueError(
            f"제외 후 LINK 수가 다릅니다: {len(routable_links):,} / "
            f"예상 {EXPECTED_ROUTABLE_LINK_COUNT:,}"
        )

    return routable_links, exclusions


def make_insert_rows(links: gpd.GeoDataFrame) -> list[tuple]:
    """GeoDataFrame 각 행을 psycopg2 일괄 INSERT용 튜플로 변환한다."""
    rows: list[tuple] = []

    for link in links.itertuples(index=False):
        rows.append(
            (
                int(link.segment_id),
                link.geometry.wkb_hex,
                float(link.length_m),
                str(link.link_type_code),
                int(link.original_start_node_id),
                int(link.original_end_node_id),
            )
        )

    return rows


def load_staging_table(
    links: gpd.GeoDataFrame,
    db_config: dict[str, str | int],
) -> dict[str, int | float]:
    """
    스냅 LINK를 route_segment_staging에 트랜잭션 단위로 다시 적재한다.

    source와 target은 vertex 추출 전 단계이므로 INSERT에서 명시적으로
    NULL로 둔다. SQL 품질검사까지 통과해야 transaction이 commit된다.
    """
    insert_rows = make_insert_rows(links)

    with psycopg2.connect(**db_config) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT to_regclass('public.route_segment_staging')"
            )
            if cursor.fetchone()[0] is None:
                raise RuntimeError(
                    "route_segment_staging 테이블이 없습니다. "
                    "Flyway V4 적용 상태를 확인하세요."
                )

            # staging은 언제든 같은 processed 파일로 재생성할 수 있는 파생 데이터다.
            cursor.execute("TRUNCATE TABLE route_segment_staging")

            execute_values(
                cursor,
                """
                INSERT INTO route_segment_staging (
                    segment_id,
                    source,
                    target,
                    geom,
                    length_m,
                    link_type_code,
                    original_start_node_id,
                    original_end_node_id
                ) VALUES %s
                """,
                insert_rows,
                template=(
                    "(%s, NULL, NULL, "
                    "ST_SetSRID(ST_GeomFromWKB(decode(%s, 'hex')), 5186), "
                    "%s, %s, %s, %s)"
                ),
                page_size=1_000,
            )

            cursor.execute(
                """
                SELECT
                    COUNT(*) AS row_count,
                    COUNT(*) FILTER (WHERE source IS NULL) AS null_source_count,
                    COUNT(*) FILTER (WHERE target IS NULL) AS null_target_count,
                    COUNT(*) FILTER (WHERE ST_SRID(geom) <> 5186) AS bad_srid_count,
                    COUNT(*) FILTER (
                        WHERE GeometryType(geom) <> 'LINESTRING'
                    ) AS bad_geometry_type_count,
                    COUNT(*) FILTER (WHERE NOT ST_IsValid(geom)) AS invalid_count,
                    COUNT(*) FILTER (
                        WHERE ST_IsEmpty(geom) OR length_m <= 0
                    ) AS unusable_count,
                    COUNT(*) - COUNT(DISTINCT segment_id) AS duplicate_id_count,
                    COALESCE(MAX(ABS(length_m - ST_Length(geom))), 0)
                        AS maximum_length_difference_m
                FROM route_segment_staging
                """
            )
            result = cursor.fetchone()

            statistics: dict[str, int | float] = {
                "row_count": int(result[0]),
                "null_source_count": int(result[1]),
                "null_target_count": int(result[2]),
                "bad_srid_count": int(result[3]),
                "bad_geometry_type_count": int(result[4]),
                "invalid_count": int(result[5]),
                "unusable_count": int(result[6]),
                "duplicate_id_count": int(result[7]),
                "maximum_length_difference_m": float(result[8]),
            }

            if statistics["row_count"] != len(links):
                raise RuntimeError("staging 적재 행 수가 입력 LINK 수와 다릅니다.")
            if statistics["null_source_count"] != len(links):
                raise RuntimeError("vertex 추출 전 source에 값이 들어갔습니다.")
            if statistics["null_target_count"] != len(links):
                raise RuntimeError("vertex 추출 전 target에 값이 들어갔습니다.")

            failure_keys = (
                "bad_srid_count",
                "bad_geometry_type_count",
                "invalid_count",
                "unusable_count",
                "duplicate_id_count",
            )
            failures = {
                key: statistics[key]
                for key in failure_keys
                if statistics[key] != 0
            }
            if failures:
                raise RuntimeError(f"staging SQL 품질검사 실패: {failures}")
            if statistics["maximum_length_difference_m"] > 0.01:
                raise RuntimeError(
                    "DB length_m과 geometry 길이 차이가 0.01m를 초과했습니다: "
                    f"{statistics['maximum_length_difference_m']:.6f}m"
                )

    return statistics


def main() -> None:
    """D2 processed 도보망을 DB staging에 적재하고 결과를 출력한다."""
    input_links = load_and_validate_snapped_links()
    links, exclusions = apply_network_exclusions(input_links)
    db_config = build_db_config()

    print(f"[입력 파일] {SNAPPED_NETWORK_PATH}")
    print(f"[입력 레이어] {SNAPPED_LAYER}")
    print(f"[원본 processed LINK] {len(input_links):,}")
    print(f"[QA 제외 LINK] {len(exclusions):,}")
    for segment_id, exclusion_code in sorted(exclusions.items()):
        print(f"  {segment_id}: {exclusion_code}")
    print(f"[routing 대상 LINK] {len(links):,}")
    print(
        "[DB 연결] "
        f"{db_config['host']}:{db_config['port']}/{db_config['dbname']}"
    )

    statistics = load_staging_table(links, db_config)

    print()
    print("[route_segment_staging 적재 결과]")
    print(f"  적재 행 수: {statistics['row_count']:,}")
    print(f"  source NULL: {statistics['null_source_count']:,}")
    print(f"  target NULL: {statistics['null_target_count']:,}")
    print(f"  잘못된 SRID: {statistics['bad_srid_count']:,}")
    print(f"  잘못된 geometry 유형: {statistics['bad_geometry_type_count']:,}")
    print(f"  유효하지 않은 geometry: {statistics['invalid_count']:,}")
    print(f"  사용 불가 LINK: {statistics['unusable_count']:,}")
    print(f"  중복 segment_id: {statistics['duplicate_id_count']:,}")
    print(
        "  최대 길이 오차: "
        f"{statistics['maximum_length_difference_m']:.6f} m"
    )
    print("\n[D2-3 staging 적재 완료]")


if __name__ == "__main__":
    main()
