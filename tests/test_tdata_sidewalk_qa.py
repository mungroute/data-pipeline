import sys
from pathlib import Path

import geopandas as gpd
import pandas as pd
from shapely.geometry import Point


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from prepare_tdata_sidewalk_qa import (  # noqa: E402
    aggregate_segment_candidates,
    bbox_match_status,
    candidate_surface_type,
    normalize_swb_code,
)


def test_normalize_swb_code_accepts_short_and_full_values() -> None:
    assert normalize_swb_code("001") == "SWB001"
    assert normalize_swb_code("SWB005") == "SWB005"
    assert normalize_swb_code("-") == "-"


def test_candidate_surface_type_keeps_unsupported_materials_for_review() -> None:
    assert candidate_surface_type("SWB001") == "pavement"
    assert candidate_surface_type("005") == "asphalt"
    assert candidate_surface_type("SWB006") == "review"
    assert candidate_surface_type("SWB999") == "review"


def test_bbox_match_status_marks_overlap_and_large_envelopes() -> None:
    assert bbox_match_status(1, 100.0, 15.0, "pavement") == "SINGLE_SMALL_BBOX"
    assert bbox_match_status(1, 3000.0, 60.0, "pavement") == "SINGLE_LARGE_BBOX"
    assert bbox_match_status(2, 100.0, 15.0, "pavement") == "MULTIPLE_BBOX"
    assert bbox_match_status(1, 100.0, 15.0, "review") == "UNKNOWN_MATERIAL"


def test_segment_auto_correction_requires_safe_coverage_and_agreement() -> None:
    samples = gpd.GeoDataFrame(
        {
            "segment_id": [10, 10, 10],
            "sample_id": [1, 2, 3],
            "surface_type": ["asphalt", "asphalt", "asphalt"],
            "candidate_surface": ["pavement", "pavement", "pavement"],
            "match_status": ["SINGLE_SMALL_BBOX"] * 3,
            "agrees_current": [False, False, False],
        },
        geometry=[Point(0, 0), Point(1, 0), Point(2, 0)],
        crs="EPSG:5186",
    )
    result = aggregate_segment_candidates(samples, pd.Series({10: 3})).iloc[0]
    assert result["safe_sample_count"] == 3
    assert result["safe_coverage_ratio"] == 1.0
    assert result["correction_policy"] == "AUTO_PAVEMENT"
