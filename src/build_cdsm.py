from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd
from osgeo import gdal, ogr, osr
from pyproj import Transformer
from scipy.spatial import cKDTree


gdal.UseExceptions()
ogr.UseExceptions()

PIPELINE_ROOT = Path(__file__).resolve().parents[1]
TREE_DIR = PIPELINE_ROOT / "data" / "raw" / "tree"
DEFAULT_CURRENT_PATH = TREE_DIR / "서울시 가로수 위치 정보(경도, 위도).csv"
DEFAULT_DETAIL_PATH = TREE_DIR / "서울시 가로수 위치정보 (좌표계_ WGS1984).csv"
DEFAULT_DTM_PATH = PIPELINE_ROOT / "data" / "processed" / "surface" / "junggu_dtm_2m.tif"
DEFAULT_CDSM_PATH = PIPELINE_ROOT / "data" / "processed" / "surface" / "junggu_cdsm_2m.tif"
DEFAULT_QUALITY_PATH = PIPELINE_ROOT / "data" / "processed" / "surface" / "junggu_cdsm_quality_2m.tif"
DEFAULT_QA_GPKG_PATH = PIPELINE_ROOT / "data" / "interim" / "surface" / "d3_cdsm_qa.gpkg"
DEFAULT_REPORT_PATH = PIPELINE_ROOT / "reports" / "d3_cdsm_qa.md"

TARGET_EPSG = 5186
MATCH_DISTANCE_M = 5.0
GLOBAL_FALLBACK_SPECIES = "__GLOBAL__"

# 최신 파일과 상세 파일의 수종 명칭이 다른 대표 항목만 같은 계열로 연결한다.
SPECIES_ALIASES = {
    "은행나무 암나무": "은행나무",
    "벚나무류": "왕벚나무",
    "반송": "소나무",
    "금강송": "소나무",
    "잣나무류": "소나무",
    "튜울립나무": "백합나무",
    "대왕참나무": "상수리나무",
    "참나무류": "상수리나무",
}


@dataclass(frozen=True)
class RasterGrid:
    """CDSM이 따라야 하는 DTM 격자와 유효 마스크를 보관한다."""

    width: int
    height: int
    geotransform: tuple[float, float, float, float, float, float]
    projection: str
    elevation: np.ndarray
    alpha: np.ndarray


@dataclass(frozen=True)
class TreeStatistics:
    """가로수 결합·보정 및 CDSM 래스터화 품질 지표를 보관한다."""

    current_rows: int
    valid_coordinate_rows: int
    duplicate_coordinate_rows: int
    trees_in_valid_dtm: int
    trees_outside_valid_dtm: int
    direct_match_count: int
    species_median_count: int
    global_median_count: int
    detail_rows: int
    detail_valid_coordinate_rows: int
    detail_invalid_dimension_rows: int
    minimum_height_m: float
    median_height_m: float
    maximum_height_m: float
    minimum_crown_width_m: float
    median_crown_width_m: float
    maximum_crown_width_m: float
    canopy_pixels: int
    canopy_area_m2: float
    summed_crown_area_m2: float
    canopy_union_ratio: float


def parse_arguments() -> argparse.Namespace:
    """PowerShell에서 전달하는 CDSM 입력·출력 경로와 매칭 거리를 해석한다."""
    parser = argparse.ArgumentParser(
        description="최신 가로수 위치와 상세 수고·수관너비를 결합해 중구 2m CDSM을 생성합니다."
    )
    parser.add_argument("--current", type=Path, default=DEFAULT_CURRENT_PATH)
    parser.add_argument("--detail", type=Path, default=DEFAULT_DETAIL_PATH)
    parser.add_argument("--dtm", type=Path, default=DEFAULT_DTM_PATH)
    parser.add_argument("--cdsm-output", type=Path, default=DEFAULT_CDSM_PATH)
    parser.add_argument("--quality-output", type=Path, default=DEFAULT_QUALITY_PATH)
    parser.add_argument("--qa-gpkg", type=Path, default=DEFAULT_QA_GPKG_PATH)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT_PATH)
    parser.add_argument("--match-distance", type=float, default=MATCH_DISTANCE_M)
    parser.add_argument("--overwrite", action="store_true")
    return parser.parse_args()


