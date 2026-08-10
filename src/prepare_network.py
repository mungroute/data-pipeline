from collections import Counter
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
from scipy.spatial import cKDTree
from shapely.geometry import LineString


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
PROCESSED_NETWORK_DIR = PIPELINE_ROOT / "data" / "processed" / "network"
SNAPPED_NETWORK_PATH = (
    PROCESSED_NETWORK_DIR / "junggu_walk_network_snapped.gpkg"
)

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


def extract_link_endpoints(
    links: gpd.GeoDataFrame,
) -> pd.DataFrame:
    """
    각 LINK의 시작점과 끝점을 추출해 endpoint 테이블을 만든다.

    원본 NODE ID는 topology 구성에 사용하지 않고, EPSG:5186으로
    변환된 실제 LineString의 양 끝 좌표를 사용한다.
    """

    endpoint_rows = []

    for link_index, link in links.iterrows():
        coordinates = list(link.geometry.coords)

        start_x, start_y = coordinates[0]
        end_x, end_y = coordinates[-1]

        endpoint_rows.append(
            {
                "link_index": link_index,
                "original_link_id": link["original_link_id"],
                "endpoint_type": "start",
                "x": start_x,
                "y": start_y,
            }
        )

        endpoint_rows.append(
            {
                "link_index": link_index,
                "original_link_id": link["original_link_id"],
                "endpoint_type": "end",
                "x": end_x,
                "y": end_y,
            }
        )

    endpoints = pd.DataFrame(endpoint_rows)

    expected_count = len(links) * 2
    if len(endpoints) != expected_count:
        raise ValueError(
            f"endpoint 수가 올바르지 않습니다: "
            f"{len(endpoints):,} / 예상 {expected_count:,}"
        )

    return endpoints


