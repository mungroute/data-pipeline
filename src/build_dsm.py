from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
from osgeo import gdal, ogr, osr


gdal.UseExceptions()
ogr.UseExceptions()

PIPELINE_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DTM_PATH = PIPELINE_ROOT / "data" / "raw" / "dem" / "jung_gu_1m.tif"
DEFAULT_BUILDING_DIR = PIPELINE_ROOT / "data" / "raw" / "building"
DEFAULT_SURFACE_DIR = PIPELINE_ROOT / "data" / "processed" / "surface"
DEFAULT_DTM_2M_PATH = DEFAULT_SURFACE_DIR / "junggu_dtm_2m.tif"
DEFAULT_HEIGHT_PATH = DEFAULT_SURFACE_DIR / "junggu_building_height_2m.tif"
DEFAULT_QUALITY_PATH = DEFAULT_SURFACE_DIR / "junggu_building_height_quality_2m.tif"
DEFAULT_DSM_PATH = DEFAULT_SURFACE_DIR / "junggu_dsm_2m.tif"
DEFAULT_REPORT_PATH = PIPELINE_ROOT / "reports" / "d3_dsm_qa.md"

TARGET_EPSG = 5186
SOURCE_PIXEL_SIZE_M = 1.0
TARGET_PIXEL_SIZE_M = 2.0
DEFAULT_HEIGHT_FIELD = "A16"
DEFAULT_FLOOR_FIELD = "A26"
DEFAULT_DISTRICT_FIELD = "A23"
DEFAULT_DISTRICT_CODE = "11140"
FLOOR_HEIGHT_M = 3.0


@dataclass(frozen=True)
class RasterGrid:
    """DTM과 파생 래스터가 공유해야 하는 격자 정보를 보관한다."""

    width: int
    height: int
    geotransform: tuple[float, float, float, float, float, float]
    projection: str

    @property
    def bounds(self) -> tuple[float, float, float, float]:
        """격자의 min_x, min_y, max_x, max_y를 반환한다."""
        origin_x, pixel_x, _, origin_y, _, pixel_y = self.geotransform
        max_x = origin_x + self.width * pixel_x
        min_y = origin_y + self.height * pixel_y
        return origin_x, min_y, max_x, origin_y


@dataclass(frozen=True)
class BuildingStatistics:
    """DTM 범위에 들어온 건물 높이 데이터의 품질 지표를 보관한다."""

    total_count: int
    measured_height_count: int
    floor_estimated_count: int
    average_estimated_count: int
    average_floor_count: float
    minimum_height_m: float
    median_height_m: float
    maximum_height_m: float


@dataclass(frozen=True)
class RasterStatistics:
    """생성된 높이 및 DSM 래스터의 핵심 통계를 보관한다."""

    valid_dtm_pixels: int
    building_pixels: int
    minimum_dtm_m: float
    maximum_dtm_m: float
    maximum_building_height_m: float
    minimum_dsm_m: float
    maximum_dsm_m: float


def parse_arguments() -> argparse.Namespace:
    """PowerShell에서 전달할 입력·출력 경로와 높이 필드를 해석한다."""
    parser = argparse.ArgumentParser(
        description="1m DTM과 GIS 건물 높이를 합성해 중구 DSM을 생성합니다."
    )
    parser.add_argument("--dtm", type=Path, default=DEFAULT_DTM_PATH)
    parser.add_argument(
        "--buildings",
        type=Path,
        help=(
            "GIS 건물 통합정보 SHP 경로. 생략하면 "
            "data/raw/building 폴더의 유일한 SHP를 사용합니다."
        ),
    )
    parser.add_argument("--height-field", default=DEFAULT_HEIGHT_FIELD)
    parser.add_argument("--floor-field", default=DEFAULT_FLOOR_FIELD)
    parser.add_argument("--district-field", default=DEFAULT_DISTRICT_FIELD)
    parser.add_argument("--district-code", default=DEFAULT_DISTRICT_CODE)
    parser.add_argument("--dtm-2m-output", type=Path, default=DEFAULT_DTM_2M_PATH)
    parser.add_argument("--height-output", type=Path, default=DEFAULT_HEIGHT_PATH)
    parser.add_argument("--quality-output", type=Path, default=DEFAULT_QUALITY_PATH)
    parser.add_argument("--dsm-output", type=Path, default=DEFAULT_DSM_PATH)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT_PATH)
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="기존 출력 래스터와 보고서를 덮어씁니다.",
    )
    return parser.parse_args()


