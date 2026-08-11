from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

# QGIS GDAL과 h5py가 서로 다른 HDF5 DLL을 포함하므로 pvlib을 GDAL보다 먼저 불러온다.
# 이 순서를 바꾸면 Windows에서 h5py의 `_errors` DLL 로드가 실패할 수 있다.
try:
    import pvlib
except ImportError:
    pvlib = None

from osgeo import gdal


gdal.UseExceptions()

PIPELINE_ROOT = Path(__file__).resolve().parents[1]
SURFACE_DIR = PIPELINE_ROOT / "data" / "processed" / "surface"
DEFAULT_DTM_PATH = SURFACE_DIR / "junggu_dtm_2m.tif"
DEFAULT_DSM_PATH = SURFACE_DIR / "junggu_dsm_2m.tif"
DEFAULT_CDSM_PATH = SURFACE_DIR / "junggu_cdsm_2m.tif"
DEFAULT_OUTPUT_DIR = PIPELINE_ROOT / "data" / "processed" / "shadow"
DEFAULT_REPORT_PATH = PIPELINE_ROOT / "reports" / "d3_shadow_qa.md"

DEFAULT_DATE = "2026-06-21"
DEFAULT_TIMES = ("09:00", "12:00", "15:00", "18:00")
DEFAULT_TIMEZONE = "Asia/Seoul"
DEFAULT_LATITUDE = 37.5636
DEFAULT_LONGITUDE = 126.9976
DEFAULT_OBSERVER_HEIGHT_M = 1.5
NODATA_VALUE = 255


@dataclass(frozen=True)
class RasterGrid:
    """모든 입력과 출력이 공유해야 하는 2m 래스터 격자 정보를 보관한다."""

    width: int
    height: int
    geotransform: tuple[float, float, float, float, float, float]
    projection: str
    dtm: np.ndarray
    valid: np.ndarray

    @property
    def pixel_size_m(self) -> float:
        """회전이 없는 정사각형 격자의 수평 해상도를 metre 단위로 반환한다."""
        return abs(float(self.geotransform[1]))


@dataclass(frozen=True)
class SolarPosition:
    """한 기준 시각의 태양 고도각과 방위각을 보관한다."""

    timestamp: pd.Timestamp
    apparent_elevation_deg: float
    azimuth_deg: float


@dataclass(frozen=True)
class ShadowStatistics:
    """시각별 그림자 결과를 보고서에 기록하기 위한 품질 지표를 보관한다."""

    timestamp: pd.Timestamp
    apparent_elevation_deg: float
    azimuth_deg: float
    valid_pixels: int
    shadow_pixels: int
    sunlit_pixels: int

    @property
    def shadow_ratio(self) -> float:
        """유효 영역에서 그림자인 셀의 비율을 반환한다."""
        return self.shadow_pixels / self.valid_pixels if self.valid_pixels else 0.0


def parse_arguments() -> argparse.Namespace:
    """PowerShell에서 전달하는 입력 래스터, 기준일, 시각과 출력 경로를 해석한다."""
    parser = argparse.ArgumentParser(
        description="DTM·DSM·CDSM과 pvlib 태양 위치를 이용해 중구 2m 그림자 래스터를 생성합니다."
    )
    parser.add_argument("--dtm", type=Path, default=DEFAULT_DTM_PATH)
    parser.add_argument("--dsm", type=Path, default=DEFAULT_DSM_PATH)
    parser.add_argument("--cdsm", type=Path, default=DEFAULT_CDSM_PATH)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT_PATH)
    parser.add_argument("--date", default=DEFAULT_DATE, help="YYYY-MM-DD")
    parser.add_argument("--times", nargs="+", default=list(DEFAULT_TIMES), help="HH:MM 목록")
    parser.add_argument("--timezone", default=DEFAULT_TIMEZONE)
    parser.add_argument("--latitude", type=float, default=DEFAULT_LATITUDE)
    parser.add_argument("--longitude", type=float, default=DEFAULT_LONGITUDE)
    parser.add_argument("--observer-height-m", type=float, default=DEFAULT_OBSERVER_HEIGHT_M)
    parser.add_argument(
        "--ray-gap-m",
        type=float,
        default=5.0,
        help="유효 영역이 이 거리보다 끊기면 같은 광선의 누적 장애물을 초기화합니다.",
    )
    parser.add_argument("--overwrite", action="store_true")
    return parser.parse_args()