def read_cp949_csv(path: Path) -> pd.DataFrame:
    """서울시 원본 CSV를 CP949로 읽고 파일 존재 여부를 검사한다."""
    if not path.exists():
        raise FileNotFoundError(f"가로수 CSV가 없습니다: {path}")
    return pd.read_csv(path, encoding="cp949", low_memory=False)


def require_columns(frame: pd.DataFrame, columns: set[str], label: str) -> None:
    """입력 데이터에 필요한 컬럼이 모두 있는지 검사한다."""
    missing = columns - set(frame.columns)
    if missing:
        raise ValueError(f"{label} 필수 컬럼이 없습니다: {sorted(missing)}")


def numeric(series: pd.Series) -> pd.Series:
    """문자열 혼합 컬럼을 결측 허용 실수로 변환한다."""
    return pd.to_numeric(series, errors="coerce")


def normalize_species(value: object) -> str:
    """수종명을 공백 정리하고 상세 데이터의 대표 명칭으로 통일한다."""
    name = str(value).strip() if pd.notna(value) else ""
    return SPECIES_ALIASES.get(name, name)


def load_dtm_grid(path: Path) -> RasterGrid:
    """건물 DSM과 공유하는 2m DTM, Alpha, 투영·격자 정보를 읽는다."""
    dataset = gdal.Open(str(path), gdal.GA_ReadOnly)
    if dataset is None:
        raise FileNotFoundError(f"2m DTM을 열 수 없습니다: {path}")
    if dataset.RasterCount < 2:
        raise ValueError("2m DTM에는 표고 Band 1과 Alpha Band 2가 필요합니다.")
    geotransform = dataset.GetGeoTransform()
    if not np.isclose(geotransform[1], 2.0) or not np.isclose(geotransform[5], -2.0):
        raise ValueError(f"CDSM 기준 DTM 픽셀이 2m가 아닙니다: {geotransform}")
    elevation = dataset.GetRasterBand(1).ReadAsArray().astype(np.float32)
    alpha = dataset.GetRasterBand(2).ReadAsArray().astype(np.float32)
    grid = RasterGrid(
        width=dataset.RasterXSize,
        height=dataset.RasterYSize,
        geotransform=tuple(geotransform),
        projection=dataset.GetProjection(),
        elevation=elevation,
        alpha=alpha,
    )
    dataset = None
    return grid


def build_detail_reference(detail: pd.DataFrame) -> tuple[pd.DataFrame, cKDTree, dict[str, tuple[float, float]], tuple[float, float], int]:
    """상세 조사점의 공간 인덱스와 수종별 수고·수관폭 중앙값을 만든다."""
    require_columns(detail, {"경도", "위도", "수목명", "수고", "수관너비"}, "상세 가로수")
    work = detail.copy()
    work["longitude"] = numeric(work["경도"])
    work["latitude"] = numeric(work["위도"])
    work["height_m"] = numeric(work["수고"])
    work["crown_width_m"] = numeric(work["수관너비"])
    work["species_key"] = work["수목명"].map(normalize_species)
    coordinate_valid = work["longitude"].between(126.8, 127.2) & work["latitude"].between(37.4, 37.7)
    dimension_valid = (work["height_m"] > 0.0) & (work["crown_width_m"] > 0.0)
    invalid_dimension_rows = int((~dimension_valid).sum())
    valid = work.loc[coordinate_valid & dimension_valid].copy()
    if valid.empty:
        raise ValueError("공간 매칭에 사용할 유효 상세 가로수가 없습니다.")

    transformer = Transformer.from_crs("EPSG:4326", f"EPSG:{TARGET_EPSG}", always_xy=True)
    valid["x"], valid["y"] = transformer.transform(
        valid["longitude"].to_numpy(), valid["latitude"].to_numpy()
    )
    spatial_index = cKDTree(valid[["x", "y"]].to_numpy())
    species_medians: dict[str, tuple[float, float]] = {}
    for species, group in valid.groupby("species_key"):
        if species:
            species_medians[species] = (
                float(group["height_m"].median()),
                float(group["crown_width_m"].median()),
            )
    global_medians = (
        float(valid["height_m"].median()),
        float(valid["crown_width_m"].median()),
    )
    return valid.reset_index(drop=True), spatial_index, species_medians, global_medians, invalid_dimension_rows


