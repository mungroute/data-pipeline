# D5 남은 작업 및 완료 계획

작성 기준일: 2026-08-13

이 문서는 현재 멍루트 D5 진행 상태를 기준으로, 다음 작업부터 D5 완료까지의
실행 순서와 종료 조건을 정리한다.

## 1. 현재까지 완료된 범위

- D4 재질 분류 정책 수정 완료
  - 차량 통행 링크: `asphalt`
  - 보행 전용 링크: `pavement`
  - 토지피복의 자연 피복은 재질을 바로 `grass`/`soil`로 바꾸지 않고 문맥
    정보로만 사용
  - 실제 잔디·흙길은 `config/surface_overrides.csv`에서 명시적으로 관리
- 수정된 D4 결과를 DB에 반영하고 검증 완료
  - `route_segment`: 7,766행
  - `segment_sample_point`: 31,167행
  - 필수 D4 값 NULL: 0행
- D5 입력 전처리 완료
  - 실제 1차 측정: 12쌍
  - 모의 2차 측정: 보정 사용 0행
  - 2026-08-11 서울 ASOS 108번의 09·12·15·18시 관측값 결합
- D5 열모델 보정 완료
  - 그늘 유효 일사 투과율: 0.461
  - 실측 ΔT MAE/RMSE: 3.95°C / 5.41°C
  - 모델 신뢰도: `LOW`
- 전 구간 계산 및 1차 통합 QA 산출물 생성 완료
  - 샘플: 31,167개
  - 링크: 7,766개
  - 전체 계산 범위: 26.34~55.74°C
  - 온도 NULL: 0개
  - QGIS 산출물:
    `data/interim/thermal/d5_thermal_qa_v2.gpkg`

`LOW`는 계산 실패를 뜻하지 않는다. 동일 날짜의 실제 기상은 사용했지만,
현장 실측이 12쌍이고 측정 위치가 서비스 대상지 밖 회사 주변이라는 한계를
사용자와 후속 개발자가 알 수 있도록 보존하는 품질 표기다.

## 2. 다음 작업부터 D5 완료까지의 순서

### D5-6. 정정된 입력으로 최종 재현 실행

**완료: 2026-08-13 08:43 KST**

최종 실행 결과:

- DB 입력 확인: `route_segment` 7,766행, `segment_sample_point` 31,167행
- 보정 사용: 실제 측정 12쌍, 모의 측정 0행
- 그늘 유효 일사 투과율: 0.461
- 샘플/링크: 31,167개 / 7,766개
- QGIS 후보: 3,182개
- 샘플 온도 범위: 26.34~55.74°C
- 링크 온도 범위: 26.35~55.55°C
- 4개 시각 온도 NULL: 샘플 0개, 링크 0개
- 재질: asphalt 22,645개, pavement 8,522개, 허용 외 재질 0개
- 기상 상태: 31,167개 모두 `OBSERVED_ASOS_SAME_DATE`
- 모델 신뢰도: 31,167개 모두 `LOW`
- 회귀 확인 표본 `sample_id=1237`, `segment_id=10940`: `pavement`
- 자동 테스트: 37개 통과
- 최종 QA 파일:
  `data/interim/thermal/d5_thermal_qa_final.gpkg`

D4 재질 규칙이 최종 확정된 상태에서 D5 전체 파이프라인을 한 번 더 순서대로
실행한다. 과거 재질값이 남은 산출물을 DB에 넣지 않기 위한 최종 재현 단계다.

PowerShell 실행 순서:

```powershell
Set-Location C:\project\mungroute\data-pipeline

$env:PROJ_DATA = "C:\Program Files\QGIS 3.44.12\share\proj"
$env:PROJ_LIB = $env:PROJ_DATA
$env:GDAL_DATA = "C:\Program Files\QGIS 3.44.12\apps\gdal\share\gdal"

$qgisPython = "C:\Program Files\QGIS 3.44.12\apps\Python312\python.exe"

& $qgisPython src\prepare_d5_inputs.py
& $qgisPython src\calibrate_thermal_model.py
& $qgisPython src\calc_thermal.py
& $qgisPython src\prepare_d5_qa.py `
  --output data\interim\thermal\d5_thermal_qa_final.gpkg `
  --overwrite
```

확인할 값:

- 실제 측정 12쌍만 보정에 사용
- 모의 측정 보정 사용 0행
- 기상 상태 `OBSERVED_ASOS_SAME_DATE`
- 샘플 31,167개, 링크 7,766개
- 온도 NULL 0개
- 재질은 현재 정책상 기본적으로 `asphalt`와 `pavement`이며, 별도 override가
  있을 때만 자연 재질 허용

### D5-7. 최종 QGIS 통합 QA

**완료: 2026-08-13**

- 사용자 확인 대상 OSM 명시 보도 후보 14개를 모두 `pavement`로 확정했다.
- 승인 링크 14개와 소속 샘플 80개의 재질 및 물성값 일치를 확인했다.
- 회귀 사례 `69414`, `247486`은 `asphalt`로 유지했다.
- 최종 확인 결과 승인 대상이 모두 올바르게 변경된 것을 QGIS에서 확인했다.