def resolve_building_path(requested_path: Path | None) -> Path:
    """명시된 SHP 또는 raw/building 폴더의 유일한 SHP를 선택한다."""
    if requested_path is not None:
        return requested_path.resolve()

    candidates = sorted(DEFAULT_BUILDING_DIR.glob("*.shp"))
    if not candidates:
        raise FileNotFoundError(
            "건물 SHP가 없습니다. --buildings로 파일 경로를 지정하거나 "
            f"{DEFAULT_BUILDING_DIR}에 SHP 묶음을 배치하세요."
        )
    if len(candidates) > 1:
        names = ", ".join(path.name for path in candidates)
        raise ValueError(
            "건물 SHP가 여러 개입니다. --buildings로 하나를 지정하세요: "
            f"{names}"
        )
    return candidates[0].resolve()


def ensure_inputs_exist(dtm_path: Path, building_path: Path) -> None:
    """실행 전에 DTM과 SHP 필수 구성 파일의 존재를 확인한다."""
    if not dtm_path.is_file():
        raise FileNotFoundError(f"DTM 파일이 없습니다: {dtm_path}")
    if not building_path.is_file():
        raise FileNotFoundError(f"건물 SHP가 없습니다: {building_path}")

    missing_sidecars = [
        suffix
        for suffix in (".dbf", ".shx", ".prj")
        if not building_path.with_suffix(suffix).is_file()
    ]
    if missing_sidecars:
        raise FileNotFoundError(
            "건물 SHP 구성 파일이 누락됐습니다: "
            + ", ".join(missing_sidecars)
        )


def spatial_reference_from_wkt(wkt: str) -> osr.SpatialReference:
    """WKT를 전통적인 GIS 축 순서로 사용하는 공간참조 객체로 변환한다."""
    reference = osr.SpatialReference()
    reference.ImportFromWkt(wkt)
    reference.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    return reference


def expected_spatial_reference() -> osr.SpatialReference:
    """프로젝트 내부 좌표계인 EPSG:5186 공간참조를 생성한다."""
    reference = osr.SpatialReference()
    reference.ImportFromEPSG(TARGET_EPSG)
    reference.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    return reference


def is_epsg_5186_compatible(reference: osr.SpatialReference) -> bool:
    """권위 코드가 없어도 EPSG:5186과 같은 TM 원점·축척·오프셋인지 검사한다."""
    if reference.IsSame(expected_spatial_reference()):
        return True

    projection_name = (reference.GetAttrValue("PROJECTION") or "").lower()
    linear_unit = reference.GetLinearUnits()
    expected_parameters = {
        osr.SRS_PP_LATITUDE_OF_ORIGIN: 38.0,
        osr.SRS_PP_CENTRAL_MERIDIAN: 127.0,
        osr.SRS_PP_SCALE_FACTOR: 1.0,
        osr.SRS_PP_FALSE_EASTING: 200_000.0,
        osr.SRS_PP_FALSE_NORTHING: 600_000.0,
    }
    return (
        projection_name == "transverse_mercator"
        and np.isclose(linear_unit, 1.0)
        and all(
            np.isclose(reference.GetProjParm(parameter), expected_value)
            for parameter, expected_value in expected_parameters.items()
        )
    )


def open_and_validate_dtm(dtm_path: Path) -> tuple[gdal.Dataset, RasterGrid]:
    """원본 DTM을 열고 CRS, 1m 북향 격자, Alpha 밴드를 검증한다."""
    dataset = gdal.Open(str(dtm_path), gdal.GA_ReadOnly)
    if dataset is None:
        raise RuntimeError(f"DTM을 열 수 없습니다: {dtm_path}")
    if dataset.RasterCount < 2:
        raise ValueError("DTM에는 표고 Band 1과 Alpha Band 2가 필요합니다.")

    geotransform = dataset.GetGeoTransform()
    if geotransform[2] != 0.0 or geotransform[4] != 0.0:
        raise ValueError("회전된 DTM 격자는 현재 파이프라인에서 지원하지 않습니다.")
    if not np.isclose(geotransform[1], SOURCE_PIXEL_SIZE_M):
        raise ValueError(f"DTM X 해상도가 1m가 아닙니다: {geotransform[1]}")
    if not np.isclose(abs(geotransform[5]), SOURCE_PIXEL_SIZE_M):
        raise ValueError(f"DTM Y 해상도가 1m가 아닙니다: {geotransform[5]}")

    actual_reference = spatial_reference_from_wkt(dataset.GetProjection())
    if not is_epsg_5186_compatible(actual_reference):
        raise ValueError("DTM 좌표계가 EPSG:5186 투영 파라미터와 호환되지 않습니다.")

    alpha_band = dataset.GetRasterBand(2)
    if alpha_band.GetColorInterpretation() != gdal.GCI_AlphaBand:
        raise ValueError("DTM Band 2가 Alpha 밴드로 지정되지 않았습니다.")

    grid = RasterGrid(
        width=dataset.RasterXSize,
        height=dataset.RasterYSize,
        geotransform=geotransform,
        projection=dataset.GetProjection(),
    )
    return dataset, grid


