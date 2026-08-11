# D3 DSM 생성 QA

## 생성 방식

- 기준 지형: 1m DTM을 유효 픽셀 평균으로 변환한 2m DTM
- 건물 자료: GIS 건물 통합정보
- 자치구 필터: `A23 = 11140` (서울 중구)
- 건물 높이 필드: `A16` (m)
- 높이 보정: `A26 × 3.0m`, 둘 다 없으면 중구 평균 층수 적용
- 계산식: `DSM = DTM + 건물 높이`
- 건물 겹침 픽셀: 가장 큰 양수 높이 사용
- DTM Alpha가 0이거나 Band 1 표고가 0인 픽셀: 분석 제외

## 입력·출력

| 구분 | 경로 |
| --- | --- |
| DTM | `C:\project\mungroute\data-pipeline\data\raw\dem\jung_gu_1m.tif` |
| 2m DTM | `C:\project\mungroute\data-pipeline\data\processed\surface\junggu_dtm_2m.tif` |
| 건물 SHP | `C:\project\mungroute\data-pipeline\data\raw\building\AL_D010_11_20260719.shp` |
| 건물 높이 래스터 | `C:\project\mungroute\data-pipeline\data\processed\surface\junggu_building_height_2m.tif` |
| 건물 높이 품질 래스터 | `C:\project\mungroute\data-pipeline\data\processed\surface\junggu_building_height_quality_2m.tif` |
| DSM | `C:\project\mungroute\data-pipeline\data\processed\surface\junggu_dsm_2m.tif` |

## 격자 검사

| 지표 | 결과 |
| --- | ---: |
| CRS | EPSG:5186 호환 TM 중부원점 2010 |
| 크기 | 2,883 × 1,564 |
| 픽셀 | 2.0m × 2.0m |
| 범위 | 196603.000, 549370.000, 202369.000, 552498.000 |
| 유효 DTM 픽셀 | 2,500,400 |

## 건물 검사

| 지표 | 결과 |
| --- | ---: |
| DTM 범위 건물 | 20,532 |
| A16 실측 높이 | 6,675 |
| A26 층수 × 3m 보정 | 7,877 |
| 중구 평균 층수 × 3m 보정 | 5,980 |
| 중구 평균 지상층수 | 3.195층 |
| 건물 높이 최소/중앙/최대 | 2.30 / 9.59 / 156.15m |
| 건물 높이 픽셀 | 736,589 (29.459%) |

## 래스터 결과

| 지표 | 결과 |
| --- | ---: |
| DTM 표고 최소/최대 | 10.523 / 271.486m |
| 적용된 건물 높이 최대 | 156.150m |
| DSM 표고 최소/최대 | 10.523 / 281.072m |

## 다음 QA

1. QGIS에서 DTM, 건물 높이, DSM을 겹쳐 건물 footprint 정합성을 확인한다.
2. 품질 래스터 1은 A등급, 2·3은 높이 추정이 포함된 B등급 근거로 사용한다.
3. DSM QA 통과 후 수목 수고·수관너비를 사용해 CDSM을 생성한다.
4. DSM/CDSM을 분리 입력으로 사용해 건물·수목 그림자를 각각 계산한다.