def coordinate_to_pixel(x: float, y: float, grid: RasterGrid) -> tuple[int, int]:
    """EPSG:5186 좌표를 DTM의 열·행 번호로 변환한다."""
    origin_x, pixel_x, _, origin_y, _, pixel_y = grid.geotransform
    column = int(np.floor((x - origin_x) / pixel_x))
    row = int(np.floor((y - origin_y) / pixel_y))
    return column, row


def prepare_current_trees(
    current: pd.DataFrame,
    detail_valid: pd.DataFrame,
    spatial_index: cKDTree,
    species_medians: dict[str, tuple[float, float]],
    global_medians: tuple[float, float],
    grid: RasterGrid,
    match_distance_m: float,
) -> tuple[pd.DataFrame, dict[str, int]]:
    """최신 위치에 상세 치수를 결합하고 DTM 유효 영역의 수목만 남긴다."""
    require_columns(
        current,
        {"자치구", "노선", "수종", "좌표(경도)", "좌표(위도)"},
        "최신 가로수",
    )
    work = current.copy()
    work["longitude"] = numeric(work["좌표(경도)"])
    work["latitude"] = numeric(work["좌표(위도)"])
    coordinate_valid = work["longitude"].between(126.8, 127.2) & work["latitude"].between(37.4, 37.7)
    valid_coordinate_rows = int(coordinate_valid.sum())
    work = work.loc[coordinate_valid].copy()
    duplicate_coordinate_rows = int(work.duplicated(["longitude", "latitude"]).sum())
    work = work.drop_duplicates(["longitude", "latitude"], keep="first").reset_index(drop=True)
    work["species_key"] = work["수종"].map(normalize_species)

    transformer = Transformer.from_crs("EPSG:4326", f"EPSG:{TARGET_EPSG}", always_xy=True)
    work["x"], work["y"] = transformer.transform(
        work["longitude"].to_numpy(), work["latitude"].to_numpy()
    )
    distances, detail_indexes = spatial_index.query(work[["x", "y"]].to_numpy(), k=1)
    work["match_distance_m"] = distances

    heights: list[float] = []
    crowns: list[float] = []
    sources: list[str] = []
    qualities: list[int] = []
    in_valid_dtm: list[bool] = []
    for row_index, row in work.iterrows():
        column, raster_row = coordinate_to_pixel(float(row["x"]), float(row["y"]), grid)
        point_is_valid = (
            0 <= column < grid.width
            and 0 <= raster_row < grid.height
            and grid.alpha[raster_row, column] > 0.0
            and np.isfinite(grid.elevation[raster_row, column])
            and grid.elevation[raster_row, column] != 0.0
        )
        in_valid_dtm.append(point_is_valid)

        if distances[row_index] <= match_distance_m:
            detail_row = detail_valid.iloc[int(detail_indexes[row_index])]
            height_m = float(detail_row["height_m"])
            crown_width_m = float(detail_row["crown_width_m"])
            source = "NEAREST_DETAIL_5M"
            quality = 1
        elif row["species_key"] in species_medians:
            height_m, crown_width_m = species_medians[str(row["species_key"])]
            source = "SPECIES_MEDIAN"
            quality = 2
        else:
            height_m, crown_width_m = global_medians
            source = "GLOBAL_MEDIAN"
            quality = 3
        heights.append(height_m)
        crowns.append(crown_width_m)
        sources.append(source)
        qualities.append(quality)

    work["height_m"] = heights
    work["crown_width_m"] = crowns
    work["dimension_source"] = sources
    work["quality"] = qualities
    work["in_valid_dtm"] = in_valid_dtm
    outside_count = int((~work["in_valid_dtm"]).sum())
    work = work.loc[work["in_valid_dtm"]].copy().reset_index(drop=True)
    counters = {
        "valid_coordinate_rows": valid_coordinate_rows,
        "duplicate_coordinate_rows": duplicate_coordinate_rows,
        "outside_valid_dtm": outside_count,
    }
    return work, counters