def open_and_validate_buildings(
    building_path: Path,
    height_field: str,
    floor_field: str,
    district_field: str,
) -> tuple[ogr.DataSource, ogr.Layer]:
    """건물 레이어를 열고 Polygon 계열, CRS, 높이·층수 필드를 검증한다."""
    data_source = ogr.Open(str(building_path), 0)
    if data_source is None:
        raise RuntimeError(f"건물 SHP를 열 수 없습니다: {building_path}")
    layer = data_source.GetLayer(0)

    geometry_type = ogr.GT_Flatten(layer.GetGeomType())
    if geometry_type not in (ogr.wkbPolygon, ogr.wkbMultiPolygon):
        raise ValueError(f"건물 geometry가 Polygon 계열이 아닙니다: {geometry_type}")

    layer_reference = layer.GetSpatialRef()
    if layer_reference is None:
        raise ValueError("건물 레이어에 CRS가 없습니다.")
    layer_reference.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    if not is_epsg_5186_compatible(layer_reference):
        raise ValueError("건물 레이어 좌표계가 EPSG:5186과 호환되지 않습니다.")

    field_names = {
        layer.GetLayerDefn().GetFieldDefn(index).GetName()
        for index in range(layer.GetLayerDefn().GetFieldCount())
    }
    missing_fields = {height_field, floor_field, district_field} - field_names
    if missing_fields:
        raise ValueError(
            f"건물 필드가 없습니다: {sorted(missing_fields)}. "
            f"사용 가능한 필드: {sorted(field_names)}"
        )
    return data_source, layer


def remove_existing_outputs(paths: list[Path], overwrite: bool) -> None:
    """덮어쓰기 여부를 확인하고 GDAL 출력 파일만 안전하게 제거한다."""
    existing_paths = [path for path in paths if path.exists()]
    if existing_paths and not overwrite:
        joined = ", ".join(str(path) for path in existing_paths)
        raise FileExistsError(
            f"출력 파일이 이미 있습니다: {joined}. 다시 만들려면 --overwrite를 사용하세요."
        )
    for path in existing_paths:
        path.unlink()


def collect_building_statistics(
    layer: ogr.Layer,
    grid: RasterGrid,
    height_field: str,
    floor_field: str,
    district_field: str,
    district_code: str,
) -> BuildingStatistics:
    """DTM 범위의 실측 높이·층수 보정·평균 보정 건물 수를 계산한다."""
    layer.SetSpatialFilterRect(*grid.bounds)
    layer.SetAttributeFilter(f'"{district_field}" = \'{district_code}\'')
    measured_heights: list[float] = []
    valid_floor_counts: list[float] = []
    pending_floor_estimates: list[float] = []
    total_count = 0
    floor_estimated_count = 0
    average_estimated_count = 0

    layer.ResetReading()
    for feature in layer:
        total_count += 1
        height_index = feature.GetFieldIndex(height_field)
        floor_index = feature.GetFieldIndex(floor_field)
        height_m = (
            feature.GetFieldAsDouble(height_index)
            if feature.IsFieldSetAndNotNull(height_index)
            else 0.0
        )
        floor_count = (
            feature.GetFieldAsDouble(floor_index)
            if feature.IsFieldSetAndNotNull(floor_index)
            else 0.0
        )
        if np.isfinite(floor_count) and floor_count > 0.0:
            valid_floor_counts.append(float(floor_count))
        if np.isfinite(height_m) and height_m > 0.0:
            measured_heights.append(float(height_m))
        elif np.isfinite(floor_count) and floor_count > 0.0:
            floor_estimated_count += 1
            pending_floor_estimates.append(float(floor_count) * FLOOR_HEIGHT_M)
        else:
            average_estimated_count += 1
    layer.ResetReading()

    if not measured_heights:
        raise ValueError("DTM 범위에서 높이가 0보다 큰 건물을 찾지 못했습니다.")
    if not valid_floor_counts:
        raise ValueError("DTM 범위에서 평균을 계산할 유효 지상층수가 없습니다.")

    average_floor_count = float(np.mean(valid_floor_counts))
    effective_heights = np.asarray(
        measured_heights
        + pending_floor_estimates
        + [average_floor_count * FLOOR_HEIGHT_M] * average_estimated_count,
        dtype=float,
    )
    return BuildingStatistics(
        total_count=total_count,
        measured_height_count=len(measured_heights),
        floor_estimated_count=floor_estimated_count,
        average_estimated_count=average_estimated_count,
        average_floor_count=average_floor_count,
        minimum_height_m=float(np.min(effective_heights)),
        median_height_m=float(np.median(effective_heights)),
        maximum_height_m=float(np.max(effective_heights)),
    )


