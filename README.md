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
├─ constraints-macos-arm64-py314-dev.txt # macOS 개발 잠금
└─ constraints-linux-aarch64-py311.txt   # Pi 배포 잠금
```

## Python 및 의존성

소스의 지원 범위는 **Python 3.10 이상, 3.15 미만**이다. 재현 가능한
설치는 현재 검증된 다음 두 환경으로 제한된다.

- macOS arm64, Python 3.14:
  `constraints-macos-arm64-py314-dev.txt`
- Raspberry Pi Linux aarch64, Python 3.11:
  `constraints-linux-aarch64-py311.txt`

`deploy/setup.sh`는 운영체제, 아키텍처, Python 마이너 버전을 검사해 정확한
파일만 선택하며, 검증된 조합이 아니면 시스템을 변경하기 전에 종료한다.
Pi에서는 Linux aarch64 잠금 파일을 사용하며 Bleak의 Linux 의존성
`dbus-fast`도 고정되어 있다.

FastAPI, Uvicorn, Pydantic, HTTPX는 다음 게이트웨이 API 단계에 필요한
런타임 기반이므로 유지한다. Bleak도 현재 `gateway.ble_source`가 제공하는
BLE 센서 경로에 필요한 런타임 의존성이다.

macOS 개발 환경 설치:

```bash
python3 -m venv --clear .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements-dev.txt \
  -c constraints-macos-arm64-py314-dev.txt
```

macOS 개발 잠금 갱신:

```bash
python3 -m venv --clear .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pip freeze --exclude pip \
  > constraints-macos-arm64-py314-dev.txt
.venv/bin/python -m pip check
.venv/bin/python -m pytest tests -q
```

Pi 잠금은 macOS의 `pip freeze`로 만들지 않는다. 검증된 target-aware
resolver인 `uv 0.11.7`로 Linux aarch64/Python 3.11을 지정해 갱신한다.

```bash
UV_CUSTOM_COMPILE_COMMAND='uv pip compile requirements.txt --python-platform aarch64-manylinux_2_17 --python-version 3.11 --only-binary :all: --output-file constraints-linux-aarch64-py311.txt' \
uv pip compile requirements.txt \
  --python-platform aarch64-manylinux_2_17 \
  --python-version 3.11 \
  --only-binary :all: \
  --output-file constraints-linux-aarch64-py311.txt
```

갱신 후 Pi 또는 동등한 Linux aarch64/Python 3.11 환경에서 다음 설치를
검증한다.

```bash
python3 -m pip install --dry-run -r requirements.txt \
  -c constraints-linux-aarch64-py311.txt
```

## 빠른 시작 (Raspberry Pi Linux aarch64/Python 3.11)
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