def open_raster(path: Path) -> gdal.Dataset:
    """필수 GeoTIFF가 존재하는지 확인하고 읽기 전용 GDAL 데이터셋으로 연다."""
    dataset = gdal.Open(str(path.resolve()), gdal.GA_ReadOnly)
    if dataset is None:
        raise FileNotFoundError(f"래스터를 열 수 없습니다: {path.resolve()}")
    return dataset


def assert_same_grid(reference: gdal.Dataset, candidate: gdal.Dataset, label: str) -> None:
    """DSM과 CDSM이 DTM과 동일한 크기·좌표·투영을 사용하는지 검증한다."""
    if (candidate.RasterXSize, candidate.RasterYSize) != (
        reference.RasterXSize,
        reference.RasterYSize,
    ):
        raise ValueError(f"{label} 크기가 DTM과 다릅니다.")
    if not np.allclose(candidate.GetGeoTransform(), reference.GetGeoTransform(), atol=1e-9):
        raise ValueError(f"{label} 격자 원점 또는 해상도가 DTM과 다릅니다.")
    if candidate.GetProjection() != reference.GetProjection():
        raise ValueError(f"{label} 투영 정보가 DTM과 다릅니다.")


def load_surfaces(
    dtm_path: Path,
    dsm_path: Path,
    cdsm_path: Path,
) -> tuple[RasterGrid, np.ndarray, np.ndarray]:
    """DTM·건물 DSM·수목 CDSM을 읽어 최종 장애물 표면과 점유 마스크를 만든다."""
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

    # DSM은 이미 DTM+건물 높이인 절대 표고이고, CDSM만 지면 기준 수목 높이이다.
    tree_surface = np.where(cdsm_height > 0.0, dtm + cdsm_height, dtm)
    obstacle_surface = np.maximum(np.where(valid, dsm, dtm), tree_surface).astype(np.float32)
    occupied = valid & (((dsm - dtm) > 0.1) | (cdsm_height > 0.0))
    obstacle_surface[~valid] = np.nan

    grid = RasterGrid(
        width=dtm_dataset.RasterXSize,
        height=dtm_dataset.RasterYSize,
        geotransform=tuple(dtm_dataset.GetGeoTransform()),
        projection=dtm_dataset.GetProjection(),
        dtm=dtm,
        valid=valid,
    )
    dtm_dataset = None
    dsm_dataset = None
    cdsm_dataset = None
    return grid, obstacle_surface, occupied


def build_timestamps(day: str, times: list[str], timezone: str) -> pd.DatetimeIndex:
    """날짜와 HH:MM 목록을 시간대가 명시된 pandas 시각 목록으로 변환한다."""
    try:
        parsed_day = date.fromisoformat(day)
        naive = pd.DatetimeIndex([f"{parsed_day.isoformat()} {value}" for value in times])
        timestamps = naive.tz_localize(timezone)
    except (ValueError, TypeError) as error:
        raise ValueError("--date는 YYYY-MM-DD, --times는 HH:MM 형식이어야 합니다.") from error
    if timestamps.has_duplicates:
        raise ValueError("--times에 중복된 시각이 있습니다.")
    return timestamps


def calculate_solar_positions(
    timestamps: pd.DatetimeIndex,
    latitude: float,
    longitude: float,
    altitude_m: float,
) -> list[SolarPosition]:
    """pvlib으로 기준 위치와 각 시각의 대기 굴절 보정 태양 위치를 계산한다."""
    if pvlib is None:
        raise RuntimeError(
            "pvlib이 설치되지 않았습니다. QGIS Python으로 `python -m pip install -r requirements.txt`를 실행하세요."
        )

    frame = pvlib.solarposition.get_solarposition(
        time=timestamps,
        latitude=latitude,
        longitude=longitude,
        altitude=altitude_m,
    )
    return [
        SolarPosition(
            timestamp=pd.Timestamp(timestamp),
            apparent_elevation_deg=float(row["apparent_elevation"]),
            azimuth_deg=float(row["azimuth"]),
        )
        for timestamp, row in frame.iterrows()
    ]


