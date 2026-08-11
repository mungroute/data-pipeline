# D2 데이터 파이프라인 완료 보고서

- 완료일: 2026-08-11
- 범위: 도보망 topology, routing QA, 수동 connector 검토 준비, 10m 공통 분할점, 산책 세션 API
- pgRouting: 3.8.0
- 내부 공간연산 좌표계: EPSG:5186

## 1. topology 생성 방식

deprecated된 `pgr_createTopology()`과 `pgr_analyzeGraph()`은 사용하지 않는다.

1. Python에서 EPSG:5186 변환과 endpoint 1m 스냅
2. `route_segment_staging` 적재
3. `pgr_extractVertices()`로 `route_vertex` 재생성
4. 실제 LineString 시작점·끝점 geometry로 source/target 연결
5. `pgr_connectedComponents()`와 `pgr_degree()` 검사
6. 검증된 staging을 `route_segment`로 발행
7. `pgr_dijkstra()` 실제 경로 검사

원본 node ID는 추적용으로만 보존하고 topology 생성에는 사용하지 않는다.

## 2. 육안 QA 제외 반영

`config/network_exclusions.csv`의 활성 상태인 `EXCLUDE`,
`EXCLUDE_TEMPORARY`만 staging 적재 전에 제거한다. 원본 processed GeoPackage는
수정하지 않는다.

| 구분 | 수량 |
| --- | ---: |
| 보행 가능 processed LINK | 7,771 |
| 육안 QA 제외 | 5 |
| 최종 routing LINK | 7,766 |

제외된 항목은 실내 1개, 공사장 1개, 사유시설 내부 3개다. D3/D4 분석값이
생긴 이후 자동 재발행으로 후속 데이터를 지우지 않도록 보호 로직도 유지한다.

## 3. 최종 routing QA

| 지표 | 결과 |
| --- | ---: |
| route_segment | 7,766 |
| route_vertex | 6,028 |
| source/target NULL | 0 / 0 |
| endpoint-vertex 불일치 | 0 |
| 미참조 vertex | 0 |
| connected component | 13 |
| 최대 component vertex | 5,966 |
| 최대 component 비율 | 98.971% |
| dead-end | 1,301 |
| 고립 vertex | 0 |
| Dijkstra 경로 edge | 166 |
| Dijkstra 경로 길이 | 6,243.75m |
| 경로 edge 전이 불일치 | 0 |

최대 component 비율은 99% 기준보다 0.029%p 낮지만 공간 무결성 오류는 아니다.
근거 없는 1m 초과 연결로 수치를 맞추지 않는다.

## 4. 분리 component와 connector 정책

- C5706은 서울 전체 원본 LINK를 100m까지 다시 검사했다. 가장 가까운 미적재
  원본 LINK가 24.095m 떨어져 있어 단순 행정경계 누락으로 보지 않고 분리 유지한다.
- C1248과 C3784는 QGIS 검토 GeoPackage를 만들었다.
- topology 재생성 후 현재 vertex ID를 좌표로 다시 찾았다.
  - MR-CONN-001: 1244 ↔ 1223
  - MR-CONN-002: 4134 ↔ 4148
- `manual_connectors_draft`는 의도적으로 비어 있다. 실제 보행로 geometry,
  출입시간, 차단문, 반려견 통행 가능 여부를 확인하고 `APPROVED`로 판정하기
  전에는 DB에 넣지 않는다.

## 5. 10m 공통 분할점

각 segment를 `max(1, round(length_m / 10m))`개 구간으로 등분하고 각 구간의
중심에 점을 하나 생성했다. junction endpoint 중복을 피하면서 전체 총연장
310.12km에 대해 약 10m 밀도를 유지한다.

| 지표 | 결과 |
| --- | ---: |
| segment_sample_point | 31,167 |
| segment당 최소/최대 점 | 1 / 92 |
| 구간 길이 최소/평균/최대 | 1.140 / 9.925 / 14.990m |
| sample 없는 segment | 0 |
| SRID 오류 | 0 |
| LineString 밖 점 | 0 |
| 중복 `(segment_id, seq)` | 0 |
| D3/D4 분석값 선입력 | 0 |

SVF, albedo, emissivity, 지면열 비율, 시간대별 그늘 필드는 D3/D4에서 같은
31,167개 점을 공통 입력으로 사용한다. 분석값이 하나라도 입력된 뒤에는
`make_sample_points.py`가 재생성을 거부한다.

## 6. PowerShell 재현 순서

QGIS Python을 사용하는 새 PowerShell에서는 먼저 다음 환경을 설정한다.

