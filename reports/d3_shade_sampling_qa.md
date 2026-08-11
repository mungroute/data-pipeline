# D3 산책로 그늘 샘플링 QA

- 실행 모드: DB 적용 완료
- sample point: 31,167
- 유효 래스터 sample: 30,698
- 래스터 범위 밖/NoData sample: 469 (65개 segment)
- 래스터 footprint 기준 건물 중첩 sample: 1,456
- `FORCE_BUILDING_SHADE` 적용 sample: 85
- `SAMPLE_NEAREST_GROUND` 적용 sample: 21
- provisional 판정 적용 sample: 8
- 최근접 지면 최대 이동: 4.47m / 허용 30.00m

수동 정책은 해당 segment의 모든 점이 아니라 실제 건물 높이 래스터(`> 0.1m`)와
중첩된 sample에만 적용했다. 최근접 지면은 거리, row, column 순으로 고정 정렬하여
동일 입력에서 항상 같은 셀을 선택한다.

## 시간대별 원인

| 시각 | N | R | B | T | NoData |
|---|---:|---:|---:|---:|---:|
| 09:00 | 16,232 | 55 | 11,416 | 2,995 | 469 |
| 12:00 | 24,476 | 0 | 3,386 | 2,836 | 469 |
| 15:00 | 19,130 | 20 | 8,540 | 3,008 | 469 |
| 18:00 | 8,504 | 586 | 19,078 | 2,530 | 469 |

## DB 트랜잭션 검증

| 지표 | 값 |
|---|---:|
| changed_before_update | 0 |
| updated_count | 0 |
| segment_updated_count | 0 |
| invalid_code_count | 0 |
| boolean_mismatch_count | 0 |
| invalid_ratio_count | 0 |
| uncovered_sample_count | 469 |
| partial_null_sample_count | 0 |
| uncovered_segment_count | 12 |
| aggregate_mismatch_count | 0 |

`is_shaded_*`는 원인이 `R/B/T`일 때만 true이다. 래스터 범위 밖 점은 다른 장소의
값을 강제 대입하지 않고 4개 시간대 모두 NULL로 보존했다. route_segment 비율은
유효 sample count를 분모로 집계한 뒤 마지막 단계에서만 소수 셋째 자리로 반올림했다.
