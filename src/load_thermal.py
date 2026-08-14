from __future__ import annotations

import argparse
from datetime import date, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import psycopg2
from psycopg2.extras import execute_values

from d4_common import EXPECTED_SEGMENT_COUNT
from load_segments import build_db_config


PIPELINE_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT = PIPELINE_ROOT / "data" / "processed" / "d5" / "route_thermal.csv"
DEFAULT_REPORT = PIPELINE_ROOT / "reports" / "d5_completion.md"
HOURS = (9, 12, 15, 18)
WEATHER_DATE = date(2026, 8, 11)
WEATHER_STATUS = "OBSERVED_ASOS_SAME_DATE"
MODEL_CONFIDENCE = "LOW"
TEMPERATURE_COLUMNS = [f"surface_temp_{hour:02d}_c" for hour in HOURS]
REQUIRED_COLUMNS = [
    "segment_id", "surface_type", "weather_status", "model_confidence",
    *TEMPERATURE_COLUMNS, "surface_temp_peak_c",
]


def parse_arguments() -> argparse.Namespace:
    """적재 CSV, 완료 보고서와 실제 반영 여부를 받는다."""
    parser = argparse.ArgumentParser(description="D5 링크 온도 트랜잭션 적재 및 사후 검증")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--apply", action="store_true", help="검증 통과 후 DB에 실제 반영")
    return parser.parse_args()


def validate_thermal_frame(frame: pd.DataFrame) -> pd.DataFrame:
    """행 수, ID, 재질, 기상, 신뢰도, 온도와 peak를 DB 연결 전에 검증한다."""
    missing_columns = [column for column in REQUIRED_COLUMNS if column not in frame.columns]
    if missing_columns:
        raise ValueError(f"D5 링크 CSV 필수 컬럼이 없습니다: {missing_columns}")
    if len(frame) != EXPECTED_SEGMENT_COUNT:
        raise ValueError(
            f"D5 링크 행 수가 다릅니다: {len(frame):,} / 예상 {EXPECTED_SEGMENT_COUNT:,}"
        )

    result = frame[REQUIRED_COLUMNS].copy()
    result["segment_id"] = pd.to_numeric(result["segment_id"], errors="coerce")
    if result["segment_id"].isna().any():
        raise ValueError("segment_id에 NULL 또는 숫자가 아닌 값이 있습니다.")
    if not np.equal(result["segment_id"], np.floor(result["segment_id"])).all():
        raise ValueError("segment_id에 정수가 아닌 값이 있습니다.")
    result["segment_id"] = result["segment_id"].astype("int64")
    if not result["segment_id"].is_unique:
        raise ValueError("segment_id가 중복되었습니다.")
    if result["surface_type"].isna().any() or (result["surface_type"].astype(str).str.strip() == "").any():
        raise ValueError("surface_type에 NULL 또는 빈 값이 있습니다.")
    if set(result["weather_status"].astype(str)) != {WEATHER_STATUS}:
        raise ValueError("weather_status가 확정 기상 상태와 다릅니다.")
    if set(result["model_confidence"].astype(str)) != {MODEL_CONFIDENCE}:
        raise ValueError("model_confidence가 LOW와 다릅니다.")

    numeric_columns = [*TEMPERATURE_COLUMNS, "surface_temp_peak_c"]
    for column in numeric_columns:
        result[column] = pd.to_numeric(result[column], errors="coerce")
    numeric = result[numeric_columns].to_numpy(dtype=float)
    if not np.isfinite(numeric).all():
        raise ValueError("온도에 NULL, NaN 또는 무한대가 있습니다.")
    if float(numeric.min()) < -50.0 or float(numeric.max()) > 100.0:
        raise ValueError("온도가 안전 범위(-50~100°C)를 벗어났습니다.")
    expected_peak = result[TEMPERATURE_COLUMNS].max(axis=1)
    if not np.allclose(result["surface_temp_peak_c"], expected_peak, atol=1e-9, rtol=0):
        raise ValueError("surface_temp_peak_c가 네 시각 최댓값과 다릅니다.")
    return result.sort_values("segment_id").reset_index(drop=True)