def create_float_raster(path: Path, grid: RasterGrid, band_count: int) -> gdal.Dataset:
    """DTM과 동일한 격자의 압축 GeoTIFF를 생성한다."""
    path.parent.mkdir(parents=True, exist_ok=True)
    driver = gdal.GetDriverByName("GTiff")
    dataset = driver.Create(
        str(path),
        grid.width,
        grid.height,
        band_count,
        gdal.GDT_Float32,
        options=[
            "TILED=YES",
            "COMPRESS=DEFLATE",
            "PREDICTOR=3",
            "BIGTIFF=IF_SAFER",
        ],
    )
    dataset.SetGeoTransform(grid.geotransform)
    dataset.SetProjection(grid.projection)
    return dataset


def create_byte_raster(path: Path, grid: RasterGrid) -> gdal.Dataset:
    """DTM과 동일한 격자의 1밴드 Byte GeoTIFF를 생성한다."""
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
    return dataset


def build_target_grid(source_grid: RasterGrid) -> RasterGrid:
    """1m 원본 범위를 모두 포함하도록 오른쪽·아래를 최대 1m 확장한 2m 격자를 만든다."""
    target_width = int(np.ceil(source_grid.width / TARGET_PIXEL_SIZE_M))
    target_height = int(np.ceil(source_grid.height / TARGET_PIXEL_SIZE_M))
    origin_x, _, rotation_x, origin_y, rotation_y, _ = source_grid.geotransform
    return RasterGrid(
        width=target_width,
        height=target_height,
        geotransform=(
            origin_x,
            TARGET_PIXEL_SIZE_M,
            rotation_x,
            origin_y,
            rotation_y,
            -TARGET_PIXEL_SIZE_M,
        ),
        projection=source_grid.projection,
    )


def resample_dtm_to_2m(
    source_dataset: gdal.Dataset,
    source_grid: RasterGrid,
    output_path: Path,
) -> tuple[gdal.Dataset, RasterGrid]:
    """유효 1m 픽셀의 평균으로 2m DTM을 만들고 Alpha 유효 영역을 보존한다."""
    target_grid = build_target_grid(source_grid)
    target_dataset = create_float_raster(output_path, target_grid, band_count=2)
    target_elevation_band = target_dataset.GetRasterBand(1)
    target_alpha_band = target_dataset.GetRasterBand(2)
    target_elevation_band.SetDescription("2m DTM elevation")
    target_elevation_band.SetUnitType("m")
    target_elevation_band.SetNoDataValue(0.0)
    target_alpha_band.SetDescription("valid area alpha")
    target_alpha_band.SetColorInterpretation(gdal.GCI_AlphaBand)

    source_elevation_band = source_dataset.GetRasterBand(1)
    source_alpha_band = source_dataset.GetRasterBand(2)
    target_block_rows = 128

    for target_row in range(0, target_grid.height, target_block_rows):
        target_rows = min(target_block_rows, target_grid.height - target_row)
        source_row = target_row * 2
        source_rows = min(target_rows * 2, source_grid.height - source_row)
        elevation = source_elevation_band.ReadAsArray(
            0,
            source_row,
            source_grid.width,
            source_rows,
        ).astype(np.float32)
        alpha = source_alpha_band.ReadAsArray(
            0,
            source_row,
            source_grid.width,
            source_rows,
        ).astype(np.float32)

        padded_elevation = np.zeros((target_rows * 2, target_grid.width * 2), dtype=np.float32)
        padded_alpha = np.zeros_like(padded_elevation)
        padded_elevation[:source_rows, :source_grid.width] = elevation
        padded_alpha[:source_rows, :source_grid.width] = alpha

        valid = (
            (padded_alpha > 0.0)
            & np.isfinite(padded_elevation)
            & (padded_elevation != 0.0)
        )
        elevation_sum = np.where(valid, padded_elevation, 0.0).reshape(
            target_rows,
            2,
            target_grid.width,
            2,
        ).sum(axis=(1, 3))
        valid_count = valid.reshape(
            target_rows,
            2,
            target_grid.width,
            2,
        ).sum(axis=(1, 3))
        target_elevation = np.divide(
            elevation_sum,
            valid_count,
            out=np.zeros_like(elevation_sum, dtype=np.float32),
            where=valid_count > 0,
        )
        target_alpha = np.where(valid_count > 0, 255.0, 0.0).astype(np.float32)

        target_elevation_band.WriteArray(target_elevation, 0, target_row)
        target_alpha_band.WriteArray(target_alpha, 0, target_row)

    target_elevation_band.ComputeStatistics(False)
    target_alpha_band.ComputeStatistics(False)
    target_dataset.SetMetadataItem("SURFACE_TYPE", "DTM")
    target_dataset.SetMetadataItem("RESAMPLING", "2x2 valid-pixel mean")
    target_dataset.SetMetadataItem("VERTICAL_UNIT", "metre")
    target_dataset.FlushCache()
    target_dataset = None

    reopened_dataset = gdal.Open(str(output_path), gdal.GA_ReadOnly)
    if reopened_dataset is None:
        raise RuntimeError(f"2m DTM을 다시 열 수 없습니다: {output_path}")
    return reopened_dataset, target_grid


