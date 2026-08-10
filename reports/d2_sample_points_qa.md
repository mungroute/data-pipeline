# D2 segment sample point QA

- 생성일: 2026-08-10
- 좌표계: EPSG:5186
- 목표 간격: 약 10m
- 규칙: `max(1, round(length_m / 10m))`개 등분 구간의 중심점
- junction endpoint 중복 생성: 없음

## 적재 결과

| 지표 | 결과 |
| --- | ---: |
| route_segment | 7,766 |
| 기존 sample | 0 |
| 생성 sample | 31,167 |
| segment당 최소/최대 sample | 1 / 92 |
| 등분 구간 길이 최소/평균/최대 | 1.140 / 9.925 / 14.990 m |
| sample 없는 segment | 0 |
| 잘못된 seq | 0 |
| 잘못된 SRID | 0 |
| 잘못된 geometry | 0 |
| 원래 segment를 벗어난 점 | 0 |
| 중복 `(segment_id, seq)` | 0 |
| D3/D4 분석값 선입력 | 0 |

## 판정

- 모든 route_segment에 최소 1개 이상의 공통 분석점이 있다.
- 모든 점은 원래 LineString 위에 있으며 `segment_id, seq`가 연속적이다.
- SVF·재질·그늘 필드는 D3/D4 입력 전이므로 모두 NULL이다.
- 이후 분석값이 존재하면 스크립트는 자동 재생성을 거부하여 결과를 보호한다.