def split_contiguous_ranges(
    perpendicular_bins: np.ndarray,
    along_sun_m: np.ndarray,
    maximum_gap_m: float,
) -> np.ndarray:
    """평행 광선 번호가 바뀌거나 유효 지형이 끊기는 지점을 누적 계산 경계로 표시한다."""
    boundaries = np.ones(len(perpendicular_bins), dtype=bool)
    if len(boundaries) <= 1:
        return boundaries
    same_ray = perpendicular_bins[1:] == perpendicular_bins[:-1]
    along_gap = along_sun_m[:-1] - along_sun_m[1:]
    boundaries[1:] = (~same_ray) | (along_gap > maximum_gap_m)
    return boundaries


def calculate_shadow_mask(
    grid: RasterGrid,
    obstacle_surface: np.ndarray,
    occupied: np.ndarray,
    solar: SolarPosition,
    observer_height_m: float,
    maximum_gap_m: float,
) -> np.ndarray:
    """태양 방향의 평행 광선마다 지형·건물·수목의 누적 최고 고도를 비교해 그림자를 판정한다."""
    result = np.full((grid.height, grid.width), NODATA_VALUE, dtype=np.uint8)
    result[grid.valid] = 0
    if solar.apparent_elevation_deg <= 0.0:
        result[grid.valid] = 1
        return result

    rows, columns = np.nonzero(grid.valid)
    origin_x, pixel_x, _, origin_y, _, pixel_y = grid.geotransform
    x = origin_x + (columns.astype(np.float64) + 0.5) * pixel_x
    y = origin_y + (rows.astype(np.float64) + 0.5) * pixel_y

    azimuth_rad = np.deg2rad(solar.azimuth_deg)
    sun_east = np.sin(azimuth_rad)
    sun_north = np.cos(azimuth_rad)
    along_sun = x * sun_east + y * sun_north
    perpendicular = x * sun_north - y * sun_east
    # floor를 사용해야 정확히 bin 경계에 놓인 동서·남북 광선이 부동소수점
    # 반올림 오차로 서로 다른 광선으로 갈라지지 않는다.
    perpendicular_bins = np.floor(perpendicular / grid.pixel_size_m).astype(np.int64)

    # 같은 광선 안에서 태양에 가까운 셀이 먼저 오도록 정렬한다.
    order = np.lexsort((-along_sun, perpendicular_bins))
    sorted_rows = rows[order]
    sorted_columns = columns[order]
    sorted_along = along_sun[order]
    sorted_bins = perpendicular_bins[order]
    tangent = np.tan(np.deg2rad(solar.apparent_elevation_deg))
    blocker_score = obstacle_surface[sorted_rows, sorted_columns].astype(np.float64) - sorted_along * tangent
    target_score = (
        grid.dtm[sorted_rows, sorted_columns].astype(np.float64)
        + observer_height_m
        - sorted_along * tangent
    )

    boundaries = split_contiguous_ranges(sorted_bins, sorted_along, maximum_gap_m)
    starts = np.flatnonzero(boundaries)
    ends = np.r_[starts[1:], len(order)]
    shaded_sorted = np.zeros(len(order), dtype=bool)
    for start, end in zip(starts, ends):
        scores = blocker_score[start:end]
        if len(scores) <= 1:
            continue
        previous_maximum = np.empty(len(scores), dtype=np.float64)
        previous_maximum[0] = -np.inf
        previous_maximum[1:] = np.maximum.accumulate(scores[:-1])
        shaded_sorted[start:end] = previous_maximum > (target_score[start:end] + 0.05)

    shaded = np.zeros((grid.height, grid.width), dtype=bool)
    shaded[sorted_rows, sorted_columns] = shaded_sorted
    # 건물 또는 수관 바로 아래의 지면은 광선 추적 이전에 직접 그림자로 처리한다.
    shaded |= occupied
    result[grid.valid & shaded] = 1
    return result


def output_path_for(output_dir: Path, timestamp: pd.Timestamp) -> Path:
    """기준 시각을 파일명에 포함한 시각별 그림자 GeoTIFF 경로를 만든다."""
    return output_dir / f"shadow_{timestamp.strftime('%Y%m%d_%H%M')}.tif"


