from __future__ import annotations

import argparse

import psycopg2

from db_config import add_target_argument, build_db_config, describe_db_target


REQUIRED_TABLES = (
    "flyway_schema_history",
    "route_segment",
    "segment_sample_point",
    "route_vertex",
    "route_segment_staging",
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="DB 연결, SSL, GIS 확장과 Flyway 테이블을 읽기 전용으로 확인"
    )
    add_target_argument(parser)
    args = parser.parse_args()
    db_config = build_db_config(args.target)
    print(f"[DB 연결 대상] {describe_db_target(args.target, db_config)}")

    with psycopg2.connect(**db_config) as connection:
        connection.set_session(readonly=True)
        ssl_enabled = bool(getattr(connection.info, "ssl_in_use", False))
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT current_database(), current_user,
                       inet_server_addr()::text,
                       current_setting('server_version')
                """
            )
            database, user, server_address, server_version = cursor.fetchone()

            cursor.execute(
                "SELECT extname, extversion FROM pg_extension "
                "WHERE extname IN ('postgis', 'pgrouting') ORDER BY extname"
            )
            extensions = dict(cursor.fetchall())

            cursor.execute(
                "SELECT EXISTS (SELECT 1 FROM pg_proc WHERE proname = 'pgr_extractvertices')"
            )
            has_extract_vertices = bool(cursor.fetchone()[0])

            cursor.execute(
                "SELECT name, to_regclass('public.' || name) IS NOT NULL "
                "FROM unnest(%s::text[]) AS name",
                (list(REQUIRED_TABLES),),
            )
            table_status = dict(cursor.fetchall())

            flyway_version = None
            if table_status["flyway_schema_history"]:
                cursor.execute(
                    "SELECT MAX(version) FROM flyway_schema_history WHERE success"
                )
                flyway_version = cursor.fetchone()[0]

            table_counts: dict[str, int] = {}
            for table in REQUIRED_TABLES[1:]:
                if table_status[table]:
                    cursor.execute(f'SELECT COUNT(*) FROM "{table}"')
                    table_counts[table] = int(cursor.fetchone()[0])

    print(
        f"[연결 성공] database={database}, user={user}, "
        f"server={server_address or 'managed'}, PostgreSQL={server_version}"
    )
    print(f"[SSL] {'사용 중' if ssl_enabled else '사용 안 함'}")
    print(
        "[확장] "
        f"PostGIS={extensions.get('postgis', 'MISSING')}, "
        f"pgRouting={extensions.get('pgrouting', 'MISSING')}"
    )
    print(f"[pgRouting 함수] pgr_extractVertices: {'OK' if has_extract_vertices else 'MISSING'}")
    print(f"[Flyway] latest={flyway_version or 'MISSING'}")
    for table, exists in table_status.items():
        count_text = f", rows={table_counts[table]:,}" if table in table_counts else ""
        print(f"[테이블] {table}: {'OK' if exists else 'MISSING'}{count_text}")

    failures: list[str] = []
    if args.target == "dev" and not ssl_enabled:
        failures.append("SSL")
    failures.extend(
        extension for extension in ("postgis", "pgrouting") if extension not in extensions
    )
    if not has_extract_vertices:
        failures.append("pgr_extractVertices")
    failures.extend(table for table, exists in table_status.items() if not exists)
    if failures:
        raise RuntimeError(
            "필수 DB 준비가 완료되지 않았습니다: " + ", ".join(failures)
            + ". 백엔드 Flyway migrations를 먼저 실행하세요."
        )


if __name__ == "__main__":
    main()
