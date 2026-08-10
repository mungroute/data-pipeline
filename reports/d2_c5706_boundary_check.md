# D2 C5706 행정경계 인접 LINK 검사

- 원본: `data/raw/network/seoul_walk_network.csv`
- 검색 기준: 현재 topology의 C5706 degree=1 endpoint
- 검색 반경: 100.0 m
- component 끝점: 3개
- 발견 후보 LINK 접점: 40개
- 1m 스냅 허용오차 이내: 0개
- 후보 시군구: 성동구 31건, 중구 9건

## 판정 원칙

- `WITHIN_SNAP_TOLERANCE`: 좌표상 1m 이내지만 원본 속성과 실제 보행 가능 여부 확인 후 복구한다.
- `NEAR_ENDPOINT`: 1m 초과 5m 이하이므로 자동 스냅하지 않고 영상·지도 검토가 필요하다.
- `MANUAL_REVIEW`: 5m 초과이므로 연결 근거로 사용하지 않고 주변 데이터 탐색 참고값으로만 쓴다.
- 이 검사는 후보를 찾기만 하며 `route_segment`나 topology를 변경하지 않는다.

## 끝점별 가장 가까운 원본 LINK

| C5706 vertex | 후보 LINK | 접점 | 거리 | 시군구 | 행정동 | 판정 |
| ---: | ---: | --- | ---: | --- | --- | --- |
| 5740 | 179606 | `line_interior` | 24.095 m | 성동구 | 금호동1가 | `MANUAL_REVIEW` |
| 5752 | 122140 | `start` | 65.594 m | 성동구 | 금호동1가 | `MANUAL_REVIEW` |
| 5855 | 3847 | `line_interior` | 29.238 m | 중구 | 신당동 | `MANUAL_REVIEW` |

## 결론

가장 가까운 미적재 원본 LINK도 24.095m 떨어져 있다. 행정경계 필터로 같은 endpoint가 빠진 단순 누락은 아니며, 근거 없이 직선 connector를 만들거나 자동 스냅하지 않는다. C5706은 분리 component로 유지한다.

## 결과 파일

- `reports/d2_c5706_boundary_candidates.csv`
