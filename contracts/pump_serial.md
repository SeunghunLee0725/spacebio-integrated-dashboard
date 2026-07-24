# 펌프 시리얼 프로토콜 (pump_serial v0)

> 게이트웨이 `SerialPumpBackend` ↔ 펌프 드라이버 보드(`pump_driver.ino`) 간 계약.
> **주의:** 지난해 데모 펌프의 프로토콜 코드가 리포에 없어 **새로 정의**했다. 실물 펌프/보드가 정해지면 재검토. → [[DEC-011 펌프 시리얼 프로토콜]]

## 물리 계층
- USB 시리얼, **115200 baud**, 8N1.
- ASCII, 한 줄 = 한 명령, `\n` 종단. 응답도 한 줄.

## 명령 (호스트 → 보드)
| 명령 | 인자 | 동작 | 응답 |
|---|---|---|---|
| `PING` | — | 헬스체크 | `PONG` |
| `RUN` | `<rate_ul_s>` | 연속 구동 시작 | `OK RUN <rate>` |
| `STOP` | — | 정지 | `OK STOP` |
| `DISP` | `<vol_ul> <rate_ul_s>` | 정량 주입(보드가 시간 계산 후 자동정지) | `OK DISP <vol> <rate>` |
| `SEL` | `<media\|drug\|none>` | 소스 밸브 선택(밸브 없으면 무시) | `OK SEL <src>` |
| `STAT` | — | 상태 조회 | `STATE <idle\|running> <dispensed_ul>` |

## 비동기 통지 (보드 → 호스트)
- `DONE DISP <vol>` — 정량 주입 완료(자동정지 시).
- `WDOG STOP` — 워치독이 연속 RUN을 자동정지(호스트 무응답 > 2000 ms).
- `ERR <msg>` — 잘못된 명령/한계 초과.

## 안전 (보드가 강제 — DEC-008 이중 방어)
- `MAX_RATE_UL_S`, `MAX_VOLUME_UL` 하드리밋(펌웨어 상수). 호스트 클램프와 이중.
- **워치독**: 연속 RUN 중 명령이 `WATCHDOG_MS`(기본 2 s) 이상 없으면 자동정지 → 호스트 크래시 시 폭주 방지.
- 정량 주입은 보드가 시간 계산·자동정지 → 호스트가 죽어도 과주입 없음.

## 유량 보정
- `UL_PER_S_AT_FULL` (PWM 255에서 uL/s)를 **펌프별 실측 보정** 후 펌웨어에 반영. rate → PWM 선형 매핑.

## 예시 세션
```
호스트> PING
보드 < PONG
호스트> SEL media
보드 < OK SEL media
호스트> DISP 200 50
보드 < OK DISP 200.00 50.00
   ...(4초 후)...
보드 < DONE DISP 200.00
```