```powershell
$env:PROJ_DATA = "C:\Program Files\QGIS 3.44.12\share\proj"
$env:PROJ_LIB = $env:PROJ_DATA
$env:GDAL_DATA = "C:\Program Files\QGIS 3.44.12\apps\gdal\share\gdal"
$qgisPython = "C:\Program Files\QGIS 3.44.12\apps\Python312\python.exe"
```

최초 구축 또는 sample 분석값이 아직 없을 때의 순서다.

```powershell
& $qgisPython data-pipeline/src/load_segments.py
& $qgisPython data-pipeline/src/build_topology.py
& $qgisPython data-pipeline/src/publish_segments.py
& $qgisPython data-pipeline/src/validate_routing.py
& $qgisPython data-pipeline/src/make_sample_points.py
```

connector 검토 파일은 다음처럼 생성한다. 기존 파일이 있으면 수동 작업 보호를
위해 덮어쓰지 않는다.

```powershell
& $qgisPython data-pipeline/src/prepare_connector_review.py --candidate MR-CONN-001
& $qgisPython data-pipeline/src/prepare_connector_review.py --candidate MR-CONN-002
```

QGIS digitizing과 현장·운영 조건 확인을 마친 뒤에만 실행한다.

```powershell
& $qgisPython data-pipeline/src/validate_connector_draft.py --candidate MR-CONN-001
```

## 7. 산책 세션 API

확정한
[`walk-session-api-contract.md`](../../backend/docs/walk-session-api-contract.md)를
기준으로 `start → points → end` 흐름을 구현했다.

### 구현 범위

- `POST /api/walks/start`
  - 사용자 존재 확인
  - 동일 사용자의 활성 산책 중복 방지
  - `201 Created`와 `Location` 헤더 반환
- `POST /api/walks/{sessionId}/points`
  - 세션 행 쓰기 잠금 후 활성 여부 확인
  - 위도·경도·정확도·기록 시각 검증
  - EPSG:4326 입력 좌표를 PostGIS에서 EPSG:5186으로 변환해 저장
  - 정상 저장 시 `204 No Content` 반환
- `POST /api/walks/{sessionId}/end`
  - 세션 행 쓰기 잠금
  - usable point를 `recorded_at, point_id` 순서로 정렬
  - EPSG:5186 평면거리 합계와 `track_geom` 계산
  - 전체·usable 포인트 수와 소요 시간 반환
  - 재호출 시 기존 종료 결과를 반환하는 멱등 처리

사용자별 활성 산책은 V5 partial unique index를 DB 최종 안전장치로 사용한다.
D2에서는 도보망 맵매칭을 수행하지 않으며, 포인트가 충분하면
`NOT_PERFORMED`, 부족하면 `INSUFFICIENT_POINTS`를 반환한다.

### 통합 QA 결과

2026-08-11 로컬 Spring Boot와 PostgreSQL/PostGIS 환경에서 다음 흐름을
실제 호출하고 DB 결과를 대조했다.

| 검사항목 | 결과 |
| --- | --- |
| 산책 시작 | `201 Created` |
| 서로 다른 GPS 포인트 2건 저장 | 각각 `204 No Content` |
| 산책 종료 | `200 OK` |
| 전체 / usable / 서로 다른 usable 포인트 | 2 / 2 / 2 |
| 계산 거리 | 56.7m |
| `track_geom` SRID | 5186 |
| `track_geom` 점 수 / geometry 유효성 | 2 / 유효 |
| 잘못된 SRID 포인트 | 0 |
| 종료 상태 | `NOT_PERFORMED` |
| 종료 API 재호출 | 기존 `endedAt`, 거리, 시간 그대로 `200 OK` |
| 종료된 세션에 포인트 추가 | `409 WALK_SESSION_ALREADY_ENDED` |
| 포인트 1건 세션 종료 | `distanceM=0.0`, `INSUFFICIENT_POINTS` |

종료 API native projection에서 PostgreSQL `TIMESTAMPTZ`가 `Instant`로
반환되는 점을 반영해, API 응답 경계에서 UTC `OffsetDateTime`으로 변환한다.
Java 컴파일, Spring Context 테스트와 PostGIS 집계 SQL 실행 계획 검증도
통과했다.

## 8. D2 완료 판정

- deprecated된 `pgr_createTopology()`과 `pgr_analyzeGraph()`에 의존하지 않음
- 좌표 기반 endpoint 1m 스냅과 topology 구성 완료
- `pgr_connectedComponents()`, `pgr_degree()`, `pgr_dijkstra()` 검증 완료
- 약 10m 공통 분할점 생성 및 DB 적재 완료
- 산책 세션 `start → points → end` 통합 흐름과 오류·멱등 처리 확인 완료

따라서 D2 필수 범위를 완료로 판정한다. 수동 connector의 현장·운영 조건
확인과 D6 맵매칭은 후속 범위로 유지한다.
