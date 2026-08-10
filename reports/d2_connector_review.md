# D2 수동 connector 검토 준비

## 생성한 검토 파일

- `data/interim/network/manual_connector_MR-CONN-001_qa.gpkg`
- `data/interim/network/manual_connector_MR-CONN-002_qa.gpkg`

각 GeoPackage에는 다음 레이어가 있다.

- `connector_endpoints`: 후보 좌표를 현재 topology vertex ID에 다시 매칭한 두 점
- `route_segment_context`: endpoint 주변 100m의 현재 도보망
- `connector_review_area`: QGIS 확인 범위
- `manual_connectors_draft`: 실제 보행로를 직접 그리는 빈 LineString 레이어

## 현재 vertex 재매칭 결과

| 후보 | component 쪽 | main network 쪽 |
| --- | ---: | ---: |
| MR-CONN-001 · C1248 | 1244 | 1223 |
| MR-CONN-002 · C3784 | 4134 | 4148 |

QA 당시 vertex ID는 topology 재생성으로 달라졌으므로 사용하지 않는다. 위 값도
향후 topology를 다시 만들면 바뀔 수 있으며, 검토 파일을 재생성해 좌표로 다시
찾아야 한다.

## 승인 조건

1. QGIS에서 실제 보행로 형상을 따라 `manual_connectors_draft`에 LineString 한 건을 그린다.
2. `candidate_id`를 해당 후보 ID로 입력한다.
3. 출입시간·차단문 여부를 `access_note`에 기록한다.
4. 반려견 통행 가능 확인 시 `pet_allowed=1`로 입력한다.
5. 모든 확인이 끝난 경우에만 `review_status=APPROVED`로 입력한다.
6. `validate_connector_draft.py`로 양 endpoint 1m 오차, geometry 및 필수 속성을 검사한다.

승인 전에는 connector를 `route_segment`에 넣지 않는다. 현재 두 후보 모두 검토
준비 상태이며 최종 topology에는 포함되지 않았다.
