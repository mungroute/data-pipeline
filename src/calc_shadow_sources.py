from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from calc_shadow import (
    DEFAULT_CDSM_PATH,
    DEFAULT_DATE,
    DEFAULT_DSM_PATH,
    DEFAULT_DTM_PATH,
    DEFAULT_LATITUDE,
    DEFAULT_LONGITUDE,
    DEFAULT_OBSERVER_HEIGHT_M,
    DEFAULT_TIMES,
    DEFAULT_TIMEZONE,
    NODATA_VALUE,
    RasterGrid,
    SolarPosition,
    assert_same_grid,
    build_timestamps,
    calculate_shadow_mask,
    calculate_solar_positions,
    open_raster,
    output_path_for,
)
from osgeo import gdal


gdal.UseExceptions()

PIPELINE_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TOTAL_SHADOW_DIR = PIPELINE_ROOT / "data" / "processed" / "shadow"
DEFAULT_OUTPUT_DIR = PIPELINE_ROOT / "data" / "processed" / "shadow_sources"
DEFAULT_REPORT_PATH = PIPELINE_ROOT / "reports" / "d3_shadow_source_qa.md"

NONE_CODE = 0
TERRAIN_CODE = 1
BUILDING_CODE = 2
TREE_CODE = 3
NODATA_CODE = NODATA_VALUE
SOURCE_LABELS = {
    NONE_CODE: "N",
    TERRAIN_CODE: "R",
    BUILDING_CODE: "B",
    TREE_CODE: "T",
}


@dataclass(frozen=True)
class ComponentSurfaces:
    grid: RasterGrid
    terrain: np.ndarray
    terrain_occupied: np.ndarray
    terrain_building: np.ndarray
    building_occupied: np.ndarray
    total: np.ndarray
    total_occupied: np.ndarray


@dataclass(frozen=True)
class SourceStatistics:
    solar: SolarPosition
    none_pixels: int
    terrain_pixels: int
    building_pixels: int
    tree_pixels: int
    nodata_pixels: int


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="기존 D3 그림자를 지형(R)·건물(B)·수목(T) 원인으로 분리합니다."
    )
    parser.add_argument("--dtm", type=Path, default=DEFAULT_DTM_PATH)
    parser.add_argument("--dsm", type=Path, default=DEFAULT_DSM_PATH)
    parser.add_argument("--cdsm", type=Path, default=DEFAULT_CDSM_PATH)
    parser.add_argument("--total-shadow-dir", type=Path, default=DEFAULT_TOTAL_SHADOW_DIR)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT_PATH)
    parser.add_argument("--date", default=DEFAULT_DATE)
    parser.add_argument("--times", nargs="+", default=list(DEFAULT_TIMES))
    parser.add_argument("--timezone", default=DEFAULT_TIMEZONE)
    parser.add_argument("--latitude", type=float, default=DEFAULT_LATITUDE)
    parser.add_argument("--longitude", type=float, default=DEFAULT_LONGITUDE)
    parser.add_argument("--observer-height-m", type=float, default=DEFAULT_OBSERVER_HEIGHT_M)
    parser.add_argument("--ray-gap-m", type=float, default=5.0)
    parser.add_argument("--overwrite", action="store_true")
    return parser.parse_args()