def rasterize_building_heights(
    data_source: ogr.DataSource,
    source_layer: ogr.Layer,
    grid: RasterGrid,
    height_field: str,
    floor_field: str,
    district_field: str,
    district_code: str,
    average_floor_count: float,
    output_path: Path,
    quality_output_path: Path,
) -> None:
    """정책 순서로 높이를 보정하고 겹침 픽셀에는 가장 높은 건물을 남긴다."""
    height_raster = create_float_raster(output_path, grid, band_count=1)
    height_band = height_raster.GetRasterBand(1)
    height_band.SetNoDataValue(0.0)
    height_band.Fill(0.0)

    quality_raster = create_byte_raster(quality_output_path, grid)
    quality_band = quality_raster.GetRasterBand(1)
    quality_band.SetNoDataValue(0.0)
    quality_band.Fill(0.0)

    layer_name = source_layer.GetName().replace('"', '""')
    height_name = height_field.replace('"', '""')
    floor_name = floor_field.replace('"', '""')
    district_name = district_field.replace('"', '""')
    average_height_m = average_floor_count * FLOOR_HEIGHT_M
    ordered_layer = data_source.ExecuteSQL(
        f'SELECT *, CASE '
        f'WHEN "{height_name}" > 0 THEN "{height_name}" '
        f'WHEN "{floor_name}" > 0 THEN "{floor_name}" * {FLOOR_HEIGHT_M} '
        f'ELSE {average_height_m} END AS effective_height, '
        f'CASE WHEN "{height_name}" > 0 THEN 1 '
        f'WHEN "{floor_name}" > 0 THEN 2 ELSE 3 END AS height_source '
        f'FROM "{layer_name}" WHERE "{district_name}" = \'{district_code}\' '
        f'ORDER BY effective_height ASC',
        dialect="SQLITE",
    )
    if ordered_layer is None:
        raise RuntimeError("건물 높이 정렬 레이어를 만들지 못했습니다.")
    try:
        ordered_layer.SetSpatialFilterRect(*grid.bounds)
        result = gdal.RasterizeLayer(
            height_raster,
            [1],
            ordered_layer,
            options=["ATTRIBUTE=effective_height", "ALL_TOUCHED=FALSE"],
        )
        if result != gdal.CE_None:
            raise RuntimeError(f"건물 높이 rasterize에 실패했습니다: GDAL {result}")
        ordered_layer.ResetReading()
        result = gdal.RasterizeLayer(
            quality_raster,
            [1],
            ordered_layer,
            options=["ATTRIBUTE=height_source", "ALL_TOUCHED=FALSE"],
        )
        if result != gdal.CE_None:
            raise RuntimeError(f"건물 높이 품질 rasterize에 실패했습니다: GDAL {result}")
    finally:
        data_source.ReleaseResultSet(ordered_layer)

    height_band.ComputeStatistics(False)
    height_raster.SetMetadataItem("SOURCE_TYPE", "GIS_BUILDING_INTEGRATED_INFORMATION")
    height_raster.SetMetadataItem("HEIGHT_FIELD", height_field)
    height_raster.SetMetadataItem("FLOOR_FIELD", floor_field)
    height_raster.SetMetadataItem("FLOOR_HEIGHT_M", str(FLOOR_HEIGHT_M))
    height_raster.SetMetadataItem("AVERAGE_FLOOR_COUNT", f"{average_floor_count:.6f}")
    height_raster.SetMetadataItem("VERTICAL_UNIT", "metre")
    height_raster.FlushCache()
    height_raster = None
    quality_band.ComputeStatistics(False)
    quality_raster.SetMetadataItem("QUALITY_1", "measured A16 height")
    quality_raster.SetMetadataItem("QUALITY_2", "A26 floor count x 3.0m")
    quality_raster.SetMetadataItem("QUALITY_3", "Jung-gu average floor count x 3.0m")
    quality_raster.FlushCache()
    quality_raster = None


