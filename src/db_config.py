from __future__ import annotations

import argparse
import os
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit


PIPELINE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PIPELINE_ROOT.parent
BACKEND_ENV_PATH = PROJECT_ROOT / "backend" / ".env"
DEV_ENV_PATH = PIPELINE_ROOT / ".env.dev"
DB_TARGETS = ("local", "dev")


def read_env_file(path: Path) -> dict[str, str]:
    """간단한 KEY=VALUE 형식의 env 파일을 읽는다."""
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


def add_target_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--target",
        choices=DB_TARGETS,
        default="local",
        help="DB 대상. 기본값은 local이며 dev를 명시해야만 Supabase를 사용합니다.",
    )


def add_replace_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--allow-replace",
        action="store_true",
        help="dev DB에 기존 파생 데이터가 있을 때만 명시적으로 교체를 허용합니다.",
    )


def _build_local_config() -> dict[str, str | int]:
    env_settings = read_env_file(BACKEND_ENV_PATH)
    config: dict[str, str | int] = {
        "host": os.getenv("POSTGRES_HOST", "localhost"),
        "port": int(os.getenv("POSTGRES_PORT", env_settings.get("POSTGRES_PORT", "15432"))),
        "dbname": os.getenv("POSTGRES_DB", env_settings.get("POSTGRES_DB", "")),
        "user": os.getenv("POSTGRES_USER", env_settings.get("POSTGRES_USER", "")),
        "password": os.getenv("POSTGRES_PASSWORD", env_settings.get("POSTGRES_PASSWORD", "")),
    }
    missing = [key for key in ("dbname", "user", "password") if not config[key]]
    if missing:
        raise ValueError(f"DB 설정값이 없습니다: {', '.join(missing)}")
    return config


def _validate_dev_url(database_url: str) -> None:
    parsed = urlsplit(database_url)
    if parsed.scheme not in {"postgresql", "postgres"}:
        raise ValueError("DEV_DATABASE_URL은 postgresql:// URI여야 합니다.")
    if not parsed.hostname or not parsed.username or not parsed.path.strip("/"):
        raise ValueError("DEV_DATABASE_URL에 host, user, database가 모두 필요합니다.")
    if not parsed.password:
        raise ValueError("DEV_DATABASE_URL에 password가 없습니다.")
    sslmode = parse_qs(parsed.query).get("sslmode", [""])[0].lower()
    if sslmode != "require":
        raise ValueError("dev DB는 DEV_DATABASE_URL에 sslmode=require가 필요합니다.")


def build_db_config(target: str = "local") -> dict[str, str | int]:
    """명시된 target의 psycopg2 연결 설정을 만든다. 기본값은 항상 local이다."""
    if target == "local":
        return _build_local_config()
    if target != "dev":
        raise ValueError(f"지원하지 않는 DB target입니다: {target}")

    settings = read_env_file(DEV_ENV_PATH)
    database_url = os.getenv("DEV_DATABASE_URL", settings.get("DEV_DATABASE_URL", "")).strip()
    if not database_url:
        raise ValueError(f"DEV_DATABASE_URL이 없습니다: {DEV_ENV_PATH}")
    _validate_dev_url(database_url)
    return {"dsn": database_url}


def describe_db_target(target: str, config: dict[str, str | int]) -> str:
    """비밀번호와 전체 URI를 제외한 연결 대상만 로그 문자열로 만든다."""
    if target == "dev":
        parsed = urlsplit(str(config["dsn"]))
        query = parse_qs(parsed.query)
        host = parsed.hostname or ""
        port = parsed.port or 5432
        database = parsed.path.lstrip("/")
        user = unquote(parsed.username or "")
        sslmode = query.get("sslmode", ["unspecified"])[0]
    else:
        host = str(config["host"])
        port = int(config["port"])
        database = str(config["dbname"])
        user = str(config["user"])
        sslmode = "local/default"
    return (
        f"target={target}, host={host}, port={port}, database={database}, "
        f"user={user}, sslmode={sslmode}"
    )


def require_dev_replace_permission(
    target: str,
    existing_rows: int,
    allow_replace: bool,
    resource: str,
) -> None:
    if target == "dev" and existing_rows > 0 and not allow_replace:
        raise RuntimeError(
            f"dev DB의 기존 {resource} {existing_rows:,}행을 교체하려면 "
            "--allow-replace를 명시하세요."
        )