def rasterize_canopies(trees: pd.DataFrame, grid: RasterGrid) -> tuple[np.ndarray, np.ndarray]:
    """수관을 원형으로 근사하고 겹친 픽셀에는 가장 높은 수관을 기록한다."""
    canopy = np.zeros((grid.height, grid.width), dtype=np.float32)
    quality = np.zeros((grid.height, grid.width), dtype=np.uint8)
    origin_x, pixel_x, _, origin_y, _, pixel_y = grid.geotransform

    for tree in trees.itertuples(index=False):
        radius = max(float(tree.crown_width_m) / 2.0, pixel_x / 2.0)
        min_column = max(0, int(np.floor((tree.x - radius - origin_x) / pixel_x)) - 1)
        max_column = min(grid.width - 1, int(np.ceil((tree.x + radius - origin_x) / pixel_x)) + 1)
        min_row = max(0, int(np.floor((origin_y - (tree.y + radius)) / abs(pixel_y))) - 1)
        max_row = min(grid.height - 1, int(np.ceil((origin_y - (tree.y - radius)) / abs(pixel_y))) + 1)

        columns = np.arange(min_column, max_column + 1)
        rows = np.arange(min_row, max_row + 1)
        center_x = origin_x + (columns + 0.5) * pixel_x
        center_y = origin_y + (rows + 0.5) * pixel_y
        distance_squared = (
            (center_x[np.newaxis, :] - float(tree.x)) ** 2
            + (center_y[:, np.newaxis] - float(tree.y)) ** 2
        )
        inside = distance_squared <= radius**2
        valid = (
            (grid.alpha[min_row : max_row + 1, min_column : max_column + 1] > 0.0)
            & (grid.elevation[min_row : max_row + 1, min_column : max_column + 1] != 0.0)
        )
        mask = inside & valid
        target = canopy[min_row : max_row + 1, min_column : max_column + 1]
        target_quality = quality[min_row : max_row + 1, min_column : max_column + 1]
        replace = mask & (
            (float(tree.height_m) > target)
            | ((float(tree.height_m) == target) & (int(tree.quality) < target_quality))
        )
        target[replace] = float(tree.height_m)
        target_quality[replace] = int(tree.quality)
    return canopy, quality


def remove_existing_outputs(paths: list[Path], overwrite: bool) -> None:
    """출력 충돌을 검사하고 명시적으로 허용된 경우에만 기존 파일을 지운다."""
    existing = [path for path in paths if path.exists()]
    if existing and not overwrite:
        raise FileExistsError(
            "출력 파일이 이미 있습니다. 다시 만들려면 --overwrite를 사용하세요: "
            + ", ".join(str(path) for path in existing)
        )
    for path in existing:
        path.unlink()


def write_cdsm(path: Path, canopy: np.ndarray, grid: RasterGrid) -> None:
    """수관 높이와 DTM 유효 Alpha를 2밴드 GeoTIFF로 저장한다."""
    path.parent.mkdir(parents=True, exist_ok=True)
    driver = gdal.GetDriverByName("GTiff")
    dataset = driver.Create(
        str(path),
        grid.width,
        grid.height,
        2,
        gdal.GDT_Float32,
        options=["TILED=YES", "COMPRESS=DEFLATE", "PREDICTOR=3", "BIGTIFF=IF_SAFER"],
    )
    dataset.SetGeoTransform(grid.geotransform)
    dataset.SetProjection(grid.projection)
    canopy_band = dataset.GetRasterBand(1)
    canopy_band.SetDescription("tree canopy height above ground")
    canopy_band.SetUnitType("m")
    canopy_band.SetNoDataValue(0.0)
    canopy_band.WriteArray(canopy)
    canopy_band.ComputeStatistics(False)
    alpha_band = dataset.GetRasterBand(2)
    alpha_band.SetDescription("valid area alpha")
    alpha_band.SetColorInterpretation(gdal.GCI_AlphaBand)
    alpha_band.WriteArray(grid.alpha.astype(np.float32))
    alpha_band.ComputeStatistics(False)
    dataset.SetMetadataItem("SURFACE_TYPE", "CDSM")
    dataset.SetMetadataItem("VALUE_TYPE", "TREE_CANOPY_HEIGHT_ABOVE_GROUND")
    dataset.SetMetadataItem("VERTICAL_UNIT", "metre")
    dataset.FlushCache()
    dataset = None