def mask_building_rasters_to_dtm(
    dtm_dataset: gdal.Dataset,
    height_path: Path,
    quality_path: Path,
    grid: RasterGrid,
) -> None:
    """DTM의 Alpha·유효 표고 밖에 rasterize된 건물 픽셀을 0으로 정리한다."""
    height_dataset = gdal.Open(str(height_path), gdal.GA_Update)
    quality_dataset = gdal.Open(str(quality_path), gdal.GA_Update)
    if height_dataset is None or quality_dataset is None:
        raise RuntimeError("마스킹할 건물 높이 또는 품질 래스터를 열 수 없습니다.")

    dtm_band = dtm_dataset.GetRasterBand(1)
    alpha_band = dtm_dataset.GetRasterBand(2)
    height_band = height_dataset.GetRasterBand(1)
    quality_band = quality_dataset.GetRasterBand(1)
    block_width, block_height = height_band.GetBlockSize()

    for row in range(0, grid.height, block_height):
        rows = min(block_height, grid.height - row)
        for column in range(0, grid.width, block_width):
            columns = min(block_width, grid.width - column)
            dtm = dtm_band.ReadAsArray(column, row, columns, rows)
            alpha = alpha_band.ReadAsArray(column, row, columns, rows)
            height = height_band.ReadAsArray(column, row, columns, rows)
            quality = quality_band.ReadAsArray(column, row, columns, rows)
            valid = (alpha > 0.0) & np.isfinite(dtm) & (dtm != 0.0)
            height_band.WriteArray(np.where(valid, height, 0.0), column, row)
            quality_band.WriteArray(np.where(valid, quality, 0), column, row)

    height_band.ComputeStatistics(False)
    quality_band.ComputeStatistics(False)
    height_dataset.FlushCache()
    quality_dataset.FlushCache()
    height_dataset = None
    quality_dataset = None


def build_dsm_raster(
    dtm_dataset: gdal.Dataset,
    grid: RasterGrid,
    height_path: Path,
    output_path: Path,
) -> RasterStatistics:
    """Alpha 유효 영역에서 DTM에 건물 높이를 더해 DSM을 생성한다."""
    height_dataset = gdal.Open(str(height_path), gdal.GA_ReadOnly)
    if height_dataset is None:
        raise RuntimeError(f"건물 높이 래스터를 열 수 없습니다: {height_path}")

    dsm_dataset = create_float_raster(output_path, grid, band_count=2)
    dsm_band = dsm_dataset.GetRasterBand(1)
    alpha_output_band = dsm_dataset.GetRasterBand(2)
    dsm_band.SetDescription("DSM elevation")
    dsm_band.SetUnitType("m")
    dsm_band.SetNoDataValue(0.0)
    alpha_output_band.SetDescription("valid area alpha")
    alpha_output_band.SetColorInterpretation(gdal.GCI_AlphaBand)

    dtm_band = dtm_dataset.GetRasterBand(1)
    alpha_band = dtm_dataset.GetRasterBand(2)
    height_band = height_dataset.GetRasterBand(1)
    block_width, block_height = dsm_band.GetBlockSize()

    valid_dtm_pixels = 0
    building_pixels = 0
    minimum_dtm_m = float("inf")
    maximum_dtm_m = float("-inf")
    maximum_building_height_m = 0.0
    minimum_dsm_m = float("inf")
    maximum_dsm_m = float("-inf")

    for row in range(0, grid.height, block_height):
        rows = min(block_height, grid.height - row)
        for column in range(0, grid.width, block_width):
            columns = min(block_width, grid.width - column)
            dtm = dtm_band.ReadAsArray(column, row, columns, rows).astype(np.float32)
            alpha = alpha_band.ReadAsArray(column, row, columns, rows).astype(np.float32)
            building_height = height_band.ReadAsArray(
                column, row, columns, rows
            ).astype(np.float32)

            valid = (alpha > 0.0) & np.isfinite(dtm) & (dtm != 0.0)
            building = valid & (building_height > 0.0)
            dsm = np.where(valid, dtm + building_height, 0.0).astype(np.float32)

            dsm_band.WriteArray(dsm, column, row)
            alpha_output_band.WriteArray(alpha, column, row)

            valid_dtm_pixels += int(np.count_nonzero(valid))
            building_pixels += int(np.count_nonzero(building))
            if np.any(valid):
                minimum_dtm_m = min(minimum_dtm_m, float(np.min(dtm[valid])))
                maximum_dtm_m = max(maximum_dtm_m, float(np.max(dtm[valid])))
                minimum_dsm_m = min(minimum_dsm_m, float(np.min(dsm[valid])))
                maximum_dsm_m = max(maximum_dsm_m, float(np.max(dsm[valid])))
            if np.any(building):
                maximum_building_height_m = max(
                    maximum_building_height_m,
                    float(np.max(building_height[building])),
                )

    dsm_band.ComputeStatistics(False)
    alpha_output_band.ComputeStatistics(False)
    dsm_dataset.SetMetadataItem("SURFACE_TYPE", "DSM")
    dsm_dataset.SetMetadataItem("FORMULA", "DTM + building height")
    dsm_dataset.SetMetadataItem("VERTICAL_UNIT", "metre")
    dsm_dataset.FlushCache()
    dsm_dataset = None
    height_dataset = None

    return RasterStatistics(
        valid_dtm_pixels=valid_dtm_pixels,
        building_pixels=building_pixels,
        minimum_dtm_m=minimum_dtm_m,
        maximum_dtm_m=maximum_dtm_m,
        maximum_building_height_m=maximum_building_height_m,
        minimum_dsm_m=minimum_dsm_m,
        maximum_dsm_m=maximum_dsm_m,
    )