def thermal_rows(frame: pd.DataFrame) -> list[tuple[object, ...]]:
    """PostgreSQL 임시 staging에 넣을 명시적 타입의 행을 만든다."""
    return [
        (
            int(row.segment_id), str(row.surface_type),
            round(float(row.surface_temp_09_c), 2),
            round(float(row.surface_temp_12_c), 2),
            round(float(row.surface_temp_15_c), 2),
            round(float(row.surface_temp_18_c), 2),
            round(float(row.surface_temp_peak_c), 2),
            str(row.model_confidence), WEATHER_DATE,
        )
        for row in frame.itertuples(index=False)
    ]


def create_staging(cursor: Any, rows: list[tuple[object, ...]]) -> None:
    """현재 트랜잭션에 D5 링크 온도 임시 staging을 생성한다."""
    cursor.execute(
        """
        CREATE TEMP TABLE d5_thermal_apply (
            segment_id BIGINT PRIMARY KEY,
            surface_type VARCHAR(20) NOT NULL,
            surface_temp_09_c NUMERIC(5,2) NOT NULL,
            surface_temp_12_c NUMERIC(5,2) NOT NULL,
            surface_temp_15_c NUMERIC(5,2) NOT NULL,
            surface_temp_18_c NUMERIC(5,2) NOT NULL,
            surface_temp_peak_c NUMERIC(5,2) NOT NULL,
            model_confidence VARCHAR(10) NOT NULL,
            weather_date DATE NOT NULL
        ) ON COMMIT DROP
        """
    )
    execute_values(
        cursor,
        """
        INSERT INTO d5_thermal_apply (
            segment_id, surface_type,
            surface_temp_09_c, surface_temp_12_c,
            surface_temp_15_c, surface_temp_18_c,
            surface_temp_peak_c, model_confidence, weather_date
        ) VALUES %s
        """,
        rows,
        template=(
            "(%s::bigint,%s::varchar,%s::numeric,%s::numeric,%s::numeric,"
            "%s::numeric,%s::numeric,%s::varchar,%s::date)"
        ),
        page_size=1000,
    )
    cursor.execute("SELECT COUNT(*) FROM d5_thermal_apply")
    staged = int(cursor.fetchone()[0])
    if staged != EXPECTED_SEGMENT_COUNT:
        raise RuntimeError(f"D5 임시 staging 행 수 불일치: {staged:,}")


def validate_schema_and_identity(cursor: Any) -> dict[str, int]:
    """V7 스키마, DB 행 수, ID 집합과 D4 재질이 staging과 같은지 확인한다."""
    cursor.execute(
        """
        SELECT COUNT(*)
        FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'route_segment'
          AND column_name IN (
              'surface_temp_09_c', 'surface_temp_12_c', 'surface_temp_15_c',
              'surface_temp_18_c', 'surface_temp_peak_c',
              'thermal_model_confidence', 'thermal_weather_date', 'thermal_updated_at'
          )
        """
    )
    thermal_column_count = int(cursor.fetchone()[0])
    if thermal_column_count != 8:
        raise RuntimeError(f"Flyway V7 온도 컬럼 수 불일치: {thermal_column_count} / 8")
    cursor.execute("SELECT COUNT(*) FROM route_segment")
    database_rows = int(cursor.fetchone()[0])
    if database_rows != EXPECTED_SEGMENT_COUNT:
        raise RuntimeError(f"DB 링크 행 수 불일치: {database_rows:,}")
    cursor.execute(
        """
        SELECT COUNT(*)
        FROM route_segment AS route
        FULL JOIN d5_thermal_apply AS staged USING (segment_id)
        WHERE route.segment_id IS NULL OR staged.segment_id IS NULL
        """
    )
    id_mismatches = int(cursor.fetchone()[0])
    cursor.execute(
        """
        SELECT COUNT(*)
        FROM route_segment AS route
        JOIN d5_thermal_apply AS staged USING (segment_id)
        WHERE route.surface_type IS DISTINCT FROM staged.surface_type
        """
    )
    surface_mismatches = int(cursor.fetchone()[0])
    if id_mismatches or surface_mismatches:
        raise RuntimeError(
            f"DB/CSV 사전 정합성 실패: ID={id_mismatches}, 재질={surface_mismatches}"
        )
    return {
        "thermal_column_count": thermal_column_count,
        "database_rows": database_rows,
        "id_mismatches": id_mismatches,
        "surface_mismatches": surface_mismatches,
    }