def write_quality_raster(path: Path, quality: np.ndarray, grid: RasterGrid) -> None:
    """수관 치수 출처를 1=근접 상세, 2=수종 중앙값, 3=전체 중앙값으로 저장한다."""
    path.parent.mkdir(parents=True, exist_ok=True)
    driver = gdal.GetDriverByName("GTiff")
    dataset = driver.Create(
        str(path),
        grid.width,
        grid.height,
        1,
        gdal.GDT_Byte,
        options=["TILED=YES", "COMPRESS=DEFLATE", "BIGTIFF=IF_SAFER"],
    )
    dataset.SetGeoTransform(grid.geotransform)
    dataset.SetProjection(grid.projection)
    band = dataset.GetRasterBand(1)
    band.SetDescription("tree dimension source quality")
    band.SetNoDataValue(0)
    band.WriteArray(quality)
    band.ComputeStatistics(False)
    dataset.SetMetadataItem("QUALITY_1", "current position matched to detailed survey within threshold")
    dataset.SetMetadataItem("QUALITY_2", "species median height and crown width")
    dataset.SetMetadataItem("QUALITY_3", "global median height and crown width")
    dataset.FlushCache()
    dataset = None


def write_tree_points(path: Path, trees: pd.DataFrame, projection: str) -> None:
    """QGIS 육안 QA용 전체 가로수와 대표 검토 후보 GeoPackage를 만든다."""
    path.parent.mkdir(parents=True, exist_ok=True)
    driver = ogr.GetDriverByName("GPKG")
    data_source = driver.CreateDataSource(str(path))
    spatial_reference = osr.SpatialReference()
    spatial_reference.ImportFromWkt(projection)
    layer = data_source.CreateLayer("tree_canopy_points", spatial_reference, ogr.wkbPoint)
    fields = [
        ("tree_no", ogr.OFTInteger64),
        ("district", ogr.OFTString),
        ("route_name", ogr.OFTString),
        ("species", ogr.OFTString),
        ("height_m", ogr.OFTReal),
        ("crown_m", ogr.OFTReal),
        ("dim_source", ogr.OFTString),
        ("quality", ogr.OFTInteger),
        ("match_m", ogr.OFTReal),
    ]
    for name, field_type in fields:
        field = ogr.FieldDefn(name, field_type)
        if field_type == ogr.OFTString:
            field.SetWidth(80)
        layer.CreateField(field)

    definition = layer.GetLayerDefn()
    for index, tree in enumerate(trees.itertuples(index=False), start=1):
        feature = ogr.Feature(definition)
        feature.SetField("tree_no", index)
        feature.SetField("district", str(tree.자치구))
        feature.SetField("route_name", str(tree.노선))
        feature.SetField("species", str(tree.수종))
        feature.SetField("height_m", float(tree.height_m))
        feature.SetField("crown_m", float(tree.crown_width_m))
        feature.SetField("dim_source", str(tree.dimension_source))
        feature.SetField("quality", int(tree.quality))
        feature.SetField("match_m", float(tree.match_distance_m))
        geometry = ogr.Geometry(ogr.wkbPoint)
        geometry.AddPoint_2D(float(tree.x), float(tree.y))
        feature.SetGeometry(geometry)
        layer.CreateFeature(feature)
        feature = None
    layer.SyncToDisk()

    # 수관·수고 극단값과 전체 중앙값 보정점만 별도 후보로 모아 육안 QA 범위를 줄인다.
    widest_indexes = set(trees.nlargest(min(15, len(trees)), "crown_width_m").index)
    tallest_indexes = set(trees.nlargest(min(15, len(trees)), "height_m").index)
    global_indexes = trees.index[trees["quality"] == 3].to_numpy()
    if len(global_indexes) > 20:
        global_indexes = global_indexes[
            np.linspace(0, len(global_indexes) - 1, 20, dtype=int)
        ]
    candidate_indexes = sorted(widest_indexes | tallest_indexes | set(global_indexes))
    candidate_layer = data_source.CreateLayer(
        "tree_review_candidates", spatial_reference, ogr.wkbPoint
    )
    for name, field_type in fields:
        field = ogr.FieldDefn(name, field_type)
        if field_type == ogr.OFTString:
            field.SetWidth(80)
        candidate_layer.CreateField(field)
    reason_field = ogr.FieldDefn("review_reason", ogr.OFTString)
    reason_field.SetWidth(120)
    candidate_layer.CreateField(reason_field)
    candidate_definition = candidate_layer.GetLayerDefn()
    for tree_index in candidate_indexes:
        tree = trees.loc[tree_index]
        reasons: list[str] = []
        if tree_index in widest_indexes:
            reasons.append("LARGE_CROWN")
        if tree_index in tallest_indexes:
            reasons.append("TALL_TREE")
        if int(tree["quality"]) == 3:
            reasons.append("GLOBAL_MEDIAN_FALLBACK")
        feature = ogr.Feature(candidate_definition)
        feature.SetField("tree_no", int(tree_index) + 1)
        feature.SetField("district", str(tree["자치구"]))
        feature.SetField("route_name", str(tree["노선"]))
        feature.SetField("species", str(tree["수종"]))
        feature.SetField("height_m", float(tree["height_m"]))
        feature.SetField("crown_m", float(tree["crown_width_m"]))
        feature.SetField("dim_source", str(tree["dimension_source"]))
        feature.SetField("quality", int(tree["quality"]))
        feature.SetField("match_m", float(tree["match_distance_m"]))
        feature.SetField("review_reason", ",".join(reasons))
        geometry = ogr.Geometry(ogr.wkbPoint)
        geometry.AddPoint_2D(float(tree["x"]), float(tree["y"]))
        feature.SetGeometry(geometry)
        candidate_layer.CreateFeature(feature)
        feature = None
    candidate_layer.SyncToDisk()
    data_source = None