def group_endpoints_within_tolerance(
    endpoints: pd.DataFrame,
    tolerance_m: float = 1.0,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    지정 거리 이내의 endpoint를 동일한 스냅 그룹으로 묶는다.

    그룹 대표 좌표는 실제 endpoint 중 하나를 사용하며, snap_group_id는
    Python 전처리용 식별자일 뿐 DB의 route_vertex.vertex_id가 아니다.
    """
    if tolerance_m <= 0:
        raise ValueError("스냅 허용오차는 0보다 커야 합니다.")
    if endpoints.empty:
        raise ValueError("그룹화할 endpoint가 없습니다.")

    grouped = endpoints.copy()
    coordinates = grouped[["x", "y"]].to_numpy(dtype=float)
    endpoint_count = len(coordinates)

    # 반경 검색을 반복해도 빠르게 가까운 endpoint를 찾도록 공간 트리를 만든다.
    tree = cKDTree(coordinates)

    # x, y, 원래 행 번호 순으로 처리하여 반복 실행 결과를 고정한다.
    processing_order = np.lexsort(
        (
            np.arange(endpoint_count),
            coordinates[:, 1],
            coordinates[:, 0],
        )
    )

    assignments = np.full(endpoint_count, -1, dtype=np.int64)
    canonical_coordinates: list[np.ndarray] = []

    for endpoint_index in processing_order:
        if assignments[endpoint_index] != -1:
            continue

        snap_group_id = len(canonical_coordinates)
        canonical_coordinate = coordinates[endpoint_index].copy()
        canonical_coordinates.append(canonical_coordinate)

        # 대표 좌표에서 tolerance_m 이내인 미배정 endpoint만 같은 그룹이 된다.
        neighbor_indexes = tree.query_ball_point(
            canonical_coordinate,
            r=tolerance_m,
        )

        for neighbor_index in sorted(neighbor_indexes):
            if assignments[neighbor_index] == -1:
                assignments[neighbor_index] = snap_group_id

    canonical_array = np.asarray(canonical_coordinates)

    grouped["snap_group_id"] = assignments
    grouped["canonical_x"] = canonical_array[assignments, 0]
    grouped["canonical_y"] = canonical_array[assignments, 1]

    # 원래 endpoint가 대표 좌표까지 실제로 이동할 거리를 계산한다.
    grouped["snap_distance_m"] = np.hypot(
        grouped["x"] - grouped["canonical_x"],
        grouped["y"] - grouped["canonical_y"],
    )

    maximum_distance = grouped["snap_distance_m"].max()
    if maximum_distance > tolerance_m + 1e-9:
        raise ValueError(
            f"스냅 이동거리가 {tolerance_m}m를 초과했습니다: "
            f"{maximum_distance:.6f}m"
        )

    canonical_endpoints = pd.DataFrame(
        {
            "snap_group_id": np.arange(len(canonical_array)),
            "x": canonical_array[:, 0],
            "y": canonical_array[:, 1],
        }
    )

    return grouped, canonical_endpoints


def apply_canonical_endpoints_to_links(
    links: gpd.GeoDataFrame,
    grouped_endpoints: pd.DataFrame,
) -> tuple[gpd.GeoDataFrame, dict]:
    """
    각 LineString의 시작점과 끝점을 스냅 그룹의 대표 좌표로 치환한다.

    LineString 내부 vertex와 원본 속성은 그대로 보존한다. 원래부터 닫혀
    있던 선은 유지하지만, 스냅 때문에 새로운 폐합 선이나 0m 선이 생기면
    잘못된 topology가 되므로 검사를 중단한다.
    """
    expected_endpoint_count = len(links) * 2
    if len(grouped_endpoints) != expected_endpoint_count:
        raise ValueError(
            "LINK와 endpoint 수가 일치하지 않습니다: "
            f"{len(links):,} LINK, {len(grouped_endpoints):,} endpoint"
        )

    endpoint_keys = grouped_endpoints.set_index(
        ["link_index", "endpoint_type"]
    )
    if not endpoint_keys.index.is_unique:
        raise ValueError("LINK별 start/end endpoint 키가 중복되었습니다.")

    snapped_geometries: list[LineString] = []
    original_closed_flags: list[bool] = []

    for link_index, geometry in links.geometry.items():
        coordinates = list(geometry.coords)
        original_closed_flags.append(coordinates[0] == coordinates[-1])

        try:
            start_endpoint = endpoint_keys.loc[(link_index, "start")]
            end_endpoint = endpoint_keys.loc[(link_index, "end")]
        except KeyError as error:
            raise ValueError(
                f"LINK {link_index}의 start/end endpoint가 없습니다."
            ) from error

        # 내부 좌표는 건드리지 않고 양 끝 좌표만 canonical 좌표로 교체한다.
        coordinates[0] = (
            float(start_endpoint["canonical_x"]),
            float(start_endpoint["canonical_y"]),
        )
        coordinates[-1] = (
            float(end_endpoint["canonical_x"]),
            float(end_endpoint["canonical_y"]),
        )
        snapped_geometries.append(LineString(coordinates))

    snapped_links = links.copy()
    snapped_links = snapped_links.set_geometry(
        gpd.GeoSeries(
            snapped_geometries,
            index=links.index,
            crs=links.crs,
        )
    )

    original_lengths = links.geometry.length
    snapped_lengths = snapped_links.geometry.length
    snapped_links["calculated_length_m"] = snapped_lengths

    snapped_closed_flags = np.asarray(
        [
            geometry.coords[0] == geometry.coords[-1]
            for geometry in snapped_links.geometry
        ]
    )
    original_closed_array = np.asarray(original_closed_flags)
    new_closed_count = int(
        (snapped_closed_flags & ~original_closed_array).sum()
    )
    zero_length_count = int((snapped_lengths <= 0).sum())

    if new_closed_count:
        raise ValueError(
            f"스냅으로 새로운 폐합 LINK가 {new_closed_count:,}개 생겼습니다."
        )
    if zero_length_count:
        raise ValueError(
            f"스냅으로 0m LINK가 {zero_length_count:,}개 생겼습니다."
        )
    if snapped_links.geometry.is_empty.any():
        raise ValueError("스냅 후 비어 있는 LINK geometry가 있습니다.")
    if not snapped_links.geometry.is_valid.all():
        raise ValueError("스냅 후 유효하지 않은 LINK geometry가 있습니다.")

    statistics = {
        "link_count": len(snapped_links),
        "changed_link_count": int(
            grouped_endpoints.loc[
                grouped_endpoints["snap_distance_m"] > 0,
                "link_index",
            ].nunique()
        ),
        "original_closed_count": int(original_closed_array.sum()),
        "snapped_closed_count": int(snapped_closed_flags.sum()),
        "new_closed_count": new_closed_count,
        "zero_length_count": zero_length_count,
        "maximum_length_change_m": float(
            (snapped_lengths - original_lengths).abs().max()
        ),
    }

    return snapped_links, statistics


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


def build_snapped_output(
    snapped_links: gpd.GeoDataFrame,
) -> gpd.GeoDataFrame:
    """스냅 LINK를 route_segment_staging 적재용 컬럼 구조로 정리한다."""
    output = snapped_links.copy()

    # 원본 LINK ID는 중구 보행망 안에서 유일한 경우 segment_id로 보존한다.
    if not output["original_link_id"].is_unique:
        raise ValueError("원본 LINK ID가 중복되어 segment_id로 사용할 수 없습니다.")

    output["segment_id"] = output["original_link_id"].astype("int64")
    output["length_m"] = output.geometry.length

    output_columns = [
        "segment_id",
        "link_type_code",
        "original_start_node_id",
        "original_end_node_id",
        "length_m",
        "geometry",
    ]
    return output[output_columns]


def write_snapped_network(snapped_output: gpd.GeoDataFrame) -> None:
    """staging 적재 전 스냅 LINK를 processed GeoPackage로 저장한다."""
    PROCESSED_NETWORK_DIR.mkdir(parents=True, exist_ok=True)

    # 재실행 결과가 이전 레이어와 섞이지 않도록 파생 파일만 새로 만든다.
    if SNAPPED_NETWORK_PATH.exists():
        SNAPPED_NETWORK_PATH.unlink()

    snapped_output.to_file(
        SNAPPED_NETWORK_PATH,
        layer="snapped_links",
        driver="GPKG",
        index=False,
    )


def validate_snapped_network_file(expected_count: int) -> gpd.GeoDataFrame:
    """저장한 processed GeoPackage를 다시 읽어 행·컬럼·공간값을 검사한다."""
    saved_links = gpd.read_file(
        SNAPPED_NETWORK_PATH,
        layer="snapped_links",
    )

    required_columns = {
        "segment_id",
        "link_type_code",
        "original_start_node_id",
        "original_end_node_id",
        "length_m",
        "geometry",
    }
    missing_columns = required_columns - set(saved_links.columns)

    if missing_columns:
        raise ValueError(
            "스냅 파일에 필수 컬럼이 없습니다: "
            + ", ".join(sorted(missing_columns))
        )
    if len(saved_links) != expected_count:
        raise ValueError(
            f"스냅 파일 행 수가 다릅니다: {len(saved_links):,} / "
            f"예상 {expected_count:,}"
        )
    if not saved_links["segment_id"].is_unique:
        raise ValueError("스냅 파일의 segment_id가 중복되었습니다.")
    if saved_links.crs is None or saved_links.crs.to_epsg() != 5186:
        raise ValueError(f"스냅 파일 CRS가 EPSG:5186이 아닙니다: {saved_links.crs}")
    if not saved_links.geometry.geom_type.eq("LineString").all():
        raise ValueError("스냅 파일에 LineString이 아닌 geometry가 있습니다.")
    if saved_links.geometry.is_empty.any() or saved_links.geometry.isna().any():
        raise ValueError("스냅 파일에 비어 있거나 누락된 geometry가 있습니다.")
    if not saved_links.geometry.is_valid.all():
        raise ValueError("스냅 파일에 유효하지 않은 geometry가 있습니다.")
    if (saved_links["length_m"] <= 0).any():
        raise ValueError("스냅 파일에 길이가 0 이하인 LINK가 있습니다.")
    if not np.allclose(
        saved_links["length_m"],
        saved_links.geometry.length,
        rtol=0,
        atol=1e-6,
    ):
        raise ValueError("스냅 파일의 length_m과 실제 geometry 길이가 다릅니다.")

    return saved_links


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
    endpoints = extract_link_endpoints(links)
    grouped_endpoints, canonical_endpoints = (
        group_endpoints_within_tolerance(endpoints)
    )
    snapped_links, snap_geometry_statistics = (
        apply_canonical_endpoints_to_links(links, grouped_endpoints)
    )
    snapped_output = build_snapped_output(snapped_links)

    print()
    print("[LINK endpoint 추출]")
    print(f"  LINK 수: {len(links):,}")
    print(f"  endpoint 레코드 수: {len(endpoints):,}")
    print(
        "  고유 endpoint 좌표 수: "
        f"{endpoints[['x', 'y']].drop_duplicates().shape[0]:,}"
    )

    print()
    print("[endpoint 1m 그룹화]")
    print(
        "  스냅 전 고유 좌표: "
        f"{endpoints[['x', 'y']].drop_duplicates().shape[0]:,}"
    )
    print(f"  스냅 후 대표 좌표: {len(canonical_endpoints):,}")
    print(
        "  이동한 endpoint: "
        f"{grouped_endpoints['snap_distance_m'].gt(0).sum():,}"
    )
    print(
        "  최대 이동거리: "
        f"{grouped_endpoints['snap_distance_m'].max():.6f} m"
    )

    print()
    print("[LINK endpoint 좌표 치환]")
    print(f"  처리 LINK: {snap_geometry_statistics['link_count']:,}")
    print(f"  좌표가 변경된 LINK: {snap_geometry_statistics['changed_link_count']:,}")
    print(f"  기존 폐합 LINK: {snap_geometry_statistics['original_closed_count']:,}")
    print(f"  스냅 후 폐합 LINK: {snap_geometry_statistics['snapped_closed_count']:,}")
    print(f"  새로 생긴 폐합 LINK: {snap_geometry_statistics['new_closed_count']:,}")
    print(f"  0m LINK: {snap_geometry_statistics['zero_length_count']:,}")
    print(
        "  최대 길이 변화: "
        f"{snap_geometry_statistics['maximum_length_change_m']:.6f} m"
    )

    validate_spatial_data(nodes, snapped_links)
    write_interim_network(nodes, links)
    print_spatial_result(nodes, links)

    write_snapped_network(snapped_output)
    saved_snapped_links = validate_snapped_network_file(len(snapped_output))

    print()
    print("[D2 processed 스냅 파일]")
    print(f"  저장 행 수: {len(saved_snapped_links):,}")
    print(f"  CRS: {saved_snapped_links.crs}")
    print(f"  레이어: snapped_links")
    print(f"  파일: {SNAPPED_NETWORK_PATH}")

    print("\n[D2-2 스냅 산출물 저장 완료]")


if __name__ == "__main__":
    main()