def create_d4_snapshot(cursor: Any) -> None:
    """온도 적재가 변경하면 안 되는 모든 기존 링크 필드를 트랜잭션 안에 보관한다."""
    cursor.execute(
        """
        CREATE TEMP TABLE d5_route_before ON COMMIT DROP AS
        SELECT segment_id, source, target, ST_AsEWKB(geom) AS geom_wkb, length_m,
               surface_type, svf, albedo, emissivity, ground_flux_ratio,
               park_proximity_m,
               tree_shade_ratio_09, tree_shade_ratio_12,
               tree_shade_ratio_15, tree_shade_ratio_18,
               bldg_shade_ratio_09, bldg_shade_ratio_12,
               bldg_shade_ratio_15, bldg_shade_ratio_18,
               shade_ratio_09, shade_ratio_12, shade_ratio_15, shade_ratio_18,
               shade_grade, temp_grade
        FROM route_segment
        """
    )


def count_d4_changes(cursor: Any) -> int:
    """D5 UPDATE 전후에 기존 네트워크·D4 필드가 바뀐 행 수를 센다."""
    cursor.execute(
        """
        SELECT COUNT(*)
        FROM route_segment AS route
        JOIN d5_route_before AS before USING (segment_id)
        WHERE route.source IS DISTINCT FROM before.source
           OR route.target IS DISTINCT FROM before.target
           OR ST_AsEWKB(route.geom) IS DISTINCT FROM before.geom_wkb
           OR route.length_m IS DISTINCT FROM before.length_m
           OR route.surface_type IS DISTINCT FROM before.surface_type
           OR route.svf IS DISTINCT FROM before.svf
           OR route.albedo IS DISTINCT FROM before.albedo
           OR route.emissivity IS DISTINCT FROM before.emissivity
           OR route.ground_flux_ratio IS DISTINCT FROM before.ground_flux_ratio
           OR route.park_proximity_m IS DISTINCT FROM before.park_proximity_m
           OR route.tree_shade_ratio_09 IS DISTINCT FROM before.tree_shade_ratio_09
           OR route.tree_shade_ratio_12 IS DISTINCT FROM before.tree_shade_ratio_12
           OR route.tree_shade_ratio_15 IS DISTINCT FROM before.tree_shade_ratio_15
           OR route.tree_shade_ratio_18 IS DISTINCT FROM before.tree_shade_ratio_18
           OR route.bldg_shade_ratio_09 IS DISTINCT FROM before.bldg_shade_ratio_09
           OR route.bldg_shade_ratio_12 IS DISTINCT FROM before.bldg_shade_ratio_12
           OR route.bldg_shade_ratio_15 IS DISTINCT FROM before.bldg_shade_ratio_15
           OR route.bldg_shade_ratio_18 IS DISTINCT FROM before.bldg_shade_ratio_18
           OR route.shade_ratio_09 IS DISTINCT FROM before.shade_ratio_09
           OR route.shade_ratio_12 IS DISTINCT FROM before.shade_ratio_12
           OR route.shade_ratio_15 IS DISTINCT FROM before.shade_ratio_15
           OR route.shade_ratio_18 IS DISTINCT FROM before.shade_ratio_18
           OR route.shade_grade IS DISTINCT FROM before.shade_grade
           OR route.temp_grade IS DISTINCT FROM before.temp_grade
        """
    )
    return int(cursor.fetchone()[0])