def build_statistics(
    current_rows: int,
    detail_rows: int,
    detail_valid_coordinate_rows: int,
    invalid_dimension_rows: int,
    counters: dict[str, int],
    trees: pd.DataFrame,
    canopy: np.ndarray,
    grid: RasterGrid,
) -> TreeStatistics:
    """보고서와 실행 요약에 사용할 CDSM 핵심 품질 지표를 계산한다."""
    canopy_pixels = int(np.count_nonzero(canopy > 0.0))
    pixel_area = abs(grid.geotransform[1] * grid.geotransform[5])
    canopy_area_m2 = canopy_pixels * pixel_area
    summed_crown_area = float(np.sum(np.pi * (trees["crown_width_m"].to_numpy() / 2.0) ** 2))
    source_counts = trees["dimension_source"].value_counts().to_dict()
    return TreeStatistics(
        current_rows=current_rows,
        valid_coordinate_rows=counters["valid_coordinate_rows"],
        duplicate_coordinate_rows=counters["duplicate_coordinate_rows"],
        trees_in_valid_dtm=len(trees),
        trees_outside_valid_dtm=counters["outside_valid_dtm"],
        direct_match_count=int(source_counts.get("NEAREST_DETAIL_5M", 0)),
        species_median_count=int(source_counts.get("SPECIES_MEDIAN", 0)),
        global_median_count=int(source_counts.get("GLOBAL_MEDIAN", 0)),
        detail_rows=detail_rows,
        detail_valid_coordinate_rows=detail_valid_coordinate_rows,
        detail_invalid_dimension_rows=invalid_dimension_rows,
        minimum_height_m=float(trees["height_m"].min()),
        median_height_m=float(trees["height_m"].median()),
        maximum_height_m=float(trees["height_m"].max()),
        minimum_crown_width_m=float(trees["crown_width_m"].min()),
        median_crown_width_m=float(trees["crown_width_m"].median()),
        maximum_crown_width_m=float(trees["crown_width_m"].max()),
        canopy_pixels=canopy_pixels,
        canopy_area_m2=canopy_area_m2,
        summed_crown_area_m2=summed_crown_area,
        canopy_union_ratio=canopy_area_m2 / summed_crown_area if summed_crown_area else 0.0,
    )