def load_component_surfaces(
    dtm_path: Path,
    dsm_path: Path,
    cdsm_path: Path,
) -> ComponentSurfaces:
    dtm_dataset = open_raster(dtm_path)
    dsm_dataset = open_raster(dsm_path)
    cdsm_dataset = open_raster(cdsm_path)
    assert_same_grid(dtm_dataset, dsm_dataset, "DSM")
    assert_same_grid(dtm_dataset, cdsm_dataset, "CDSM")
    if dtm_dataset.RasterCount < 2:
        raise ValueError("DTM에는 표고 Band 1과 유효 영역 Alpha Band 2가 필요합니다.")

    dtm = dtm_dataset.GetRasterBand(1).ReadAsArray().astype(np.float32)
    alpha = dtm_dataset.GetRasterBand(2).ReadAsArray()
    dsm = dsm_dataset.GetRasterBand(1).ReadAsArray().astype(np.float32)
    cdsm_height = cdsm_dataset.GetRasterBand(1).ReadAsArray().astype(np.float32)
    valid = (alpha > 0) & np.isfinite(dtm) & (dtm != 0.0)
    building_occupied = valid & ((dsm - dtm) > 0.1)
    tree_occupied = valid & (cdsm_height > 0.0)

    terrain = dtm.copy()
    terrain[~valid] = np.nan
    terrain_building = np.maximum(dtm, np.where(valid, dsm, dtm)).astype(np.float32)
    terrain_building[~valid] = np.nan
    tree_surface = np.where(tree_occupied, dtm + cdsm_height, dtm)
    total = np.maximum(terrain_building, tree_surface).astype(np.float32)
    total[~valid] = np.nan

    grid = RasterGrid(
        width=dtm_dataset.RasterXSize,
        height=dtm_dataset.RasterYSize,
        geotransform=tuple(dtm_dataset.GetGeoTransform()),
        projection=dtm_dataset.GetProjection(),
        dtm=dtm,
        valid=valid,
    )
    dtm_dataset = dsm_dataset = cdsm_dataset = None
    return ComponentSurfaces(
        grid=grid,
        terrain=terrain,
        terrain_occupied=np.zeros_like(valid),
        terrain_building=terrain_building,
        building_occupied=building_occupied,
        total=total,
        total_occupied=building_occupied | tree_occupied,
    )


def classify_shadow_sources(
    terrain_shadow: np.ndarray,
    terrain_building_shadow: np.ndarray,
    total_shadow: np.ndarray,
) -> np.ndarray:
    if not (
        terrain_shadow.shape == terrain_building_shadow.shape == total_shadow.shape
    ):
        raise ValueError("그림자 마스크 크기가 서로 다릅니다.")
    valid = total_shadow != NODATA_VALUE
    if not np.array_equal(terrain_shadow == NODATA_VALUE, ~valid) or not np.array_equal(
        terrain_building_shadow == NODATA_VALUE, ~valid
    ):
        raise ValueError("세 그림자 마스크의 NoData 영역이 서로 다릅니다.")
    terrain = terrain_shadow == 1
    terrain_building = terrain_building_shadow == 1
    total = total_shadow == 1
    if np.any(terrain & ~terrain_building) or np.any(terrain_building & ~total):
        raise ValueError("지형 ⊆ 지형+건물 ⊆ 전체 그림자 부분집합 조건을 만족하지 않습니다.")

    source = np.full(total.shape, NONE_CODE, dtype=np.uint8)
    source[terrain] = TERRAIN_CODE
    source[terrain_building & ~terrain] = BUILDING_CODE
    source[total & ~terrain_building] = TREE_CODE
    source[~valid] = NODATA_CODE
    if not np.array_equal(valid & (source != NONE_CODE), total):
        raise RuntimeError("원인 코드 합집합이 전체 그림자와 일치하지 않습니다.")
    return source


def measure_artificial_shadow_length(
    apparent_elevation_deg: float,
    obstacle_height_m: float = 50.0,
    observer_height_m: float = DEFAULT_OBSERVER_HEIGHT_M,
    pixel_size_m: float = 2.0,
) -> float:
    """평탄면의 50m 단일 장애물로 광선 추적 길이를 재현성 있게 검사한다."""
    width = 256
    obstacle_column = 200
    dtm = np.zeros((1, width), dtype=np.float32)
    valid = np.ones_like(dtm, dtype=bool)
    surface = dtm.copy()
    surface[0, obstacle_column] = obstacle_height_m
    occupied = np.zeros_like(valid)
    occupied[0, obstacle_column] = True
    grid = RasterGrid(
        width=width,
        height=1,
        geotransform=(0.0, pixel_size_m, 0.0, pixel_size_m, 0.0, -pixel_size_m),
        projection="",
        dtm=dtm,
        valid=valid,
    )
    shadow = calculate_shadow_mask(
        grid,
        surface,
        occupied,
        SolarPosition(
            timestamp=pd.Timestamp("2026-06-21 12:00", tz="Asia/Seoul"),
            apparent_elevation_deg=apparent_elevation_deg,
            azimuth_deg=90.0,
        ),
        observer_height_m,
        maximum_gap_m=pixel_size_m * 1.5,
    )
    shadow_cells = 0
    for column in range(obstacle_column - 1, -1, -1):
        if shadow[0, column] != 1:
            break
        shadow_cells += 1
    return shadow_cells * pixel_size_m


