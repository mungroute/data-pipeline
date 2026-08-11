# D3 그늘 데이터 파이프라인 완료 보고서

## 완료 범위

- DTM, DSM, CDSM으로 2026-06-21 09/12/15/18시 최종 그림자 생성
- 지형 `R`, 건물 `B`, 수목 `T`, 일조 `N`의 상호 배타적 원인 분리
- `segment_sample_point` 시간대별 `is_shaded_*`, `shade_src_*` 반영
- `route_segment` 시간대별 tree/building/total 그늘 비율 집계
- 수동 DSM 중첩 판정 적용 및 멱등성 검증

## 핵심 결과

| 항목 | 결과 |
|---|---:|
| route_segment | 7,766 |
| segment_sample_point | 31,167 |
| 유효 그림자 sample | 30,698 |
| 래스터 범위 밖/NoData sample | 469 |
| 범위 밖 sample을 포함한 segment | 65 |
| 유효 sample이 하나도 없는 segment | 12 |
| FORCE_BUILDING_SHADE 적용 sample | 85 |
| SAMPLE_NEAREST_GROUND 적용 sample | 21 |
| 최근접 지면 최대 이동 | 4.47m |

원인 분리 후의 `R ∪ B ∪ T`는 기존 최종 그림자 래스터와 네 시각 모두 셀 단위로
완전히 일치했다. DB 재실행 시 `changed_before_update=0`, 잘못된 코드·boolean 불일치·
부분 NULL·구간 집계 불일치가 모두 0으로 확인됐다.

## 데이터 커버리지 정책

원본 DEM과 Alpha 경계 밖에 있는 469개 sample은 가장 가까운 셀로 강제 이동하지
않았다. 일부는 유효 경계에서 250m 이상 떨어져 있어 이동 샘플링이 다른 장소의 값을
붙일 수 있기 때문이다. 이 점들은 네 시각 필드를 모두 NULL로 보존하고, 구간 비율의
분모에서는 제외했다. 유효점이 없는 12개 segment의 12개 비율 역시 NULL이다.

## 산출물

- `src/calc_shadow_sources.py`: 원인 래스터 생성과 기존 total 완전 일치 검증
- `src/sample_shadow.py`: dry-run, 수동 정책, 트랜잭션 반영, 구간 집계, DB QA
- `reports/d3_shadow_source_qa.md`: 원인별 픽셀 통계
- `reports/d3_shadow_visual_qa.md`: 합성 장애물 및 실제 건물 QA
- `reports/d3_shade_sampling_qa.md`: sample·DB 반영 통계
- `tests/test_shadow_sources.py`, `tests/test_sample_shadow.py`: 분류·길이·좌표·최근접 지면 회귀 테스트

## 남은 외부 확인

- `segment_id=221983`은 근거가 제한되어 `PROVISIONAL` 상태를 유지한다.
- 실제 현장 파일럿 검증은 이동·현장 조건이 필요한 별도 외부 작업으로 남긴다.
- D4 범위인 SVF, 재질, albedo, 공원 접근성은 이번 작업에 포함하지 않았다.

## 판정

D3 계산·DB 반영·자동 QA는 완료했다. 다만 서비스에서 12개 경계 segment까지 반드시
그늘값이 필요하다면, 해당 구간을 포함하는 DEM·건물·수목 원자료 확장이 선행되어야 한다.
