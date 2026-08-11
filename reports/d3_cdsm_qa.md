# D3 CDSM 생성 QA

## 생성 방식

- 위치 기준: 최신 서울시 가로수 위치 CSV
- 치수 기준: 상세 WGS1984 CSV의 `수고`, `수관너비`
- 직접 결합: 최신 위치에서 5.0m 이내의 가장 가까운 상세 조사점
- 미결합 보정: 수종별 중앙값, 해당 수종이 없으면 중구 전체 중앙값
- 수관 형상: `수관너비 ÷ 2` 반경의 원
- 수관 겹침: 높이를 더하지 않고 가장 높은 수관 사용
- 공간 필터: 중구 2m DTM Alpha 유효 영역
- CDSM 값: 지면 기준 수관 높이(m)

## 입력·출력

| 구분 | 경로 |
| --- | --- |
| 최신 가로수 위치 | `C:\project\mungroute\data-pipeline\data\raw\tree\서울시 가로수 위치 정보(경도, 위도).csv` |
| 상세 수고·수관너비 | `C:\project\mungroute\data-pipeline\data\raw\tree\서울시 가로수 위치정보 (좌표계_ WGS1984).csv` |
| 기준 2m DTM | `C:\project\mungroute\data-pipeline\data\processed\surface\junggu_dtm_2m.tif` |
| CDSM | `C:\project\mungroute\data-pipeline\data\processed\surface\junggu_cdsm_2m.tif` |
| 치수 품질 래스터 | `C:\project\mungroute\data-pipeline\data\processed\surface\junggu_cdsm_quality_2m.tif` |
| QGIS QA GeoPackage | `C:\project\mungroute\data-pipeline\data\interim\surface\d3_cdsm_qa.gpkg` |

## 입력·결합 QA

| 지표 | 결과 |
| --- | ---: |
| 최신 입력 행 | 8,536 |
| 유효 경위도 행 | 8,536 |
| 중복 좌표 제거 | 1 |
| DTM 유효 영역 수목 | 8,448 |
| DTM 유효 영역 밖 | 87 |
| 5.0m 상세 치수 직접 결합 | 5,489 |
| 수종별 중앙값 보정 | 2,658 |
| 전체 중앙값 보정 | 301 |
| 상세 입력 행 | 7,745 |
| 상세 유효 좌표·치수 | 7,724 |
| 상세 수고·수관폭 0/결측 | 21 |

## 치수·래스터 QA

| 지표 | 결과 |
| --- | ---: |
| 수고 최소/중앙/최대 | 3.00 / 10.00 / 40.00m |
| 수관너비 최소/중앙/최대 | 1.00 / 6.00 / 30.00m |
| 수관 픽셀 | 66,604 |
| 수관 합집합 면적 | 266,416.0m² |
| 개별 원 면적 합 | 346,381.8m² |
| 합집합/개별 면적 비율 | 0.769 |
| 유효 DTM 중 수관 픽셀 비율 | 2.664% |

## 품질 코드

- `1`: 최신 위치와 5.0m 이내 상세 조사점 치수
- `2`: 상세 자료의 동일 수종 중앙값
- `3`: 상세 자료 전체 중앙값

## QGIS 육안 QA

1. `d3_cdsm_qa.gpkg`의 `tree_canopy_points`를 불러온다.
2. `tree_review_candidates`를 불러와 큰 수관·큰 수고·전체 중앙값 보정 후보를 먼저 확인한다.
3. `tree_canopy_points`의 `quality`를 분류해 1·2·3의 위치 분포를 확인한다.
4. `junggu_cdsm_2m.tif`를 반투명으로 올려 대표 도로 3~5곳에서 실제 가로수 열과 비교한다.
