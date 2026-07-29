# 배포 — 새 기계에서 처음부터

이 저장소는 두 프로세스로 나뉘어 파이에서 돈다.

```
브라우저 ──▶ clinostat (:8000, 공개)  ──▶ SpaceBio Gateway (127.0.0.1:8010, loopback 전용)
             server.py                    gateway/app.py
             spacebio_proxy.py            └─ BLE 저항센서 · 무선 펌프 · 세션 기록
             static/index.html
```

**게이트웨이는 절대 외부에 바인딩하지 않는다.** `load_config`가 `0.0.0.0`을 거부하고,
외부 접점은 기존 `:8000` 하나뿐이다. 브라우저는 `/api/spacebio/*` 프록시만 쓴다.

---

## 1. 저장소 가져오기

```bash
git clone <저장소> integration
cd integration
git checkout feature/spacebio-integrated-dashboard
```

## 2. 의존성

```bash
bash deploy/setup.sh          # venv + 잠긴 의존성
```

플랫폼별 잠금 파일이 있어야 한다. 현재 검증된 조합은 셋뿐이다.

| 플랫폼 | Python | 잠금 파일 |
|---|---|---|
| macOS arm64 (개발) | 3.14 | `constraints-macos-arm64-py314-dev.txt` |
| Raspberry Pi OS aarch64 | 3.13 | `constraints-linux-aarch64-py313.txt` |
| Raspberry Pi OS aarch64 | 3.11 | `constraints-linux-aarch64-py311.txt` |

다른 조합이면 `setup.sh`가 **거부한다**. 잠금 없이 설치하면 실기에서만 터지는
버전 차이가 생기기 때문이다. 새 조합을 쓰려면 잠금 파일을 먼저 만들어라.

기계의 기본 `python3`가 위 표와 다를 수 있다(macOS 기본이 3.13인 반면 잠금은
3.14뿐인 식). 그럴 땐 인터프리터를 지정한다.

```bash
PYTHON=python3.14 bash deploy/setup.sh
```

```bash
.venv/bin/python -m pytest -q      # 537개가 통과해야 한다
```

## 3. 파이 런타임 배치

새 파이라면 아래 두 디렉터리를 만들어야 배포 스크립트가 돈다.

```
/home/<사용자>/spacebio-gateway/       ← 게이트웨이
    gateway/            (저장소의 gateway/*.py)
    datasets/           (CSV 재생용, 선택)
    config.yaml         ← 이 기계 고유 설정. 저장소에서 복사한 뒤 손으로 고친다
    .venv/              (bash deploy/setup.sh 를 이 디렉터리에서)
    requirements.txt, constraints-*.txt

/home/<사용자>/clinostat/              ← 기존 클리노스텟 앱 (이미 있다)
    server.py, spacebio_proxy.py, static/index.html   ← 이 저장소가 덮어쓰는 3개
    control_loop.py, camera.py, ...                   ← 건드리지 않는다

/home/<사용자>/spacebio-data/sessions/ ← 측정 기록이 쌓이는 곳
```

### config.yaml — 기계마다 다른 것

저장소의 `config.yaml`은 **모의 백엔드 기준**이다. 실기에서는 최소한 아래를 고친다.

```yaml
pump:
  backend: wireless           # 실기 무선 펌프. 모의로 두려면 null
mqtt:
  host: <브로커 IP>            # 무선 펌프 보드가 붙는 브로커
  username: <계정>
  password: <암호>            # ⚠ 파이 로컬에만. 커밋 금지
ble:
  device_name: ResistanceSensor
  rref_ohm: 10000
store:
  data_root: /home/<사용자>/spacebio-data
coordinator:
  state_root: /home/<사용자>/clinostat/spacebio-state
```

`deploy/deploy_to_pi.sh`는 **config.yaml을 절대 덮어쓰지 않는다.** 설정 키가
늘어났으면 이 문서를 보고 손으로 더해라.

### systemd

```bash
sudo cp deploy/spacebio-gateway.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now spacebio-gateway
```

유닛의 `User`/경로가 `aiworker-1` 기준이다. 사용자명이 다르면 고쳐라.
`clinostat.service`는 기존 앱 것이고 **게이트웨이에 의존시키지 않는다** —
게이트웨이가 죽어도 모터 제어는 계속 돌아야 한다.

### BLE 저항센서

- BlueZ가 필요하다(실측 5.82). `bleak`는 `requirements.txt`에 있다.
- **BLE 주변장치는 중앙장치를 하나만 받는다.** 노트북의 모니터 앱이 붙어 있으면
  파이가 못 붙는다. 먼저 그쪽을 끊어라.
