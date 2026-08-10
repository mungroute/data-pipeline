from collections import Counter
from pathlib import Path

import geopandas as gpd
import pandas as pd


# prepare_network.py가 있는 src 폴더의 상위 폴더(data-pipeline)를 기준으로
# 원본 도보망 CSV 경로를 구성한다.
PIPELINE_ROOT = Path(__file__).resolve().parents[1]
RAW_NETWORK_PATH = (
    PIPELINE_ROOT
    / "data"
    / "raw"
    / "network"
    / "seoul_walk_network.csv"
)
INTERIM_NETWORK_DIR = PIPELINE_ROOT / "data" / "interim" / "network"
INTERIM_NETWORK_PATH = INTERIM_NETWORK_DIR / "junggu_walk_network_5186.gpkg"

# 이후 전처리에 반드시 필요한 원본 CSV 컬럼이다.
REQUIRED_COLUMNS = {
    "노드링크 유형",
    "노드 WKT",
    "노드 ID",
    "노드 유형 코드",
    "링크 WKT",
    "링크 ID",
    "링크 유형 코드",
    "시작노드 ID",
    "종료노드 ID",
    "링크 길이",
    "시군구명",
}


def read_header() -> list[str]:
    """원본 CSV의 데이터는 읽지 않고 헤더만 읽어 컬럼 목록을 반환한다."""
    header = pd.read_csv(
        RAW_NETWORK_PATH,
        encoding="cp949",
        nrows=0,
    )
    return list(header.columns)


def validate_required_columns(columns: list[str]) -> None:
    """원본 CSV에 전처리 필수 컬럼이 모두 존재하는지 검사한다."""
    missing_columns = REQUIRED_COLUMNS - set(columns)

    if missing_columns:
        missing = ", ".join(sorted(missing_columns))
        raise ValueError(f"필수 컬럼이 없습니다: {missing}")


def print_input_information(columns: list[str]) -> None:
    """입력 파일 경로, 크기, 인코딩과 전체 컬럼을 출력한다."""
    print(f"[입력 파일] {RAW_NETWORK_PATH}")
    print(f"[파일 크기] {RAW_NETWORK_PATH.stat().st_size:,} bytes")
    print("[인코딩] CP949")
    print(f"[컬럼 수] {len(columns)}")

    for index, column in enumerate(columns, start=1):
        print(f"  {index:02d}. {column}")


