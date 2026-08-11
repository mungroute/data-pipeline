# D3 DSM 공간 QA

## 검사 방식

- 최종 D2 도보망을 약 2.0m 간격으로 등분해 중심점을 샘플링했다.
- 건물 높이 래스터가 0보다 큰 점을 건물 중첩 후보로 분류했다.
- 중첩은 자동 삭제 근거가 아니라 QGIS 육안 검토 후보이다.

## 입력·출력

| 구분 | 경로 |
| --- | --- |
| 도보망 | `C:\project\mungroute\data-pipeline\data\processed\network\junggu_walk_network_snapped.gpkg` |
| 건물 높이 | `C:\project\mungroute\data-pipeline\data\processed\surface\junggu_building_height_2m.tif` |
| 높이 품질 | `C:\project\mungroute\data-pipeline\data\processed\surface\junggu_building_height_quality_2m.tif` |
| QGIS QA | `C:\project\mungroute\data-pipeline\data\interim\surface\d3_dsm_spatial_qa.gpkg` |

## 결과

| 지표 | 결과 |
| --- | ---: |
| 최종 도보 LINK | 7,766 |
| 2m 샘플점 | 158,958 |
| 중첩 후보 LINK | 1,482 |
| 중첩 샘플점 | 6,976 |
| HIGH | 263 |
| MEDIUM | 449 |
| LOW | 770 |

## QGIS 확인 순서

1. `route_overlap_candidates`를 `severity`로 분류해 HIGH부터 확인한다.
2. 실제 보행로가 건물 옆을 지나는데 footprint 오차로 겹친 경우는 유지한다.
3. 실내·통행불가 건물 내부를 통과하는 경우만 D2 제외 후보로 별도 기록한다.
4. 원본 도보망이나 DSM을 즉시 수정하지 않고 판정 근거를 먼저 남긴다.

## 확정 판정

| segment_id | 판정 | 그림자 정책 | 근거 |
| ---: | --- | --- | --- |
| 99075 | KEEP_COVERED_PASSAGE | FORCE_BUILDING_SHADE | 건물 외곽·상부 구조 아래에 실제 공개 보행로가 있으므로 도보망은 유지하고, DSM 지붕면 샘플링 대신 건물 그늘로 처리한다. |
| 216272 | KEEP_FOOTPRINT_OFFSET | SAMPLE_NEAREST_GROUND | 건물 옆 외부 보도로 재확인되어 도보망은 유지하며, 인접 지면 픽셀에서 일반 그림자 계산을 수행한다. |
| 179751 | KEEP_COVERED_PASSAGE | FORCE_BUILDING_SHADE | 지붕이 덮인 시장형 공개 통행로이므로 도보망은 유지하고 모든 분석 시각에 건물 그늘로 처리한다. |
| 140037 | KEEP_FOOTPRINT_OFFSET | SAMPLE_NEAREST_GROUND | 건물 옆 외부 보도로 확인되어 도보망은 유지하며, 겹친 DSM 지붕면 대신 인접 지면 픽셀에서 일반 그림자 계산을 수행한다. |
| 140271 | KEEP_COVERED_PASSAGE | FORCE_BUILDING_SHADE | 건물 내부·상부 구조 아래로 이어지는 실제 공개 보행로이므로 도보망은 유지하고 모든 분석 시각에 건물 그늘로 처리한다. |
| 127887 | KEEP_COVERED_PASSAGE | FORCE_BUILDING_SHADE | 건물 옆 보행로 위를 연결 통로·지붕 구조물이 연속적으로 덮고 있으므로 도보망은 유지하고 모든 분석 시각에 건물 그늘로 처리한다. |
| 231166 | KEEP_COVERED_PASSAGE | FORCE_BUILDING_SHADE | 상부 건축 구조 아래로 이어지는 실제 공개 통행로이므로 도보망은 유지하고 모든 분석 시각에 건물 그늘로 처리한다. |
| 253958 | KEEP_COVERED_PASSAGE | FORCE_BUILDING_SHADE | 고가 연결통로 아래로 이어지는 실제 공개 보행 공간이므로 도보망은 유지하고, 현재 2D DSM 분석에서는 상부 구조에 의한 그늘로 처리한다. |
| 231025 | KEEP_COVERED_PASSAGE | FORCE_BUILDING_SHADE | 아파트 단지 내부의 실제 보행 동선이므로 도보망은 유지하고, 건물 래스터 중첩 지점은 필로티·연결 구조에 의한 그늘로 처리한다. 단지 출입 제한 여부는 별도 접근성 QA 대상으로 남긴다. |
| 221983 | KEEP_FOOTPRINT_OFFSET (잠정) | SAMPLE_NEAREST_GROUND | 인접 거리뷰상 아파트 단지 내 개방형 보행로로 이어지는 것으로 보이므로 경로는 유지하고 인접 지면을 사용한다. 정확한 segment 구간을 직접 확인하지 못했으므로 상태는 `PROVISIONAL`로 남긴다. |
| 135500 | KEEP_COVERED_PASSAGE | FORCE_BUILDING_SHADE | 건물 아래를 통과하는 실제 보행로로 확인되어 도보망은 유지하고 건물 래스터 중첩 구간은 상부 구조에 의한 그늘로 처리한다. |
| 105789 | KEEP_COVERED_PASSAGE | FORCE_BUILDING_SHADE | 건물 사이의 실제 단지 내 보행로이므로 도보망은 유지한다. 개방 구간에는 일반 그림자 계산을 적용하고, 건물 래스터 중첩 지점에만 필로티 상부 구조 그늘을 적용한다. |
| 151606 | KEEP_COVERED_PASSAGE | FORCE_BUILDING_SHADE | 건물 아래를 통과하는 실제 보행로로 확인되어 도보망은 유지하고 건물 래스터 중첩 구간은 상부 구조에 의한 그늘로 처리한다. |