- 첫 연결이 느리거나 실패하면 `sudo systemctl restart bluetooth` 로 복구된다.

### 방화벽

`spacebio-guard.service`가 `/etc/nftables.d/spacebio-guard.nft`를 적용해
`:8000`을 loopback + Tailscale로만 연다. 무선 펌프 보드가 다른 대역으로
옮겨가면 **MQTT가 조용히 막힌다** — 보드 IP 대역을 허용목록에 더해야 한다.

## 4. 배포

```bash
PI_HOST=<주소> PI_USER=<사용자> bash deploy/deploy_to_pi.sh
DRY_RUN=1 ... bash deploy/deploy_to_pi.sh     # 보낼 파일만 확인
```

스크립트가 하는 일:

1. SSH 접속과 디렉터리 존재 확인
2. **측정 중이면 중단** — 게이트웨이 재시작은 BLE를 끊고 세션을 자른다 (`FORCE=1`로 무시 가능)
3. 로컬 테스트 실행 — 깨졌으면 배포하지 않는다
4. `~/spacebio-backups/<타임스탬프>/` 로 백업
5. `gateway/*.py` 와 확장 3개 파일 전송 (config.yaml 제외)
6. 두 서비스 재시작 후 4가지 검증. 실패하면 되돌리는 명령을 출력한다

인증은 SSH 키로 한다. 스크립트는 암호를 인자로 받지 않는다.

## 5. 확인

```
http://<파이>:8000/
```

- 우측 **SpaceBio Integration** 패널 → 실험명이 `YYYYMMDD-` 로 채워져 있다
- **기록 관리** 카드에 세션 목록·용량이 뜬다
- 측정 시작 → 저항 추이 그래프가 흐른다

CLI로 빠르게:

```bash
ssh <파이> 'curl -s localhost:8010/health; curl -s localhost:8000/api/spacebio/sessions | head -c 200'
```

---

## 화면 배치

한 화면(2560×1440) 기준으로 런타임에 3열로 재배치한다. `@media (min-width: 1600px)`
아래에서만 동작하고, 그보다 좁으면 원래 세로 배치로 돌아간다.

```
연결설정/운전조건 │ 카메라 │ 중력벡터분포 │ SpaceBio: 저항센서 │ 이벤트
                  │ RPM차트 │ taSMG        │ 저항 추이
                  │ IMU 9열                │ 기록 관리
                  │ I(5°) │ D[θ₁,θ₂]
```

`static/index.html`은 **append-only 계약**을 지킨다 — 캡처된 베이스라인의 어떤
줄도 고치거나 지우지 않고 `</body>` 앞에 블록만 덧붙인다.
`clinostat_extension/tests/test_existing_contract.py`가 이걸 검증하므로,
베이스라인 줄을 건드리면 테스트가 먼저 깨진다. 기본값을 바꿔야 하면
(예: 실험명 `value="resistance-run"`) 마크업 대신 덧붙인 스크립트로 덮어라.

## 기록 관리 API

| 메서드 | 브라우저 경로 | 게이트웨이 경로 |
|---|---|---|
| GET | `/api/spacebio/sessions` | `/api/sessions` |
| GET | `/api/spacebio/sessions/{id}/download` | `/api/sessions/{id}/download` |
| DELETE | `/api/spacebio/sessions/{id}` | `/api/sessions/{id}` |

- 다운로드는 `.tar.gz` 스트리밍이다. CSV가 약 5배 줄어든다(실측 4.98 MB → 970 KB).
- **기록 중인 세션은 지울 수 없다** — 409 `session_active`.
- `session_id`는 `^spacebio_\d{8}_\d{6}_[A-Za-z0-9]+$` 를 통과해야 하고, 조립한
  경로가 `sessions/` 바로 아래가 아니면 404다. 심볼릭 링크로 밖을 가리켜도 걸린다.
- 화면의 삭제 버튼은 **두 번 눌러야** 지워진다(4초 뒤 확인 상태 해제).

## 저장 용량

BLE 실측 36 Hz 기준 CSV 한 줄이 약 106 B다.

| | 크기 |
|---|---|
| 1시간 | 약 13 MB |
| 하루 | 약 319 MB |
| 20 GB 여유 | 약 63일 |

`gateway.min_free_bytes`(기본 500 MB) 아래로 떨어지면 세션 시작이 거부된다.
