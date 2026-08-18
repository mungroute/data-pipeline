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
- REVIEW_OSM_CONFIRM: 16개
- REVIEW_OSM_CONFLICT: 35개
- asphalt → pavement 검토 후보: 14개
- pavement → asphalt 역방향 충돌 후보: 21개

## asphalt → pavement 검토 후보

아래 링크는 OSM에 `footway=sidewalk`와 `surface=paving_stones`가 직접 명시되어 있지만,
OSM만으로 자동 보정하지 않고 QGIS 확인 후 승인 목록에 넣는다.

| segment_id | OSM surface | support | overlap | median distance(m) | p90 angle(°) |
|---:|---|---:|---:|---:|---:|
| 14072 | paving_stones | 0.800 | 0.903 | 2.28 | 2.92 |
| 25629 | paving_stones | 0.923 | 0.954 | 2.54 | 0.33 |
| 33580 | paving_stones | 1.000 | 1.000 | 1.00 | 13.11 |
| 50173 | paving_stones | 0.800 | 0.805 | 1.82 | 12.42 |
| 107801 | paving_stones | 1.000 | 1.000 | 0.97 | 12.52 |
| 112224 | paving_stones | 0.933 | 0.960 | 2.31 | 1.23 |
| 142762 | paving_stones | 0.800 | 0.877 | 0.58 | 10.36 |
| 165921 | paving_stones | 1.000 | 1.000 | 1.13 | 0.91 |
| 169916 | paving_stones | 1.000 | 1.000 | 2.36 | 0.02 |
| 230054 | paving_stones | 1.000 | 1.000 | 1.89 | 1.04 |
| 250285 | paving_stones | 0.867 | 0.883 | 1.08 | 4.83 |
| 254137 | paving_stones | 0.750 | 0.844 | 0.68 | 2.32 |
| 275255 | paving_stones | 1.000 | 1.000 | 0.59 | 3.94 |
| 277250 | paving_stones | 1.000 | 1.000 | 0.70 | 2.60 |

## QGIS 레이어

- `review_to_pavement`: 현재 asphalt이지만 OSM 명시 보도는 pavement인 링크
- `review_to_asphalt`: 현재 pavement이지만 OSM 명시 보도는 asphalt인 링크
- `route_osm_surface_matches`: 확인·충돌을 모두 포함한 전체 정밀 매칭
- `osm_sidewalk_surface_known`: 매칭에 사용한 OSM 원본 보도선

## 적용 제한

- 이 산출물은 검토 목록이며 `d4_sample_surface.csv`, `d4_route_surface.csv`, DB를 변경하지 않는다.
- 대로변 전체 보정은 OSM 미기재 구간이 많으므로 이 14개만으로 끝나지 않는다.
- 기하 기반 대로 후보는 `d4_surface_qa.gpkg`의 `secondary_major_road_candidates`에서 별도로 검토한다.
