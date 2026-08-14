# D5 완료 및 DB 적재 검증

## 실행 결과

- 판정: **PASS**
- 실행 시각: 2026-08-14T11:44:42+09:00
- 입력: `data/processed/d5/route_thermal.csv`
- CSV / DB 링크: 7,766 / 7,766
- UPDATE 행: 7,766
- DB/CSV ID 불일치: 0
- D4 재질 불일치: 0
- 기존 네트워크·D4 필드 변경 행: 0
- 온도 NULL 행: 0
- peak 계산 오류: 0
- 기상일·신뢰도 불일치: 0
- CSV/DB 온도 불일치: 0
- CSV/DB 최대 온도 오차: 0.000000°C
- 기상 기준일: 2026-08-11
- 모델 신뢰도: `LOW`
- Flyway: V7 `add route thermal fields` 성공

## DB 온도 통계

| 시각 | 최소 | 평균 | 최대 |
|---|---:|---:|---:|
| 09 | 26.34 | 30.20 | 33.97 |
| 12 | 36.23 | 47.80 | 53.51 |
| 15 | 39.01 | 48.15 | 55.55 |
| 18 | 30.44 | 32.99 | 36.58 |
| peak | 39.09 | 49.55 | 55.55 |

## 대표 링크 CSV·DB 대조

| segment_id | 09시 | 12시 | 15시 | 18시 | peak | 신뢰도 | 기상일 |
|---:|---:|---:|---:|---:|---:|---|---|
| 4179 | 29.83 | 49.44 | 55.55 | 33.85 | 55.55 | LOW | 2026-08-11 |
| 67747 | 26.42 | 36.34 | 39.09 | 30.51 | 39.09 | LOW | 2026-08-11 |
| 142707 | 30.94 | 47.59 | 46.66 | 33.30 | 47.59 | LOW | 2026-08-11 |

## 트랜잭션 정책

- 임시 staging 7,766행과 DB ID 집합이 완전히 일치할 때만 UPDATE한다.
- D4 재질이 CSV와 다르면 적재 전에 실패한다.
- 네트워크·geometry·D4 재질·SVF·물성·공원거리·그늘 필드를 스냅샷과 비교한다.
- UPDATE 수 또는 사후 검사가 하나라도 실패하면 연결 context가 전체 트랜잭션을 롤백한다.
- 커밋 후 새 연결에서 CSV 7,766행 전체를 DB와 다시 비교했다.

현재 단계에서는 샘플별 온도를 DB에 중복 저장하지 않았으며 링크 집계 온도만 적재했다.

## 실행 및 서비스 재기동 검증

- 데이터 파이프라인 전체 unit test: **55개 통과**
- backend `bootJar`: **BUILD SUCCESSFUL**
- Spring Boot Flyway validation: **7개 migration 검증 성공**
- 현재 DB schema version: **7**
- 추가 migration 필요 여부: 없음(`Schema public is up to date`)
- `/actuator/health`: **UP**
- health 확인용 임시 서버는 검증 직후 종료했다.

## 재현 명령

```powershell
Set-Location C:\project\mungroute\data-pipeline
& "C:\Program Files\QGIS 3.44.12\apps\Python312\python.exe" `
  src\load_thermal.py --apply

Set-Location C:\project\mungroute\backend
.\gradlew.bat bootJar
```

## D5-12 backend 연결

- 전용 JDBC projection으로 `route_segment`의 네 시각 온도, peak, 신뢰도, 기상일, 갱신시각을 읽는다.
- 요청 `OffsetDateTime`을 `Asia/Seoul`로 변환한 뒤 10:30·13:30·16:30을 경계로 가장 가까운 09·12·15·18시 기준값에 연결한다.
- 경계와 정확히 같은 시각에는 더 늦은 기준값을 사용한다.
- 선택된 온도나 peak·신뢰도·기상일·갱신시각이 NULL이면 0으로 대체하지 않고 `THERMAL_DATA_UNAVAILABLE`을 발생시킨다.
- 존재하지 않는 링크는 `ROUTE_SEGMENT_NOT_FOUND`로 구분한다.
- 실제 DB 통합 테스트에서 대표 링크 `4179`의 09/12/15/18시 온도 `29.83/49.44/55.55/33.85°C`와 peak `55.55°C`를 조회했다.
- 14:30 요청이 15시 기준값 `55.55°C`를 선택하는 것을 검증했다.
- repository 통합 테스트: **1개 실행, 실패·오류·skip 0개**