def inspect_network_rows() -> dict:
    """
    CSV를 여러 조각으로 나누어 읽으면서 중구 NODE/LINK 행을 검사한다.

    원본 CSV가 약 112MB이므로 전체를 한 번에 메모리에 올리지 않고
    100,000행 단위로 나누어 처리한다. 중구 행만 필터링
    """
    total_count = 0
    junggu_count = 0
    node_count = 0
    link_count = 0
    walkable_link_count = 0
    unwalkable_link_count = 0
    node_wkt_count = 0
    link_wkt_count = 0
    link_type_counts = Counter()

    # dtype=str로 읽어 ID와 유형 코드가 숫자로 자동 변환되는 것을 방지한다.
    # keep_default_na=False를 사용해 빈 문자열을 NaN으로 바꾸지 않는다.
    chunks = pd.read_csv(
        RAW_NETWORK_PATH,
        encoding="cp949",
        dtype=str,
        keep_default_na=False,
        chunksize=100_000,
    )

    for chunk in chunks:
        total_count += len(chunk)

        # 서울시 전체 데이터에서 중구 행만 추출한다.
        junggu = chunk.loc[
            chunk["시군구명"].str.strip() == "중구"
        ].copy()
        junggu_count += len(junggu)

        # NODE와 LINK 구분값의 앞뒤 공백을 제거하고 대문자로 통일한다.
        row_types = junggu["노드링크 유형"].str.strip().str.upper()

        nodes = junggu.loc[row_types == "NODE"]
        links = junggu.loc[row_types == "LINK"]

        node_count += len(nodes)
        link_count += len(links)

        # 링크 유형 코드 첫 자리가 1이면 보행 가능,
        # 0이면 보행 불가 링크로 분류한다.
        link_type_codes = links["링크 유형 코드"].str.strip()
        walkable_mask = link_type_codes.str.startswith("1")
        unwalkable_mask = link_type_codes.str.startswith("0")

        walkable_link_count += int(walkable_mask.sum())
        unwalkable_link_count += int(unwalkable_mask.sum())

        # 0 또는 1로 시작하지 않는 코드는 임의로 처리하지 않고 중단한다.
        unknown_link_mask = ~(walkable_mask | unwalkable_mask)
        if unknown_link_mask.any():
            unknown_codes = sorted(
                link_type_codes.loc[unknown_link_mask].unique()
            )
            raise ValueError(
                f"판정할 수 없는 링크 유형 코드가 있습니다: {unknown_codes}"
            )

        # NODE 행에는 노드 WKT가, LINK 행에는 링크 WKT가 있어야 한다.
        node_wkt_count += int(
            nodes["노드 WKT"].str.strip().ne("").sum()
        )
        link_wkt_count += int(
            links["링크 WKT"].str.strip().ne("").sum()
        )

        # 다음 단계에서 보행 불가 링크를 판정할 수 있도록
        # 중구 LINK의 유형 코드별 개수를 집계한다.
        chunk_link_type_counts = (
            links["링크 유형 코드"]
            .str.strip()
            .replace("", "(빈값)")
            .value_counts()
        )

        link_type_counts.update(
            {
                code: int(count)
                for code, count in chunk_link_type_counts.items()
            }
        )

    return {
        "total_count": total_count,
        "junggu_count": junggu_count,
        "node_count": node_count,
        "link_count": link_count,
        "walkable_link_count": walkable_link_count,
        "unwalkable_link_count": unwalkable_link_count,
        "node_wkt_count": node_wkt_count,
        "link_wkt_count": link_wkt_count,
        "link_type_counts": link_type_counts,
    }


def load_junggu_rows() -> pd.DataFrame:
    """원본 CSV를 청크 단위로 읽어 중구 행만 메모리에 적재한다."""
    junggu_chunks: list[pd.DataFrame] = []

    chunks = pd.read_csv(
        RAW_NETWORK_PATH,
        encoding="cp949",
        dtype=str,
        keep_default_na=False,
        chunksize=100_000,
    )

    for chunk in chunks:
        # 원본 문자열에 포함될 수 있는 앞뒤 공백을 제거한 뒤 중구만 남긴다.
        junggu = chunk.loc[
            chunk["시군구명"].str.strip() == "중구"
        ].copy()
        junggu_chunks.append(junggu)

    if not junggu_chunks:
        raise ValueError("원본 CSV에서 중구 데이터를 찾지 못했습니다.")

    return pd.concat(junggu_chunks, ignore_index=True)


def build_nodes(junggu_rows: pd.DataFrame) -> gpd.GeoDataFrame:
    """중구 NODE의 WKT를 Point로 파싱하고 EPSG:5186으로 변환한다."""
    row_types = junggu_rows["노드링크 유형"].str.strip().str.upper()
    node_rows = junggu_rows.loc[row_types == "NODE"].reset_index(drop=True)

    # 원본 WKT는 경도·위도 순서의 WGS84(EPSG:4326) 좌표다.
    geometry = gpd.GeoSeries.from_wkt(
        node_rows["노드 WKT"],
        crs="EPSG:4326",
        on_invalid="raise",
    )

    nodes = gpd.GeoDataFrame(
        {
            "original_node_id": pd.to_numeric(
                node_rows["노드 ID"], errors="raise"
            ).astype("int64"),
            "node_type_code": node_rows["노드 유형 코드"].str.strip(),
            "district_code": node_rows["시군구코드"].str.strip(),
            "district_name": node_rows["시군구명"].str.strip(),
            "neighborhood_code": node_rows["읍면동코드"].str.strip(),
            "neighborhood_name": node_rows["읍면동명"].str.strip(),
        },
        geometry=geometry,
        crs="EPSG:4326",
    )

    return nodes.to_crs("EPSG:5186")