def write_qa_report(
    report_path: Path,
    dtm_path: Path,
    dtm_2m_path: Path,
    building_path: Path,
    height_path: Path,
    quality_path: Path,
    dsm_path: Path,
    grid: RasterGrid,
    building_stats: BuildingStatistics,
    raster_stats: RasterStatistics,
) -> None:
    """입력 검증과 DSM 생성 결과를 재현 가능한 Markdown 보고서로 기록한다."""
    min_x, min_y, max_x, max_y = grid.bounds
    building_pixel_ratio = (
        raster_stats.building_pixels / raster_stats.valid_dtm_pixels * 100.0
    )
    report = f"""# D3 DSM 생성 QA

## 생성 방식

- 기준 지형: 1m DTM을 유효 픽셀 평균으로 변환한 2m DTM
- 건물 자료: GIS 건물 통합정보
- 자치구 필터: `{DEFAULT_DISTRICT_FIELD} = {DEFAULT_DISTRICT_CODE}` (서울 중구)
- 건물 높이 필드: `{DEFAULT_HEIGHT_FIELD}` (m)
- 높이 보정: `{DEFAULT_FLOOR_FIELD} × {FLOOR_HEIGHT_M:.1f}m`, 둘 다 없으면 중구 평균 층수 적용
- 계산식: `DSM = DTM + 건물 높이`
- 건물 겹침 픽셀: 가장 큰 양수 높이 사용
- DTM Alpha가 0이거나 Band 1 표고가 0인 픽셀: 분석 제외

## 입력·출력

| 구분 | 경로 |
| --- | --- |
| DTM | `{dtm_path}` |
| 2m DTM | `{dtm_2m_path}` |
| 건물 SHP | `{building_path}` |
| 건물 높이 래스터 | `{height_path}` |
| 건물 높이 품질 래스터 | `{quality_path}` |
| DSM | `{dsm_path}` |

## 격자 검사

| 지표 | 결과 |
| --- | ---: |
| CRS | EPSG:{TARGET_EPSG} 호환 TM 중부원점 2010 |
| 크기 | {grid.width:,} × {grid.height:,} |
| 픽셀 | {TARGET_PIXEL_SIZE_M:.1f}m × {TARGET_PIXEL_SIZE_M:.1f}m |
| 범위 | {min_x:.3f}, {min_y:.3f}, {max_x:.3f}, {max_y:.3f} |
| 유효 DTM 픽셀 | {raster_stats.valid_dtm_pixels:,} |

## 건물 검사

| 지표 | 결과 |
| --- | ---: |
| DTM 범위 건물 | {building_stats.total_count:,} |
| A16 실측 높이 | {building_stats.measured_height_count:,} |
| A26 층수 × 3m 보정 | {building_stats.floor_estimated_count:,} |
| 중구 평균 층수 × 3m 보정 | {building_stats.average_estimated_count:,} |
| 중구 평균 지상층수 | {building_stats.average_floor_count:.3f}층 |
| 건물 높이 최소/중앙/최대 | {building_stats.minimum_height_m:.2f} / {building_stats.median_height_m:.2f} / {building_stats.maximum_height_m:.2f}m |
| 건물 높이 픽셀 | {raster_stats.building_pixels:,} ({building_pixel_ratio:.3f}%) |

## 래스터 결과

| 지표 | 결과 |
| --- | ---: |
| DTM 표고 최소/최대 | {raster_stats.minimum_dtm_m:.3f} / {raster_stats.maximum_dtm_m:.3f}m |
| 적용된 건물 높이 최대 | {raster_stats.maximum_building_height_m:.3f}m |
| DSM 표고 최소/최대 | {raster_stats.minimum_dsm_m:.3f} / {raster_stats.maximum_dsm_m:.3f}m |

## 다음 QA

1. QGIS에서 DTM, 건물 높이, DSM을 겹쳐 건물 footprint 정합성을 확인한다.
2. 품질 래스터 1은 A등급, 2·3은 높이 추정이 포함된 B등급 근거로 사용한다.
3. DSM QA 통과 후 수목 수고·수관너비를 사용해 CDSM을 생성한다.
4. DSM/CDSM을 분리 입력으로 사용해 건물·수목 그림자를 각각 계산한다.
"""
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(report, encoding="utf-8")