def write_shadow_raster(
    path: Path,
    shadow: np.ndarray,
    grid: RasterGrid,
    solar: SolarPosition,
    overwrite: bool,
) -> None:
    """0=일조, 1=그림자, 255=범위 밖 규칙으로 압축 GeoTIFF를 기록한다."""
    if path.exists() and not overwrite:
        raise FileExistsError(f"출력이 이미 존재합니다. --overwrite를 사용하세요: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()

    driver = gdal.GetDriverByName("GTiff")
    dataset = driver.Create(
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
    band.SetDescription("ground shadow mask")
    band.SetNoDataValue(NODATA_VALUE)
    band.WriteArray(shadow)
    band.ComputeStatistics(False)
    dataset.SetMetadataItem("VALUE_0", "SUNLIT")
    dataset.SetMetadataItem("VALUE_1", "SHADOW")
    dataset.SetMetadataItem("TIMESTAMP", solar.timestamp.isoformat())
    dataset.SetMetadataItem("SOLAR_POSITION_LIBRARY", "pvlib")
    dataset.SetMetadataItem("APPARENT_ELEVATION_DEG", f"{solar.apparent_elevation_deg:.8f}")
    dataset.SetMetadataItem("AZIMUTH_DEG", f"{solar.azimuth_deg:.8f}")
    dataset.FlushCache()
    dataset = None


def collect_statistics(shadow: np.ndarray, solar: SolarPosition) -> ShadowStatistics:
    """완성된 그림자 마스크에서 유효·그림자·일조 셀 수를 집계한다."""
    valid_pixels = int(np.count_nonzero(shadow != NODATA_VALUE))
    shadow_pixels = int(np.count_nonzero(shadow == 1))
    return ShadowStatistics(
        timestamp=solar.timestamp,
        apparent_elevation_deg=solar.apparent_elevation_deg,
        azimuth_deg=solar.azimuth_deg,
        valid_pixels=valid_pixels,
        shadow_pixels=shadow_pixels,
        sunlit_pixels=valid_pixels - shadow_pixels,
    )


def write_report(
    path: Path,
    grid: RasterGrid,
    statistics: list[ShadowStatistics],
    dtm_path: Path,
    dsm_path: Path,
    cdsm_path: Path,
    output_dir: Path,
    latitude: float,
    longitude: float,
    observer_height_m: float,
    overwrite: bool,
) -> None:
    """태양 위치와 시각별 그림자 비율, QGIS 육안 검사 항목을 Markdown으로 기록한다."""
    if path.exists() and not overwrite:
        raise FileExistsError(f"보고서가 이미 존재합니다. --overwrite를 사용하세요: {path}")
    rows = "\n".join(
        "| "
        + " | ".join(
            [
                item.timestamp.strftime("%Y-%m-%d %H:%M %Z"),
                f"{item.apparent_elevation_deg:.3f}°",
                f"{item.azimuth_deg:.3f}°",
                f"{item.shadow_pixels:,}",
                f"{item.sunlit_pixels:,}",
                f"{item.shadow_ratio:.2%}",
            ]
        )
        + " |"
        for item in statistics
    )
    content = f"""# D3 그림자 래스터 계산 보고서

## 입력과 계산 정책

| 항목 | 값 |
|---|---|
| DTM | `{dtm_path}` |
| 건물 DSM | `{dsm_path}` |
| 수목 CDSM | `{cdsm_path}` |
| 출력 디렉터리 | `{output_dir}` |
| 격자 | {grid.width:,} × {grid.height:,}, {grid.pixel_size_m:.1f}m |
| 기준 위치 | 위도 {latitude:.6f}, 경도 {longitude:.6f} |
| 보행자 기준 높이 | {observer_height_m:.2f}m |
| 태양 위치 | pvlib `apparent_elevation`, `azimuth` |
| 판정값 | 0=일조, 1=그림자, 255=NoData |

DSM은 절대 표고이고 CDSM은 지면 기준 수고이므로, 최종 장애물 표면은
`max(DSM, DTM + CDSM)`으로 계산했다. 각 셀은 태양 방향의 같은 광선에 있는
지형·건물·수목 중 보행자 시선보다 높은 장애물이 있으면 그림자로 판정한다.

## 시각별 결과

| 기준 시각 | 태양 고도 | 태양 방위각 | 그림자 셀 | 일조 셀 | 그림자 비율 |
|---|---:|---:|---:|---:|---:|
{rows}

## QGIS QA

1. `09:00`, `12:00`, `15:00`, `18:00` 래스터를 같은 심볼 규칙으로 불러온다.
2. 오전 그림자가 서쪽, 오후 그림자가 동쪽으로 이동하는지 확인한다.
3. 정오의 그림자가 오전·오후보다 짧은지 확인한다.
4. 남산처럼 경사가 큰 지역에서 지형 그림자가 비정상적으로 끊기지 않는지 확인한다.
5. 고층 건물과 가로수 열 주변에서 장애물 위치와 그림자 시작점이 일치하는지 확인한다.
6. 중구 외부 Alpha 영역이 NoData로 유지되는지 확인한다.

## 제한사항

- 수관은 원형의 불투명 장애물로 단순화했으므로 잎 사이 투과광은 반영하지 않는다.
- 2m 래스터와 평행 광선 구간화에 따른 약 1~2셀 수준의 경계 오차가 있을 수 있다.
- 이 단계는 래스터 QA이며, 10m 산책로 샘플점 추출과 세그먼트 집계는 다음 단계에서 수행한다.
"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def validate_shadow_output(path: Path, grid: RasterGrid) -> None:
    """출력 래스터의 격자와 값 도메인이 계약대로 생성됐는지 다시 읽어 확인한다."""
    dataset = open_raster(path)
    if (dataset.RasterXSize, dataset.RasterYSize) != (grid.width, grid.height):
        raise RuntimeError(f"그림자 출력 격자 크기가 잘못되었습니다: {path}")
    values = np.unique(dataset.GetRasterBand(1).ReadAsArray())
    if not set(values.tolist()).issubset({0, 1, NODATA_VALUE}):
        raise RuntimeError(f"그림자 출력에 계약 밖 값이 있습니다: {values.tolist()}")
    dataset = None


def main() -> None:
    """입력을 검증하고 태양 위치별 그림자 래스터 네 장과 QA 보고서를 생성한다."""
    arguments = parse_arguments()
    if arguments.observer_height_m < 0.0:
        raise ValueError("--observer-height-m은 0 이상이어야 합니다.")
    if arguments.ray_gap_m <= 0.0:
        raise ValueError("--ray-gap-m은 0보다 커야 합니다.")

    grid, obstacle_surface, occupied = load_surfaces(
        arguments.dtm,
        arguments.dsm,
        arguments.cdsm,
    )
    timestamps = build_timestamps(arguments.date, arguments.times, arguments.timezone)
    valid_dtm = grid.dtm[grid.valid]
    altitude_m = float(np.median(valid_dtm))
    solar_positions = calculate_solar_positions(
        timestamps,
        arguments.latitude,
        arguments.longitude,
        altitude_m,
    )

    expected_paths = [output_path_for(arguments.output_dir, item.timestamp) for item in solar_positions]
    if not arguments.overwrite:
        existing = [path for path in [*expected_paths, arguments.report] if path.exists()]
        if existing:
            raise FileExistsError("출력이 이미 존재합니다. --overwrite를 사용하세요: " + ", ".join(map(str, existing)))

    all_statistics: list[ShadowStatistics] = []
    for solar, output_path in zip(solar_positions, expected_paths):
        print(
            f"[태양] {solar.timestamp.isoformat()} "
            f"고도={solar.apparent_elevation_deg:.3f}° 방위각={solar.azimuth_deg:.3f}°"
        )
        shadow = calculate_shadow_mask(
            grid,
            obstacle_surface,
            occupied,
            solar,
            arguments.observer_height_m,
            arguments.ray_gap_m,
        )
        write_shadow_raster(output_path, shadow, grid, solar, arguments.overwrite)
        validate_shadow_output(output_path, grid)
        statistics = collect_statistics(shadow, solar)
        all_statistics.append(statistics)
        print(
            f"[그림자] {output_path} "
            f"{statistics.shadow_pixels:,}/{statistics.valid_pixels:,} ({statistics.shadow_ratio:.2%})"
        )

    write_report(
        arguments.report,
        grid,
        all_statistics,
        arguments.dtm.resolve(),
        arguments.dsm.resolve(),
        arguments.cdsm.resolve(),
        arguments.output_dir.resolve(),
        arguments.latitude,
        arguments.longitude,
        arguments.observer_height_m,
        arguments.overwrite,
    )
    print(f"[보고서] {arguments.report.resolve()}")


if __name__ == "__main__":
    main()
