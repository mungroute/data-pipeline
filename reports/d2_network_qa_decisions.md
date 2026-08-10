# D2 도보 네트워크 QA 결정 기록

- 기록일: 2026-08-10
- 원칙: 원본 LINK를 직접 삭제하거나 DB에서 임의 수정하지 않는다.
- 제외와 connector는 `segment_id` 및 좌표 기반 설정 파일로 관리한다.
- component ID와 vertex ID는 topology 재생성 후 달라질 수 있으므로 QA 참고값으로만 사용한다.

## 1. 라우팅 제외 목록

실행 가능한 설정 파일:
[`config/network_exclusions.csv`](../config/network_exclusions.csv)

| component | segment_id | 판정 | 근거 |
| ---: | ---: | --- | --- |
| 344 | 271404 | `INDOOR` | 원본 `is_indoor=1` |
| 131 | 74299 | `CONSTRUCTION` | 위성영상에서 공사장 내부로 확인 |
| 2973 | 10384 | `PRIVATE_ACCESS` | 호텔·회원제 클럽 내부 시설 |
| 2973 | 118064 | `PRIVATE_ACCESS` | 호텔·회원제 클럽 내부 시설 |
| 2973 | 147186 | `PRIVATE_ACCESS` | 사유 시설 내부 폐합 LINK |

공사장 제외 항목은 영구 삭제가 아니며 최신 영상 또는 현장 상태에 따라
재검토한다. C466 서울역 교통섬은 횡단보도 확인 전까지 분리 상태를
유지하지만, 이번 확정 제외 목록에는 넣지 않았다.

## 2. 수동 connector 후보

실행 가능한 후보 파일:
[`config/manual_connector_candidates.csv`](../config/manual_connector_candidates.csv)

### MR-CONN-001 — C1248

- 후보 연결: vertex 1248 → vertex 1227
- 거리: 30.319 m
- 위치: 남산도서관·안중근의사기념관 주변
- 근거: 위성영상과 지도에서 포장 동선이 이어지는 것으로 확인
- 남은 확인: 시설 출입시간, 차단문, 반려견 통행 가능 여부
- 상태: `REVIEW_ACCESS`

### MR-CONN-002 — C3784

- 후보 연결: vertex 4141 → vertex 4155
- 거리: 7.292 m
- 위치: 청계천 보행로
- 근거: 하천 보행 동선이 이어지고 일부 LINK가 횡단보도 속성을 가짐
- 남은 확인: 지상·하부 레벨과 실제 계단·램프 위치
- 상태: `REVIEW_LEVEL`

두 후보 모두 최근접 vertex 사이를 자동 직선 연결하지 않는다. 실제 보행
동선과 레벨을 확인한 geometry만 별도 보정 데이터로 추가한다.

## 참고 판정

- C5706: 대현산배수지공원의 공공 보행망. 제외하지 않음. 중구·성동구
  경계에서 누락된 원본 LINK 복구 대상으로 분류.
- C466: 서울역 교통섬. 공식 횡단보도 또는 지하 연결 확인 전까지 분리 유지.
