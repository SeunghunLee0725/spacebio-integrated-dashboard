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
├─ requirements.txt     # 런타임 직접 의존성
├─ requirements-dev.txt # 테스트/개발 의존성
└─ constraints.txt      # 검증된 전체 의존성 버전
```

## Python 및 의존성

지원 범위는 **Python 3.10 이상, 3.15 미만**이다. 런타임 설치는 직접
의존성을 `requirements.txt`에서 읽고, 검증된 전체 버전을
`constraints.txt`로 고정한다.

```bash
python3 -m venv --clear .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt -c constraints.txt
```

테스트 환경은 다음과 같이 설치한다.

```bash
.venv/bin/python -m pip install -r requirements-dev.txt -c constraints.txt
```

직접 의존성 범위를 변경한 뒤 잠금 파일을 갱신하려면 깨끗한 가상환경에서
개발 의존성을 설치하고 `pip freeze` 결과를 다시 생성한 후 전체 테스트를
실행한다. 제약 파일에 포함된 플랫폼 전용 패키지는 해당 플랫폼에서
의존성으로 선택될 때만 적용되며, 제약 파일 자체가 패키지를 설치하지 않는다.

```bash
python3 -m venv --clear .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pip freeze --exclude pip > constraints.txt
.venv/bin/python -m pip check
.venv/bin/python -m pytest tests -q
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
