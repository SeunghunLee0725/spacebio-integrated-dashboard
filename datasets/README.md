# Datasets

CSV 재생 소스는 `manifest.json`에 등록된 `dataset_id`만 로드한다 (허용목록).
`gateway/csv_replay.py`의 `resolve_dataset_path`는 각 로드에서 다음을 검증한다:

1. `dataset_id`가 manifest에 등록되어 있는가
2. 파일 경로가 `datasets_dir` 하위로 resolve되는가 (path traversal 방어)
3. 파일의 실제 SHA-256이 manifest에 기록된 값과 일치하는가

## 등록된 dataset

| dataset_id | filename | sample_count | provenance |
|---|---|---|---|
| `thinkpad_20260714_172138_ble_test` | `thinkpad_20260714_172138_ble_test.csv` | 522 | ThinkPad 게이트웨이 실측 BLE 저항 센서 측정 (2026-07-14 17:21:38 세션) |

CSV 필수 header: `timestamp_ms,raw_adc,resistance_ohm,delta_r_over_r0,temperature_c,battery_pct`.
`elapsed_s`는 ThinkPad 변형에서 허용되는 optional 열이며 재생 시 사용되지 않는다
(session_elapsed_ms는 `timestamp_ms`를 첫 샘플 기준으로 rebase해 재계산한다).

## dataset 추가 절차

1. CSV를 `datasets/`에 배치한다.
2. `shasum -a 256 <file>`로 SHA-256을 계산한다.
3. `manifest.json`에 `dataset_id`, `filename`, `sha256`, `provenance`, `sample_count`를 추가한다.
4. `dataset_id`는 `^[A-Za-z0-9_-]+$` 패턴을 따라야 한다 (경로 구분자·점·공백 금지).