QGIS에서 `d5_thermal_qa_final.gpkg`를 열고 다음 레이어를 확인한다.

- `sample_thermal`: 약 10m 샘플별 계산값
- `route_thermal`: 링크별 길이 가중 집계값
- `qa_candidates`: 자동 선별된 고온·저온·품질 확인 후보

필수 확인 항목:

1. 기존 오분류 지점
   - `sample_id=1237`, `segment_id=10940`이 `pavement`인지 확인
   - 신당 푸르지오·시청역 앞과 같은 일반 보도블록이 자연 재질로 남지 않았는지
     표본 확인
2. 15시 고온 후보
   - `HIGH_P99_15`가 실제 보행 공간에 놓이는지 확인
   - 건물 내부·수면·명백한 비보행 영역이면 원인 기록
3. 09시·18시 저온 후보
   - 공원 인접, 건물/수목 그늘, 낮은 SVF와 공간적으로 설명되는지 확인
4. 구조물 아래 구간
   - D3의 강제 건물 그늘 구간이 양지로 계산되지 않았는지 확인
5. 시간 변화
   - 일반적으로 09시보다 12·15시가 높고 18시에 다시 낮아지는지 확인
   - 일부 그늘 전환 구간은 예외가 가능하지만 `qa_reason`에 설명할 수 있어야 함
6. 재질 효과
   - 같은 기상·그늘 조건에서 `asphalt`가 `pavement`보다 대체로 높게 나타나는지
     확인

최종 QA에서 발견한 명백한 입력 오류만 수정한다. 실제로 가능한 극값은 임의로
삭제하지 않고 보고서에 위치와 이유를 남긴다.

### D5-8. 자동 검사와 계산식 최종 검토

**완료: 2026-08-13**

- 전체 자동 테스트: 49개 통과
- D5 자동 산출물 검증: PASS
- 샘플/링크: 31,167개 / 7,766개
- 온도 범위: 26.34~55.74°C, 온도 NULL 0개
- 시간대별 중앙값: 09시 29.93°C, 12시 50.72°C, 15시 50.20°C,
  18시 32.96°C
- D3 범위 밖 그림자 미관측 샘플: 469개, 일부 시각만 결측인 샘플: 0개
- 그림자 미관측 링크 비율: 각 시각 12개 링크에서 NULL 유지
- 링크 길이 가중 집계: PASS, CSV 반올림 최대 오차 0.01°C
- 확정 재질 분포: asphalt 20,443개, pavement 10,724개
- 재질 품질 분포: A 80개, B 29,776개, C 1,311개
- 검증 보고서: `reports/d5_thermal_validation.md`
- 확정 QGIS 산출물: `data/interim/thermal/d5_thermal_d5_8_qa.gpkg`

그림자 원본 결측은 NULL로 보존하고, 온도 계산에서는 확인되지 않은 냉각 효과를
주지 않도록 양지로 취급한다. 링크 그림자 비율은 관측값만 분모에 포함하며 해당
링크의 모든 샘플이 미관측이면 NULL을 유지한다.

다음 불변 조건을 테스트와 통계로 다시 확인한다.

- 일사량 증가 시 계산 온도가 상승한다.
- 풍속 증가 시 계산 온도가 하락한다.
- 같은 조건에서 그늘은 양지보다 낮다.
- 알베도가 높아지면 흡수 단파복사가 감소한다.
- SVF와 그림자를 동일한 일사 항에서 중복 차감하지 않는다.
- 공원 냉각은 200m 이내 최대 1.5°C를 넘지 않는다.
- 모든 시각의 값이 유한값이며 허용 범위를 벗어나지 않는다.
- 링크 온도는 단순 샘플 평균이 아니라 샘플 대표구간 길이 가중평균이다.

테스트 명령:

```powershell
& $qgisPython -m unittest discover -s tests -p "test_*.py"
```

실측 양지-그늘 ΔT 잔차가 10°C를 넘는 관측이 있다는 경고는 숨기지 않는다.
현재 표본만으로 계수를 과적합하지 않고 `LOW` 신뢰도를 유지한다.

### D5-9. D5 DB 스키마 추가

현재 DB에는 D4 입력과 단일 `temp_grade`만 있고, D5의 시간대별 실제 계산
온도를 저장할 컬럼이 없다. 이미 적용된 `V1`~`V6`는 수정하지 않고 다음
migration을 새로 만든다.

예정 파일:

`backend/src/main/resources/db/migration/V7__add_route_thermal_fields.sql`

저장 대상은 런타임 라우팅에 필요한 링크 집계 결과로 한정한다.

- `surface_temp_09_c`
- `surface_temp_12_c`
- `surface_temp_15_c`
- `surface_temp_18_c`
- `surface_temp_peak_c`
- `thermal_model_confidence`
- `thermal_weather_date`
- `thermal_updated_at`