def print_summary(
    dtm_path: Path,
    dtm_2m_path: Path,
    building_path: Path,
    height_path: Path,
    quality_path: Path,
    dsm_path: Path,
    report_path: Path,
    building_stats: BuildingStatistics,
    raster_stats: RasterStatistics,
) -> None:
    """PowerShell에서 바로 확인할 핵심 경로와 QA 수치를 출력한다."""
    summary: dict[str, Any] = {
        "DTM": dtm_path,
        "2m DTM": dtm_2m_path,
        "건물 SHP": building_path,
        "DTM 범위 건물": f"{building_stats.total_count:,}",
        "A16 실측 높이": f"{building_stats.measured_height_count:,}",
        "층수 보정": f"{building_stats.floor_estimated_count:,}",
        "평균층수 보정": f"{building_stats.average_estimated_count:,}",
        "건물 높이 픽셀": f"{raster_stats.building_pixels:,}",
        "건물 높이 래스터": height_path,
        "건물 높이 품질": quality_path,
        "DSM": dsm_path,
        "QA 보고서": report_path,
    }
    for label, value in summary.items():
        print(f"[{label}] {value}")


def main() -> None:
    """D3 건물 DSM 생성 전체 단계를 순서대로 실행한다."""
    arguments = parse_arguments()
    dtm_path = arguments.dtm.resolve()
    building_path = resolve_building_path(arguments.buildings)
    dtm_2m_path = arguments.dtm_2m_output.resolve()
    height_path = arguments.height_output.resolve()
    quality_path = arguments.quality_output.resolve()
    dsm_path = arguments.dsm_output.resolve()
    report_path = arguments.report.resolve()

    ensure_inputs_exist(dtm_path, building_path)
    remove_existing_outputs(
        [dtm_2m_path, height_path, quality_path, dsm_path, report_path],
        overwrite=arguments.overwrite,
    )

    source_dtm_dataset, source_grid = open_and_validate_dtm(dtm_path)
    dtm_dataset, grid = resample_dtm_to_2m(
        source_dtm_dataset,
        source_grid,
        dtm_2m_path,
    )
    building_source, building_layer = open_and_validate_buildings(
        building_path,
        arguments.height_field,
        arguments.floor_field,
        arguments.district_field,
    )
    building_stats = collect_building_statistics(
        building_layer,
        grid,
        arguments.height_field,
        arguments.floor_field,
        arguments.district_field,
        arguments.district_code,
    )
    rasterize_building_heights(
        building_source,
        building_layer,
        grid,
        arguments.height_field,
        arguments.floor_field,
        arguments.district_field,
        arguments.district_code,
        building_stats.average_floor_count,
        height_path,
        quality_path,
    )
    mask_building_rasters_to_dtm(
        dtm_dataset,
        height_path,
        quality_path,
        grid,
    )
    raster_stats = build_dsm_raster(
        dtm_dataset,
        grid,
        height_path,
        dsm_path,
    )
    write_qa_report(
        report_path,
        dtm_path,
        dtm_2m_path,
        building_path,
        height_path,
        quality_path,
        dsm_path,
        grid,
        building_stats,
        raster_stats,
    )
    print_summary(
        dtm_path,
        dtm_2m_path,
        building_path,
        height_path,
        quality_path,
        dsm_path,
        report_path,
        building_stats,
        raster_stats,
    )


if __name__ == "__main__":
    main()