def build_walkable_links(junggu_rows: pd.DataFrame) -> gpd.GeoDataFrame:
    """보행 가능한 중구 LINK만 남겨 LineString으로 파싱하고 변환한다."""
    row_types = junggu_rows["노드링크 유형"].str.strip().str.upper()
    link_rows = junggu_rows.loc[row_types == "LINK"].copy()

    # 링크 유형 코드 첫 자리가 1인 링크만 보행 가능 링크로 사용한다.
    link_type_codes = link_rows["링크 유형 코드"].str.strip()
    link_rows = link_rows.loc[
        link_type_codes.str.startswith("1")
    ].reset_index(drop=True)

    geometry = gpd.GeoSeries.from_wkt(
        link_rows["링크 WKT"],
        crs="EPSG:4326",
        on_invalid="raise",
    )

    links = gpd.GeoDataFrame(
        {
            "original_link_id": pd.to_numeric(
                link_rows["링크 ID"], errors="raise"
            ).astype("int64"),
            "link_type_code": link_rows["링크 유형 코드"].str.strip(),
            # 원본 노드 참조는 QA용으로 보존만 하고 source/target으로 쓰지 않는다.
            "original_start_node_id": pd.to_numeric(
                link_rows["시작노드 ID"], errors="raise"
            ).astype("int64"),
            "original_end_node_id": pd.to_numeric(
                link_rows["종료노드 ID"], errors="raise"
            ).astype("int64"),
            "original_length_m": pd.to_numeric(
                link_rows["링크 길이"], errors="coerce"
            ),
            "district_code": link_rows["시군구코드"].str.strip(),
            "district_name": link_rows["시군구명"].str.strip(),
            "neighborhood_code": link_rows["읍면동코드"].str.strip(),
            "neighborhood_name": link_rows["읍면동명"].str.strip(),
            "is_elevated": link_rows["고가도로"].str.strip(),
            "is_subway_network": link_rows["지하철네트워크"].str.strip(),
            "is_bridge": link_rows["교량"].str.strip(),
            "is_tunnel": link_rows["터널"].str.strip(),
            "is_overpass": link_rows["육교"].str.strip(),
            "is_crosswalk": link_rows["횡단보도"].str.strip(),
            "is_park_green": link_rows["공원,녹지"].str.strip(),
            "is_indoor": link_rows["건물내"].str.strip(),
        },
        geometry=geometry,
        crs="EPSG:4326",
    ).to_crs("EPSG:5186")

    # 원본 길이는 보존하고, 변환된 미터 좌표계에서 길이를 다시 계산한다.
    links["calculated_length_m"] = links.geometry.length
    return links


def validate_spatial_data(
    nodes: gpd.GeoDataFrame,
    links: gpd.GeoDataFrame,
) -> None:
    """공간 변환 결과의 행 수, CRS, geometry 종류와 유효성을 검사한다."""
    if len(nodes) != 5_825:
        raise ValueError(f"NODE 수가 예상과 다릅니다: {len(nodes):,}")
    if len(links) != 7_771:
        raise ValueError(f"보행 가능 LINK 수가 예상과 다릅니다: {len(links):,}")

    if nodes.crs is None or nodes.crs.to_epsg() != 5186:
        raise ValueError(f"NODE CRS가 EPSG:5186이 아닙니다: {nodes.crs}")
    if links.crs is None or links.crs.to_epsg() != 5186:
        raise ValueError(f"LINK CRS가 EPSG:5186이 아닙니다: {links.crs}")

    if not nodes.geometry.geom_type.eq("Point").all():
        raise ValueError("NODE에 Point가 아닌 geometry가 있습니다.")
    if not links.geometry.geom_type.eq("LineString").all():
        raise ValueError("LINK에 LineString이 아닌 geometry가 있습니다.")

    if nodes.geometry.is_empty.any() or nodes.geometry.isna().any():
        raise ValueError("NODE에 비어 있거나 누락된 geometry가 있습니다.")
    if links.geometry.is_empty.any() or links.geometry.isna().any():
        raise ValueError("LINK에 비어 있거나 누락된 geometry가 있습니다.")
    if not nodes.geometry.is_valid.all():
        raise ValueError("NODE에 유효하지 않은 geometry가 있습니다.")
    if not links.geometry.is_valid.all():
        raise ValueError("LINK에 유효하지 않은 geometry가 있습니다.")


