# D5 OSM 명시 보도 재질 QA

## 정책

- highway=footway, footway=sidewalk, surface가 명시된 선만 사용한다.
- 도로 자체의 surface 태그는 보도 재질로 사용하지 않는다.
- 거리 3m, 국소 방향차 20도, 링크 지지율/중첩률 70%를 모두 통과해야 한다.
- OSM 결과는 REVIEW 증거이며 이 단계에서는 재질과 DB를 변경하지 않는다.

## 결과

- 사용한 OSM 명시 보도: 29개
- 정밀 매칭된 멍루트 링크: 51개
- OSM asphalt: 7개
- OSM pavement: 22개
- REVIEW_OSM_CONFIRM: 30개
- REVIEW_OSM_CONFLICT: 21개
- asphalt → pavement 검토 후보: 0개
- pavement → asphalt 역방향 충돌 후보: 21개

## asphalt → pavement 검토 후보

아래 링크는 OSM에 `footway=sidewalk`와 `surface=paving_stones`가 직접 명시되어 있지만,
OSM만으로 자동 보정하지 않고 QGIS 확인 후 승인 목록에 넣는다.

| segment_id | OSM surface | support | overlap | median distance(m) | p90 angle(°) |
|---:|---|---:|---:|---:|---:|

## QGIS 레이어

- `review_to_pavement`: 현재 asphalt이지만 OSM 명시 보도는 pavement인 링크
- `review_to_asphalt`: 현재 pavement이지만 OSM 명시 보도는 asphalt인 링크
- `route_osm_surface_matches`: 확인·충돌을 모두 포함한 전체 정밀 매칭
- `osm_sidewalk_surface_known`: 매칭에 사용한 OSM 원본 보도선

## 최초 후보 산출 단계의 적용 제한

- OSM 매칭 자체는 검토 목록만 만들며, 사용자 검증 전에는 `d4_sample_surface.csv`, `d4_route_surface.csv`, DB를 변경하지 않는다.
- 대로변 전체 보정은 OSM 미기재 구간이 많으므로 이 0개만으로 끝나지 않는다.
- 기하 기반 대로 후보는 `d4_surface_qa.gpkg`의 `secondary_major_road_candidates`에서 별도로 검토한다.

## 사용자 검증 및 최종 처리

- 검증일: 2026-08-13
- 최초 `asphalt → pavement` 검토 후보 14개를 QGIS 및 거리뷰로 모두 확인했다.
- 확인 결과: 14개 중 14개가 보도블록이었다.
- 14개 링크는 `config/surface_overrides.csv`에 사용자 검증 override로 기록하고 `pavement`로 재산출했다.
- 재산출 후 이 보고서의 `asphalt → pavement 검토 후보`가 0개인 것은 누락이 아니라 승인 후보가 모두 반영되었기 때문이다.
- `pavement → asphalt` 역방향 충돌 21개는 사용자 확인 전이므로 변경하지 않았다.