def verify_staged_database(cursor: Any) -> dict[str, Any]:
    """DB 온도와 staging 전체를 대조하고 NULL·peak·기상 메타데이터를 검사한다."""
    cursor.execute(
        """
        SELECT
            COUNT(*) FILTER (
                WHERE route.surface_temp_09_c IS NULL OR route.surface_temp_12_c IS NULL
                   OR route.surface_temp_15_c IS NULL OR route.surface_temp_18_c IS NULL
                   OR route.surface_temp_peak_c IS NULL
            ) AS null_rows,
            COUNT(*) FILTER (
                WHERE route.surface_temp_peak_c IS DISTINCT FROM GREATEST(
                    route.surface_temp_09_c, route.surface_temp_12_c,
                    route.surface_temp_15_c, route.surface_temp_18_c
                )
            ) AS peak_errors,
            COUNT(*) FILTER (
                WHERE route.thermal_model_confidence IS DISTINCT FROM staged.model_confidence
                   OR route.thermal_weather_date IS DISTINCT FROM staged.weather_date
            ) AS metadata_mismatches,
            COUNT(*) FILTER (WHERE route.thermal_updated_at IS NULL) AS updated_at_nulls,
            COUNT(*) FILTER (
                WHERE route.surface_temp_09_c IS DISTINCT FROM staged.surface_temp_09_c
                   OR route.surface_temp_12_c IS DISTINCT FROM staged.surface_temp_12_c
                   OR route.surface_temp_15_c IS DISTINCT FROM staged.surface_temp_15_c
                   OR route.surface_temp_18_c IS DISTINCT FROM staged.surface_temp_18_c
                   OR route.surface_temp_peak_c IS DISTINCT FROM staged.surface_temp_peak_c
            ) AS csv_mismatches,
            COALESCE(MAX(GREATEST(
                ABS(route.surface_temp_09_c - staged.surface_temp_09_c),
                ABS(route.surface_temp_12_c - staged.surface_temp_12_c),
                ABS(route.surface_temp_15_c - staged.surface_temp_15_c),
                ABS(route.surface_temp_18_c - staged.surface_temp_18_c),
                ABS(route.surface_temp_peak_c - staged.surface_temp_peak_c)
            )), 0) AS maximum_error
        FROM route_segment AS route
        JOIN d5_thermal_apply AS staged USING (segment_id)
        """
    )
    row = cursor.fetchone()
    checks: dict[str, Any] = {
        "null_rows": int(row[0]),
        "peak_errors": int(row[1]),
        "metadata_mismatches": int(row[2]),
        "updated_at_nulls": int(row[3]),
        "csv_mismatches": int(row[4]),
        "maximum_error_c": float(row[5]),
    }
    failures = {key: value for key, value in checks.items() if key != "maximum_error_c" and value != 0}
    if failures or checks["maximum_error_c"] > 0.01:
        raise RuntimeError(f"D5 DB 온도 사후 검증 실패: {checks}")

    cursor.execute(
        """
        SELECT
            MIN(surface_temp_09_c), AVG(surface_temp_09_c), MAX(surface_temp_09_c),
            MIN(surface_temp_12_c), AVG(surface_temp_12_c), MAX(surface_temp_12_c),
            MIN(surface_temp_15_c), AVG(surface_temp_15_c), MAX(surface_temp_15_c),
            MIN(surface_temp_18_c), AVG(surface_temp_18_c), MAX(surface_temp_18_c),
            MIN(surface_temp_peak_c), AVG(surface_temp_peak_c), MAX(surface_temp_peak_c)
        FROM route_segment
        """
    )
    values = [float(value) for value in cursor.fetchone()]
    checks["temperature_statistics"] = {
        label: {"min": values[index], "mean": values[index + 1], "max": values[index + 2]}
        for index, label in zip(range(0, 15, 3), ["09", "12", "15", "18", "peak"])
    }
    return checks