def source_output_path(output_dir: Path, timestamp: pd.Timestamp) -> Path:
    return output_dir / f"shade_source_{timestamp.strftime('%Y%m%d_%H%M')}.tif"


def read_shadow(path: Path, grid: RasterGrid) -> np.ndarray:
    dataset = open_raster(path)
    if (dataset.RasterXSize, dataset.RasterYSize) != (grid.width, grid.height):
        raise ValueError(f"기존 전체 그림자 격자 크기가 다릅니다: {path}")
    if not np.allclose(dataset.GetGeoTransform(), grid.geotransform, atol=1e-9):
        raise ValueError(f"기존 전체 그림자 격자가 다릅니다: {path}")
    if dataset.GetProjection() != grid.projection:
        raise ValueError(f"기존 전체 그림자 투영이 다릅니다: {path}")
    values = dataset.GetRasterBand(1).ReadAsArray().astype(np.uint8)
    dataset = None
    return values


def write_source_raster(
    path: Path,
    source: np.ndarray,
    grid: RasterGrid,
    solar: SolarPosition,
    overwrite: bool,
) -> None:
    if path.exists() and not overwrite:
        raise FileExistsError(f"출력이 이미 존재합니다. --overwrite를 사용하세요: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    dataset = gdal.GetDriverByName("GTiff").Create(
        str(path),
        grid.width,
        grid.height,
        1,
        gdal.GDT_Byte,
        options=["TILED=YES", "COMPRESS=DEFLATE", "PREDICTOR=2", "BIGTIFF=IF_SAFER"],
    )
    dataset.SetGeoTransform(grid.geotransform)
    dataset.SetProjection(grid.projection)
    band = dataset.GetRasterBand(1)
    band.SetDescription("exclusive shade source")
    band.SetNoDataValue(NODATA_CODE)
    band.WriteArray(source)
    band.ComputeStatistics(False)
    for code, label in SOURCE_LABELS.items():
        dataset.SetMetadataItem(f"VALUE_{code}", label)
    dataset.SetMetadataItem("TIMESTAMP", solar.timestamp.isoformat())
    dataset.SetMetadataItem("APPARENT_ELEVATION_DEG", f"{solar.apparent_elevation_deg:.8f}")
    dataset.SetMetadataItem("AZIMUTH_DEG", f"{solar.azimuth_deg:.8f}")
    dataset.FlushCache()
    dataset = None


def collect_statistics(source: np.ndarray, solar: SolarPosition) -> SourceStatistics:
    counts = np.bincount(source.ravel(), minlength=256)
    return SourceStatistics(
        solar=solar,
        none_pixels=int(counts[NONE_CODE]),
        terrain_pixels=int(counts[TERRAIN_CODE]),
        building_pixels=int(counts[BUILDING_CODE]),
        tree_pixels=int(counts[TREE_CODE]),
        nodata_pixels=int(counts[NODATA_CODE]),
    )


