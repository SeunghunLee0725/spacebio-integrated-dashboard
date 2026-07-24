# integration — 통합 게이트웨이 (노트북 단독)

클리노스텟 + LOC(펌프·센싱) + 근세포 수축력을 **하나의 앱에서 관제**하기 위한 엣지 게이트웨이.
설계: `../우주바이오_Vault/10_계획/통합 아키텍처 (노트북 단독).md`

## 구조
```
integration/
├─ gateway/        # Python 두뇌
│  ├─ main.py          # 오케스트레이터 (--self-test 로 S0 게이트 확인)
│  ├─ mqtt_bridge.py   # paho-mqtt 래퍼 (로컬 Mosquitto)
│  ├─ contracts.py     # 토픽 상수 + 페이로드 스키마 (contracts/topics.md 미러)
│  ├─ ble_source.py    # 센서 소스 (SimulatedSource / bleak 예정)
│  ├─ pump_actuator.py # 펌프+밸브 '액추에이터 시퀀스' 추상화 (유로 방식 미정 수용)
│  ├─ control_loop.py  # 폐루프 규칙 엔진 + 인터록 (로컬 실행)
│  └─ store.py         # SQLite 로거
├─ contracts/topics.md # 토픽·페이로드 계약 (단일 진실 소스)
├─ deploy/             # mosquitto.conf, setup.sh, systemd 유닛
├─ tests/              # 브로커 비의존 단위 테스트
├─ config.yaml
└─ requirements.txt
```

## 빠른 시작 (Linux)
```bash
bash deploy/setup.sh                      # mosquitto + venv + 의존성
source .venv/bin/activate
python -m gateway.main --self-test        # S0 게이트: hello 왕복
python -m gateway.main                    # 게이트웨이 구동 (시뮬 센서)
```

확인용 구독(다른 터미널):
```bash
mosquitto_sub -t 's25007/#' -v
```

## 현재 상태 (S0)
- 시뮬레이션 센서 소스로 전체 파이프라인(브리지·저장·폐루프) 구동 가능.
- 실제 하드웨어(BLE 센서·펌프)는 config에서 `bleak`/`serial` 백엔드로 교체 예정(S1·S4).
- 유로 방식(밸브/볼루스/수동)은 미정 → `pump_actuator`가 소스-불문 API로 흡수. (DEC-009)

## 테스트
```bash
cd integration && python -m pytest tests/ -q
```
