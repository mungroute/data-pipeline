# D2 도보 네트워크 topology 품질검사

- 검사일: 2026-08-10
- 좌표계: EPSG:5186
- endpoint 스냅 허용오차: 1 m
- pgRouting: 3.8.0
- topology 구성: `pgr_extractVertices()`
- 연결성 검사: `pgr_connectedComponents()`
- degree 검사: `pgr_degree()`
- 경로 검사: `pgr_dijkstra(..., directed := false)`

## 전처리 및 적재 지표

| 지표 | 결과 |
| --- | ---: |
| 중구 입력 LINK | 7,797 |
| 보행 불가 제외 | 26 |
| 보행 가능 processed LINK | 7,771 |
| 육안 QA 제외 LINK | 5 |
| 최종 routing 대상 LINK | 7,766 |
| EPSG:5186 변환 | 통과 |
| endpoint 레코드 | 15,542 |
| 스냅 전 고유 endpoint | 6,038 |
| 스냅 후 canonical endpoint | 6,035 |
| 이동한 endpoint | 4 |
| 최대 endpoint 이동거리 | 0.254 m |
| QA 제외 전 canonical endpoint | 6,035 |
| 생성된 `route_vertex` | 6,028 |
| `route_segment_staging` | 7,766 |
| 최종 `route_segment` | 7,766 |

## routing graph 품질 지표

| 지표 | 결과 |
| --- | ---: |
| connected component | 13 |
| 최대 component vertex | 5,966 |
| 최대 component 비율 | 98.971% |
| 최대 component LINK | 7,704 |
| 최대 component 밖 vertex | 62 |
| 최대 component 밖 LINK | 62 |
| dead-end (`degree=1`) | 1,301 |
| 고립 vertex (`degree=0`) | 0 |
| degree 최소/평균/최대 | 1 / 2.577 / 5 |

QA에서 확정한 실내·공사장·사유지 LINK 5개를 제외한 뒤 component는
16개에서 13개로 감소했고 최대 component 비율은 98.857%에서 98.971%로
상승했다. 엄격한 `>= 99%` 기준에는 0.029%p 부족하다. C1248·C3784는
검토 후보로만 유지하고 근거 없이 1m를 초과하여 연결하지 않는다.

## 실제 경로 검증

최대 component의 서쪽 끝과 동쪽 끝에 가까운 vertex를 사용했다.

| 지표 | 결과 |
| --- | ---: |
| 시작 vertex | 1 |
| 종료 vertex | 6,028 |
| 경로 LINK | 166 |
| 경로 비용(길이) | 6,243.75 m |
| 연결되지 않은 edge 전이 | 0 |

`pgr_dijkstra()`가 실제 경로를 반환했고, 경로의 각 LINK가 현재 node와
다음 node를 source/target으로 연결하는지 검사한 결과 불일치는 없었다.

## 판정

- topology 필수 무결성 검사 통과
- `network_exclusions.csv`의 활성 제외 5건 적용 완료
- 최대 component에서 실제 경로 탐색 성공
- 소규모 분리 component는 추후 원본 속성·공간 위치를 검토할 QA 항목으로 유지
- 자동 연결이나 임의 삭제는 수행하지 않음
- 육안 QA 결정과 보정 후보는
  [`d2_network_qa_decisions.md`](d2_network_qa_decisions.md)에 기록
- C5706의 서울 전체 원본 LINK 재검색 결과는
  [`d2_c5706_boundary_check.md`](d2_c5706_boundary_check.md)에 기록
- 수동 connector 검토 절차는
  [`d2_connector_review.md`](d2_connector_review.md)에 기록
- 10m 공통 분할점 결과는
  [`d2_sample_points_qa.md`](d2_sample_points_qa.md)에 기록
- 산책 API 시작 전 D2 데이터 파이프라인 종합 결과는
  [`d2_data_pipeline_completion.md`](d2_data_pipeline_completion.md)에 기록