def write_report(path: Path, statistics: list[SourceStatistics], overwrite: bool) -> None:
    if path.exists() and not overwrite:
        raise FileExistsError(f"보고서가 이미 존재합니다. --overwrite를 사용하세요: {path}")
    rows = "\n".join(
        f"| {item.solar.timestamp.strftime('%H:%M')} | {item.solar.apparent_elevation_deg:.3f}° | "
        f"{item.solar.azimuth_deg:.3f}° | {item.terrain_pixels:,} | {item.building_pixels:,} | "
        f"{item.tree_pixels:,} | {item.none_pixels:,} |"
        for item in statistics
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"""# D3 그림자 원인 분리 QA

## 판정 규칙

- `R`: DTM만으로도 그림자
- `B`: `max(DTM, DSM)`에서 새로 생긴 그림자
- `T`: `max(DSM, DTM + CDSM)`에서 새로 생긴 그림자
- `N`: 전체 표면에서도 일조
- 원인 코드는 상호 배타적이며 `R ∪ B ∪ T`는 기존 최종 그림자와 정확히 같다.

| 시각 | 태양 고도 | 방위각 | R | B | T | N |
|---|---:|---:|---:|---:|---:|---:|
{rows}

## 자동 검증

- 모든 시각에서 `terrain ⊆ terrain+building ⊆ total`을 확인했다.
- 새로 계산한 total 마스크가 기존 `shadow_*.tif`와 바이트 단위로 동일함을 확인했다.
- NoData 영역과 태양 위치는 기존 계산 계약을 그대로 사용했다.
""",
        encoding="utf-8",
    )


def main() -> None:
    args = parse_arguments()
    if args.observer_height_m < 0.0:
        raise ValueError("--observer-height-m은 0 이상이어야 합니다.")
    if args.ray_gap_m <= 0.0:
        raise ValueError("--ray-gap-m은 0보다 커야 합니다.")
    components = load_component_surfaces(args.dtm, args.dsm, args.cdsm)
    timestamps = build_timestamps(args.date, args.times, args.timezone)
    altitude_m = float(np.median(components.grid.dtm[components.grid.valid]))
    solar_positions = calculate_solar_positions(
        timestamps, args.latitude, args.longitude, altitude_m
    )
    expected = [source_output_path(args.output_dir, item.timestamp) for item in solar_positions]
    if not args.overwrite:
        existing = [path for path in [*expected, args.report] if path.exists()]
        if existing:
            raise FileExistsError(
                "출력이 이미 존재합니다. --overwrite를 사용하세요: "
                + ", ".join(map(str, existing))
            )

    statistics: list[SourceStatistics] = []
    for solar, output_path in zip(solar_positions, expected):
        terrain_shadow = calculate_shadow_mask(
            components.grid,
            components.terrain,
            components.terrain_occupied,
            solar,
            args.observer_height_m,
            args.ray_gap_m,
        )
        terrain_building_shadow = calculate_shadow_mask(
            components.grid,
            components.terrain_building,
            components.building_occupied,
            solar,
            args.observer_height_m,
            args.ray_gap_m,
        )
        total_shadow = calculate_shadow_mask(
            components.grid,
            components.total,
            components.total_occupied,
            solar,
            args.observer_height_m,
            args.ray_gap_m,
        )
        existing_total = read_shadow(
            output_path_for(args.total_shadow_dir, solar.timestamp), components.grid
        )
        if not np.array_equal(total_shadow, existing_total):
            difference = int(np.count_nonzero(total_shadow != existing_total))
            raise RuntimeError(f"기존 최종 그림자와 새 계산이 다릅니다: {solar.timestamp}, {difference:,}셀")
        source = classify_shadow_sources(
            terrain_shadow, terrain_building_shadow, total_shadow
        )
        write_source_raster(output_path, source, components.grid, solar, args.overwrite)
        item = collect_statistics(source, solar)
        statistics.append(item)
        print(
            f"[원인] {solar.timestamp.strftime('%H:%M')} "
            f"R={item.terrain_pixels:,} B={item.building_pixels:,} T={item.tree_pixels:,} N={item.none_pixels:,}"
        )
    write_report(args.report, statistics, args.overwrite)
    print(f"[보고서] {args.report.resolve()}")


if __name__ == "__main__":
    main()