def apply_thermal(frame: pd.DataFrame, db_config: dict[str, str | int]) -> dict[str, Any]:
    """D5 온도를 한 트랜잭션으로 갱신하고 실패 시 전체 롤백한다."""
    rows = thermal_rows(frame)
    with psycopg2.connect(**db_config) as connection:
        with connection.cursor() as cursor:
            cursor.execute("LOCK TABLE route_segment IN SHARE ROW EXCLUSIVE MODE")
            create_staging(cursor, rows)
            preflight = validate_schema_and_identity(cursor)
            create_d4_snapshot(cursor)
            cursor.execute(
                """
                UPDATE route_segment AS route SET
                    surface_temp_09_c = staged.surface_temp_09_c,
                    surface_temp_12_c = staged.surface_temp_12_c,
                    surface_temp_15_c = staged.surface_temp_15_c,
                    surface_temp_18_c = staged.surface_temp_18_c,
                    surface_temp_peak_c = staged.surface_temp_peak_c,
                    thermal_model_confidence = staged.model_confidence,
                    thermal_weather_date = staged.weather_date,
                    thermal_updated_at = CURRENT_TIMESTAMP
                FROM d5_thermal_apply AS staged
                WHERE route.segment_id = staged.segment_id
                """
            )
            updated_rows = int(cursor.rowcount)
            if updated_rows != EXPECTED_SEGMENT_COUNT:
                raise RuntimeError(f"D5 UPDATE 행 수 불일치: {updated_rows:,}")
            d4_changed_rows = count_d4_changes(cursor)
            if d4_changed_rows:
                raise RuntimeError(f"온도 적재 중 기존 D4 필드가 변경됐습니다: {d4_changed_rows:,}행")
            verification = verify_staged_database(cursor)
    return {
        "preflight": preflight,
        "updated_rows": updated_rows,
        "d4_changed_rows": d4_changed_rows,
        "transaction_verification": verification,
    }