def write_report(
    path: Path,
    current_path: Path,
    detail_path: Path,
    dtm_path: Path,
    cdsm_path: Path,
    quality_path: Path,
    qa_gpkg_path: Path,
    match_distance_m: float,
    grid: RasterGrid,
    statistics: TreeStatistics,
) -> None:
    """재현 규칙과 입력·보정·래스터 QA 결과를 Markdown으로 기록한다."""
    valid_dtm_pixels = int(np.count_nonzero((grid.alpha > 0.0) & (grid.elevation != 0.0)))
    canopy_ratio = statistics.canopy_pixels / valid_dtm_pixels if valid_dtm_pixels else 0.0
    report = f"""# D3 CDSM 생성 QA

## 생성 방식

- 위치 기준: 최신 서울시 가로수 위치 CSV
- 치수 기준: 상세 WGS1984 CSV의 `수고`, `수관너비`
- 직접 결합: 최신 위치에서 {match_distance_m:.1f}m 이내의 가장 가까운 상세 조사점
- 미결합 보정: 수종별 중앙값, 해당 수종이 없으면 중구 전체 중앙값
- 수관 형상: `수관너비 ÷ 2` 반경의 원
- 수관 겹침: 높이를 더하지 않고 가장 높은 수관 사용
- 공간 필터: 중구 2m DTM Alpha 유효 영역
- CDSM 값: 지면 기준 수관 높이(m)

## 입력·출력

| 구분 | 경로 |
| --- | --- |
| 최신 가로수 위치 | `{current_path}` |
| 상세 수고·수관너비 | `{detail_path}` |
| 기준 2m DTM | `{dtm_path}` |
| CDSM | `{cdsm_path}` |
| 치수 품질 래스터 | `{quality_path}` |
| QGIS QA GeoPackage | `{qa_gpkg_path}` |

## 입력·결합 QA

| 지표 | 결과 |
| --- | ---: |
| 최신 입력 행 | {statistics.current_rows:,} |
| 유효 경위도 행 | {statistics.valid_coordinate_rows:,} |
| 중복 좌표 제거 | {statistics.duplicate_coordinate_rows:,} |
| DTM 유효 영역 수목 | {statistics.trees_in_valid_dtm:,} |
| DTM 유효 영역 밖 | {statistics.trees_outside_valid_dtm:,} |
| {match_distance_m:.1f}m 상세 치수 직접 결합 | {statistics.direct_match_count:,} |
| 수종별 중앙값 보정 | {statistics.species_median_count:,} |
| 전체 중앙값 보정 | {statistics.global_median_count:,} |
| 상세 입력 행 | {statistics.detail_rows:,} |
| 상세 유효 좌표·치수 | {statistics.detail_valid_coordinate_rows:,} |
| 상세 수고·수관폭 0/결측 | {statistics.detail_invalid_dimension_rows:,} |

## 치수·래스터 QA

| 지표 | 결과 |
| --- | ---: |
| 수고 최소/중앙/최대 | {statistics.minimum_height_m:.2f} / {statistics.median_height_m:.2f} / {statistics.maximum_height_m:.2f}m |
| 수관너비 최소/중앙/최대 | {statistics.minimum_crown_width_m:.2f} / {statistics.median_crown_width_m:.2f} / {statistics.maximum_crown_width_m:.2f}m |
| 수관 픽셀 | {statistics.canopy_pixels:,} |
| 수관 합집합 면적 | {statistics.canopy_area_m2:,.1f}m² |
| 개별 원 면적 합 | {statistics.summed_crown_area_m2:,.1f}m² |
| 합집합/개별 면적 비율 | {statistics.canopy_union_ratio:.3f} |
| 유효 DTM 중 수관 픽셀 비율 | {canopy_ratio:.3%} |

## 품질 코드

- `1`: 최신 위치와 {match_distance_m:.1f}m 이내 상세 조사점 치수
- `2`: 상세 자료의 동일 수종 중앙값
- `3`: 상세 자료 전체 중앙값

## QGIS 육안 QA

1. `d3_cdsm_qa.gpkg`의 `tree_canopy_points`를 불러온다.
2. `tree_review_candidates`를 불러와 큰 수관·큰 수고·전체 중앙값 보정 후보를 먼저 확인한다.
3. `tree_canopy_points`의 `quality`를 분류해 1·2·3의 위치 분포를 확인한다.
4. `junggu_cdsm_2m.tif`를 반투명으로 올려 대표 도로 3~5곳에서 실제 가로수 열과 비교한다.
"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(report, encoding="utf-8")


def validate_outputs(cdsm_path: Path, quality_path: Path, grid: RasterGrid) -> None:
    """생성된 CDSM·품질 래스터의 크기·투영·마스크 정합성을 재검사한다."""
    cdsm = gdal.Open(str(cdsm_path), gdal.GA_ReadOnly)
    quality = gdal.Open(str(quality_path), gdal.GA_ReadOnly)
    if cdsm is None or quality is None:
        raise RuntimeError("생성된 CDSM 또는 품질 래스터를 다시 열 수 없습니다.")
    for label, dataset in (("CDSM", cdsm), ("품질", quality)):
        if dataset.RasterXSize != grid.width or dataset.RasterYSize != grid.height:
            raise RuntimeError(f"{label} 격자가 DTM과 다릅니다.")
        if tuple(dataset.GetGeoTransform()) != grid.geotransform:
            raise RuntimeError(f"{label} GeoTransform이 DTM과 다릅니다.")
        if dataset.GetProjection() != grid.projection:
            raise RuntimeError(f"{label} 투영이 DTM과 다릅니다.")
    canopy = cdsm.GetRasterBand(1).ReadAsArray()
    quality_values = quality.GetRasterBand(1).ReadAsArray()
    invalid = (grid.alpha <= 0.0) | (grid.elevation == 0.0)
    if np.any(canopy[invalid] != 0.0) or np.any(quality_values[invalid] != 0):
        raise RuntimeError("DTM 유효 영역 밖에 CDSM 값이 존재합니다.")
    if not np.array_equal(canopy > 0.0, quality_values > 0):
        raise RuntimeError("CDSM 수관 마스크와 품질 마스크가 다릅니다.")
    cdsm = None
    quality = None


def print_summary(statistics: TreeStatistics, cdsm_path: Path, quality_path: Path, qa_path: Path, report_path: Path) -> None:
    """PowerShell에서 즉시 확인할 CDSM 핵심 결과를 출력한다."""
    print(f"[DTM 유효 영역 수목] {statistics.trees_in_valid_dtm:,}")
    print(f"[상세 치수 직접 결합] {statistics.direct_match_count:,}")
    print(f"[수종 중앙값 보정] {statistics.species_median_count:,}")
    print(f"[전체 중앙값 보정] {statistics.global_median_count:,}")
    print(f"[수관 픽셀] {statistics.canopy_pixels:,}")
    print(f"[수관 합집합 면적] {statistics.canopy_area_m2:,.1f}m²")
    print(f"[CDSM] {cdsm_path}")
    print(f"[품질 래스터] {quality_path}")
    print(f"[QGIS QA] {qa_path}")
    print(f"[QA 보고서] {report_path}")


def main() -> None:
    """최신 위치 결합부터 CDSM·QGIS QA·보고서 생성까지 순서대로 실행한다."""
    arguments = parse_arguments()
    current_path = arguments.current.resolve()
    detail_path = arguments.detail.resolve()
    dtm_path = arguments.dtm.resolve()
    cdsm_path = arguments.cdsm_output.resolve()
    quality_path = arguments.quality_output.resolve()
    qa_path = arguments.qa_gpkg.resolve()
    report_path = arguments.report.resolve()
    if arguments.match_distance <= 0.0:
        raise ValueError("--match-distance는 0보다 커야 합니다.")
    remove_existing_outputs([cdsm_path, quality_path, qa_path, report_path], arguments.overwrite)

    current = read_cp949_csv(current_path)
    detail = read_cp949_csv(detail_path)
    grid = load_dtm_grid(dtm_path)
    detail_valid, spatial_index, species_medians, global_medians, invalid_dimensions = build_detail_reference(detail)
    trees, counters = prepare_current_trees(
        current,
        detail_valid,
        spatial_index,
        species_medians,
        global_medians,
        grid,
        arguments.match_distance,
    )
    if trees.empty:
        raise ValueError("DTM 유효 영역에 CDSM을 만들 가로수가 없습니다.")

    canopy, quality = rasterize_canopies(trees, grid)
    write_cdsm(cdsm_path, canopy, grid)
    write_quality_raster(quality_path, quality, grid)
    write_tree_points(qa_path, trees, grid.projection)
    detail_coordinate_valid = (
        numeric(detail["경도"]).between(126.8, 127.2)
        & numeric(detail["위도"]).between(37.4, 37.7)
        & (numeric(detail["수고"]) > 0.0)
        & (numeric(detail["수관너비"]) > 0.0)
    )
    statistics = build_statistics(
        len(current),
        len(detail),
        int(detail_coordinate_valid.sum()),
        invalid_dimensions,
        counters,
        trees,
        canopy,
        grid,
    )
    write_report(
        report_path,
        current_path,
        detail_path,
        dtm_path,
        cdsm_path,
        quality_path,
        qa_path,
        arguments.match_distance,
        grid,
        statistics,
    )
    validate_outputs(cdsm_path, quality_path, grid)
    print_summary(statistics, cdsm_path, quality_path, qa_path, report_path)
    print("\n[D3 CDSM 생성 완료]")


if __name__ == "__main__":
    main()
