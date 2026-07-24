# 토픽·페이로드 계약 (contracts v0)

> 앱 ↔ 게이트웨이 ↔ 디바이스가 공유하는 **단일 진실 소스**. 코드(`gateway/contracts.py`)와 항상 일치시킨다.
> 버스: 로컬 Mosquitto `localhost:1883` (→ [[DEC-007 통합 버스 로컬 MQTT]]). 페이로드: UTF-8 JSON.

## 네임스페이스
루트 프리픽스 `s25007/`.

| 토픽 | 방향 | QoS | Retain | 용도 |
|---|---|---|---|---|
| `s25007/gateway/status` | GW→앱 | 1 | ✓ | 게이트웨이 하트비트/상태 |
| `s25007/clinostat/status` | 디바이스→앱 | 1 | ✓ | 클리노스텟 회전 상태 |
| `s25007/clinostat/cmd` | 앱→디바이스 | 1 | ✗ | 회전 명령 |
| `s25007/loc/sensor` | GW→앱 | 0 | ✗ | 센서 스트림(인장/근수축·온도), 고빈도 |
| `s25007/pump/status` | GW→앱 | 1 | ✓ | 펌프·밸브 상태 |
| `s25007/pump/cmd` | 앱→GW | 1 | ✗ | 펌프 액추에이터 명령 |
| `s25007/loop/state` | GW→앱 | 1 | ✓ | 폐루프 상태(무장/해제·최근 이벤트) |
| `s25007/loop/cmd` | 앱→GW | 1 | ✗ | 폐루프 무장/해제·임계 설정 |

> 기존 앱 토픽 `s25007/board1/{status,cmd}` = 클리노스텟과 동일 → `clinostat`로 매핑(마이그레이션 시 병행 구독).

## 페이로드 스키마 (JSON)

### 공통 필드
모든 메시지: `{"ts": <epoch_ms:int>, "src": <str>, ...}`

### `gateway/status`
```json
{"ts": 1690000000000, "src": "gateway", "state": "online", "ble": "disconnected", "pump": "idle", "loop": "disarmed"}
```
`state`: online|degraded|offline · `ble`: connected|disconnected|scanning

### `loc/sensor`
```json
{"ts": 1690000000000, "src": "loc", "tension_n": 3.24, "delta_r_over_r0": 0.031, "temp_c": 36.8, "battery_pct": 87}
```

### `pump/cmd`  (액추에이터 시퀀스 — 유로 방식 미정 수용, → [[DEC-009 약물 주입 유로 방식]])
```json
{"ts": 1690000000000, "src": "app", "action": "dispense", "source": "media", "volume_ul": 200, "rate_ul_s": 50}
```
`action`: on|off|dispense|stop · `source`: media|drug|null (밸브 없으면 null) · 정량 주입은 `dispense`+`volume_ul`.

### `pump/status`
```json
{"ts": 1690000000000, "src": "pump", "state": "running", "action": "dispense", "source": "media", "dispensed_ul": 120, "rate_ul_s": 50}
```
`state`: idle|running|fault

### `loop/cmd`
```json
{"ts": 1690000000000, "src": "app", "action": "arm", "trigger": "tension_n", "threshold": 5.0, "on_trigger": {"source": "drug", "volume_ul": 50}}
```
`action`: arm|disarm|estop

### `loop/state`
```json
{"ts": 1690000000000, "src": "loop", "armed": true, "trigger": "tension_n", "threshold": 5.0, "last_fired_ts": null, "interlock": "ok"}
```
`interlock`: ok|max_volume|max_duration|watchdog

## 안전 규칙 (계약 수준)
- `loop/cmd action=estop` 는 **즉시 최우선** 처리, 진행 중 `dispense` 중단.
- 게이트웨이는 `pump/cmd`의 `volume_ul`·`rate_ul_s`를 하드리밋으로 클램프(→ [[DEC-008 폐루프 실행 위치]]). 펌웨어 하드리밋과 이중.