def verify_committed_database(frame: pd.DataFrame, db_config: dict[str, str | int]) -> dict[str, Any]:
    """커밋 후 새 연결에서 CSV 전체와 영속 DB 상태를 다시 비교한다."""
    rows = thermal_rows(frame)
    with psycopg2.connect(**db_config) as connection:
        with connection.cursor() as cursor:
            create_staging(cursor, rows)
            identity = validate_schema_and_identity(cursor)
            verification = verify_staged_database(cursor)
            cursor.execute(
                """
                SELECT version, description, success, installed_on
                FROM flyway_schema_history WHERE version = '7'
                """
            )
            flyway_v7 = cursor.fetchone()
            if flyway_v7 is None or not bool(flyway_v7[2]):
                raise RuntimeError("Flyway V7 성공 이력을 찾을 수 없습니다.")

            representative_ids = [
                int(frame.loc[frame["surface_temp_peak_c"].idxmin(), "segment_id"]),
                int(frame.iloc[len(frame) // 2]["segment_id"]),
                int(frame.loc[frame["surface_temp_peak_c"].idxmax(), "segment_id"]),
            ]
            cursor.execute(
                """
                SELECT segment_id, surface_temp_09_c, surface_temp_12_c,
                       surface_temp_15_c, surface_temp_18_c, surface_temp_peak_c,
                       thermal_model_confidence, thermal_weather_date
                FROM route_segment
                WHERE segment_id = ANY(%s)
                ORDER BY segment_id
                """,
                (representative_ids,),
            )
            representative_rows = [tuple(row) for row in cursor.fetchall()]
    return {
        "identity": identity,
        "verification": verification,
        "flyway_v7": tuple(flyway_v7),
        "representative_rows": representative_rows,
    }


def write_report(frame: pd.DataFrame, result: dict[str, Any], path: Path) -> None:
    """D5 완료 범위와 커밋 후 DB 검증 증거를 Markdown으로 기록한다."""
    verification = result["committed"]["verification"]
    statistics = verification["temperature_statistics"]
    statistic_rows = "\n".join(
        f"| {hour} | {values['min']:.2f} | {values['mean']:.2f} | {values['max']:.2f} |"
        for hour, values in statistics.items()
    )
    representative_rows = "\n".join(
        f"| {row[0]} | {float(row[1]):.2f} | {float(row[2]):.2f} | {float(row[3]):.2f} | "
        f"{float(row[4]):.2f} | {float(row[5]):.2f} | {row[6]} | {row[7]} |"
        for row in result["committed"]["representative_rows"]
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"""# D5 완료 및 DB 적재 검증

## 실행 결과

- 판정: **PASS**
- 실행 시각: {datetime.now().astimezone().isoformat(timespec='seconds')}
- 입력: `data/processed/d5/route_thermal.csv`
- CSV / DB 링크: {len(frame):,} / {result['committed']['identity']['database_rows']:,}
- UPDATE 행: {result['applied']['updated_rows']:,}
- DB/CSV ID 불일치: {result['committed']['identity']['id_mismatches']}
- D4 재질 불일치: {result['committed']['identity']['surface_mismatches']}
- 기존 네트워크·D4 필드 변경 행: {result['applied']['d4_changed_rows']}
- 온도 NULL 행: {verification['null_rows']}
- peak 계산 오류: {verification['peak_errors']}
- 기상일·신뢰도 불일치: {verification['metadata_mismatches']}
- CSV/DB 온도 불일치: {verification['csv_mismatches']}
- CSV/DB 최대 온도 오차: {verification['maximum_error_c']:.6f}°C
- 기상 기준일: 2026-08-11
- 모델 신뢰도: `LOW`
- Flyway: V7 `{result['committed']['flyway_v7'][1]}` 성공

## DB 온도 통계

| 시각 | 최소 | 평균 | 최대 |
|---|---:|---:|---:|
{statistic_rows}

## 대표 링크 CSV·DB 대조

| segment_id | 09시 | 12시 | 15시 | 18시 | peak | 신뢰도 | 기상일 |
|---:|---:|---:|---:|---:|---:|---|---|
{representative_rows}

## 트랜잭션 정책

- 임시 staging 7,766행과 DB ID 집합이 완전히 일치할 때만 UPDATE한다.
- D4 재질이 CSV와 다르면 적재 전에 실패한다.
- 네트워크·geometry·D4 재질·SVF·물성·공원거리·그늘 필드를 스냅샷과 비교한다.
- UPDATE 수 또는 사후 검사가 하나라도 실패하면 연결 context가 전체 트랜잭션을 롤백한다.
- 커밋 후 새 연결에서 CSV 7,766행 전체를 DB와 다시 비교했다.

현재 단계에서는 샘플별 온도를 DB에 중복 저장하지 않았으며 링크 집계 온도만 적재했다.
""", encoding="utf-8")


def main() -> None:
    """CSV를 검증하고 선택적으로 적재한 뒤 커밋 상태를 보고한다."""
    args = parse_arguments()
    if not args.input.is_file():
        raise FileNotFoundError(f"D5 링크 온도 CSV가 없습니다: {args.input}")
    frame = validate_thermal_frame(pd.read_csv(args.input, encoding="utf-8-sig"))
    db_config = build_db_config()
    print(f"[사전 검증 통과] D5 링크 {len(frame):,}행")
    print(f"[DB 연결] {db_config['host']}:{db_config['port']}/{db_config['dbname']}")
    if not args.apply:
        print("[안내] dry-run입니다. 실제 반영은 --apply를 사용하세요.")
        return

    applied = apply_thermal(frame, db_config)
    print(f"[트랜잭션 반영 완료] UPDATE {applied['updated_rows']:,}행")
    committed = verify_committed_database(frame, db_config)
    result = {"applied": applied, "committed": committed}
    write_report(frame, result, args.report.resolve())
    print("[커밋 후 DB 검증 통과]")
    print(f"- D4 변경 행: {applied['d4_changed_rows']}")
    print(f"- CSV/DB 온도 불일치: {committed['verification']['csv_mismatches']}")
    print(f"- 최대 온도 오차: {committed['verification']['maximum_error_c']:.6f}°C")
    print(f"- 보고서: {args.report.resolve()}")


if __name__ == "__main__":
    main()