약 10m 샘플별 온도는 QA·재계산용 파일에 유지하고 DB에는 중복 저장하지 않는다.
기존 `temp_grade`는 15시 대표 등급으로 사용할지, 런타임에서 온도로부터 계산할지
적재 구현 전에 한 가지 정책으로 고정한다. 권장안은 시간대별 원시 온도를 DB에
저장하고 등급은 요청 시간에 계산하는 방식이다.

### D5-10. 링크 온도 적재기 구현 및 DB 반영

새 파일 `src/load_thermal.py`를 작성한다.

적재 정책:

1. `route_thermal.csv`가 정확히 7,766행인지 선검증
2. `segment_id` 중복·NULL·DB 미존재 ID 검사
3. 09·12·15·18시 온도와 peak의 유한값·허용 범위 검사
4. 임시 staging 또는 PostgreSQL COPY를 사용해 한 트랜잭션으로 적재
5. 7,766행 전체가 갱신되지 않으면 롤백
6. `thermal_model_confidence='LOW'`, 기상일 `2026-08-11`을 함께 기록

개발 DB 반영 전 Flyway를 먼저 실행해 `V7` 성공을 확인한다. 이미 적용된
migration을 수정해서 checksum mismatch를 만들지 않는다.

### D5-11. DB 반영 후 검증

아래 조건을 모두 만족해야 한다.

- `route_segment` 전체 7,766행에 4개 시각 온도가 존재
- 4개 시각 온도 NULL 0행
- `surface_temp_peak_c`가 4개 시각 최대값과 일치
- 파일의 최소·최대·평균과 DB 집계가 반올림 오차 범위에서 일치
- 임의 표본과 고온/저온 후보의 CSV·QGIS·DB 값이 일치
- D4의 `surface_type`, SVF, 물성값이 D5 적재로 변경되지 않음
- Spring Boot 재기동 후 Flyway validation과 health API 정상

### D5-12. backend 연결 최소 검증

D5 종료 범위에서는 전체 추천 API를 완성하지 않아도 된다. 다만 backend가
링크의 시간대별 온도를 읽을 수 있어야 한다.

- `route_segment` 매핑 또는 전용 projection에 새 온도 필드 연결
- 요청 시각을 09·12·15·18시 중 사용할 기준 시각으로 매핑하는 규칙 작성
- 존재하지 않는 온도는 조용히 0으로 취급하지 않고 명시적으로 실패 또는 폴백
- repository/integration test에서 대표 링크 한 건의 온도를 조회

실제 열쾌적 경로 비용의 가중치 튜닝과 사용자별 추천은 다음 개발 범위로 넘길 수
있지만, 어떤 온도 컬럼을 읽을지는 D5에서 고정한다.

### D5-13. 보고서와 산출물 마감

최종적으로 다음 문서를 갱신 또는 생성한다.

- `reports/d5_input_qa.md`
- `reports/d5_calibration_qa.md`
- `reports/d5_thermal_compute_qa.md`
- `reports/d5_integrated_qa.md`
- `reports/d5_completion.md`

`d5_integrated_qa.md`에 남아 있는 과거 문구인 `asphalt/pavement/soil` 확인 항목은
현재 override 정책에 맞게 수정한다. DB 반영을 마친 뒤에는 “DB 반영 전” 문구도
실제 적재 시각·행 수·검증 결과로 교체한다.

대용량 GPKG, raw 기상, 실제 실측 원본은 Git 추적 대상인지 다시 확인하고 원본
데이터가 실수로 커밋되지 않게 한다. 커밋은 사용자가 수행한다.

## 3. D5 완료 기준

다음 조건을 모두 만족하면 D5 완료로 판정한다.

- [x] 정정된 D4 재질 결과로 D5 전체 파이프라인을 재현 실행했다.
- [x] 실제 측정 12쌍만 보정에 사용했고 모의 측정은 보정에서 제외했다.
- [x] 2026-08-11 ASOS 09·12·15·18시 관측을 사용했다.
- [x] 샘플 31,167개와 링크 7,766개에 온도 결측이 없다.
- [x] 최종 QGIS 표본 QA에서 재질·그늘·공원·SVF 영향이 설명 가능하다.
- [x] 계산식 불변 테스트와 전체 자동 테스트가 통과한다.
- [x] 신규 Flyway migration으로 시간대별 링크 온도 스키마를 추가했다.
- [x] DB 7,766행의 온도와 CSV/QGIS 결과가 일치한다.
- [x] backend에서 대표 링크의 시간대별 온도를 조회할 수 있다.
- [x] `LOW` 신뢰도와 실측 한계를 최종 보고서에 명시했다.
- [x] `reports/d5_completion.md`에 실행 명령, 통계, QA, DB 검증을 기록했다.

## 4. 바로 이어서 할 한 단계

D5-9~D5-12까지 완료되어 위 완료 기준을 모두 충족했다. 다음 작업은 실제 경로
탐색에서 `RouteThermalService`가 선택한 시간대별 온도를 열쾌적 비용에 반영하고,
추천 API 응답에 기준 시각·온도·모델 신뢰도·기상 기준일을 포함하는 단계다.