def write_interim_network(
    nodes: gpd.GeoDataFrame,
    links: gpd.GeoDataFrame,
) -> None:
    """변환된 NODE와 LINK를 하나의 중간 GeoPackage에 레이어로 저장한다."""
    INTERIM_NETWORK_DIR.mkdir(parents=True, exist_ok=True)

    # 다시 실행할 때 이전 산출물과 레이어가 섞이지 않도록 새로 생성한다.
    if INTERIM_NETWORK_PATH.exists():
        INTERIM_NETWORK_PATH.unlink()

    nodes.to_file(
        INTERIM_NETWORK_PATH,
        layer="nodes",
        driver="GPKG",
        index=False,
    )
    links.to_file(
        INTERIM_NETWORK_PATH,
        layer="walkable_links",
        driver="GPKG",
        mode="a",
        index=False,
    )


def print_spatial_result(
    nodes: gpd.GeoDataFrame,
    links: gpd.GeoDataFrame,
) -> None:
    """공간 변환 및 중간 파일 저장 결과를 출력한다."""
    print()
    print("[공간 변환 결과]")
    print(f"  NODE: {len(nodes):,}")
    print(f"  보행 가능 LINK: {len(links):,}")
    print(f"  CRS: {links.crs}")
    print(f"  NODE geometry: {nodes.geometry.geom_type.unique().tolist()}")
    print(f"  LINK geometry: {links.geometry.geom_type.unique().tolist()}")
    print(f"  LINK 계산 길이 합계: {links['calculated_length_m'].sum() / 1000:,.3f} km")
    print(f"  중간 파일: {INTERIM_NETWORK_PATH}")


def print_inspection_result(result: dict) -> None:
    """도보망 원본 검사 결과를 사람이 확인하기 좋은 형식으로 출력한다."""
    print()
    print("[행 수]")
    print(f"  서울 전체: {result['total_count']:,}")
    print(f"  중구 전체: {result['junggu_count']:,}")
    print(f"  중구 NODE: {result['node_count']:,}")
    print(f"  중구 LINK: {result['link_count']:,}")

    print()
    print("[보행 가능 여부]")
    print(f"  보행 가능 LINK: {result['walkable_link_count']:,}")
    print(f"  보행 불가 LINK: {result['unwalkable_link_count']:,}")

    print()
    print("[WKT 확인]")
    print(
        f"  NODE WKT 존재: "
        f"{result['node_wkt_count']:,} / {result['node_count']:,}"
    )
    print(
        f"  LINK WKT 존재: "
        f"{result['link_wkt_count']:,} / {result['link_count']:,}"
    )

    print()
    print("[중구 링크 유형 코드]")

    link_type_counts = result["link_type_counts"]
    for code, count in sorted(link_type_counts.items()):
        print(f"  {code}: {count:,}")

    # 원본에 NODE와 LINK 이외의 유형이 있는지 확인한다.
    known_count = result["node_count"] + result["link_count"]
    unknown_count = result["junggu_count"] - known_count

    if unknown_count:
        print(f"\n[경고] NODE/LINK 이외 유형: {unknown_count:,}")


def main() -> None:
    """D1 도보망 전처리의 원본 파일 존재 여부와 입력 구조를 검사한다."""
    if not RAW_NETWORK_PATH.exists():
        raise FileNotFoundError(
            f"원본 도보망 파일이 없습니다: {RAW_NETWORK_PATH}"
        )

    columns = read_header()

    validate_required_columns(columns)
    print_input_information(columns)

    result = inspect_network_rows()
    print_inspection_result(result)

    junggu_rows = load_junggu_rows()
    nodes = build_nodes(junggu_rows)
    links = build_walkable_links(junggu_rows)

    validate_spatial_data(nodes, links)
    write_interim_network(nodes, links)
    print_spatial_result(nodes, links)

    print("\n[D1 도보망 정제 착수 완료]")


if __name__ == "__main__":
    main()
