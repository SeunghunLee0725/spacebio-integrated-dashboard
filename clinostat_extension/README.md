# Clinostat extension

`deploy/capture_pi_baseline.sh` makes a read-only snapshot of the existing Pi
application. Raw evidence is written below the ignored `deploy/baseline/raw/`
directory, scanned for secrets, and never committed. Only the explicit,
sanitized contract allowlist is copied to `baseline/sanitized/<timestamp>/`.

Run it with `PI_HOST` and `PI_USER` set. SSH host verification is mandatory and
authentication remains interactive; credentials must never be supplied as
arguments or stored by this repository.

Live capture requires local `websocat` and at least one of `gitleaks` or
`trufflehog`. The WebSocket route is discovered from the captured Python/HTML
sources and observed through a local SSH port forward. A portable Python
supervisor measures the monotonic observation duration, rejects early client
exit, and requires both 55 seconds of observation and three messages. Production
Python sources are obtained from a bounded recursive manifest; backups, run
data, caches, Git metadata, and tests are excluded. Required control symbols are
resolved uniquely by AST and every constant/calculation records source path,
line, and SHA-256. Status is read three
times from `/api/control/status`; neither observation starts or changes control
hardware. Publication is fail-closed: raw evidence is scanned first, sanitized
output is built in a temporary sibling, rescanned, hashed, and atomically moved.

The `work/` directory contains exact editable copies of the captured
`server.py` and `static/index.html`. Baseline files are evidence and should not
be edited.

## 확장 지점 지도 (2026-07-24 실측 분석)

Task 7–8이 붙을 자리를 미리 찾아둔 것이다. 줄 번호는 캡처 시점 기준이라
편집하면 밀린다 — 앵커 문자열로 찾아라.

### `work/server.py` (970줄)

| 앵커 | 무엇 | 주의 |
|---|---|---|
| `async def lifespan(app: FastAPI):` | **기존 lifespan.** 현재 `yield` 앞에 setup이 전혀 없고 뒤에만 정리 코드가 있다 | 프록시용 `httpx.AsyncClient`를 `yield` 앞에서 만들고 뒤에서 닫아라. **기존 정리 코드(`control.stop()`, `*.disconnect()`, `camera.stop()`)를 건드리지 마라** |
| `app = FastAPI(title="Clinostat Controller", lifespan=lifespan)` | app 생성 | 새 `/api/spacebio/*` 라우트는 이 줄 이후 아무 곳에나 추가 가능 |
| `@app.post("/api/control/stop")` | 기존 정지 | `STOP ALL`이 호출한다. **먼저 호출**하고 그 결과와 무관하게 펌프 정지를 별도 bounded task로 |
| `@app.post("/api/run/save")` | run 생성 경계 | 세션 코디네이터가 `clinostat_run_id`를 잡을 지점. **기존 run 형식을 바꾸지 마라** |
| `@app.websocket("/ws")` | 기존 WS | 절대 수정 금지. `/ws/spacebio`를 따로 만든다 |
| `uvicorn.run(app, host="0.0.0.0", port=8000)` | 마지막 줄 | 그대로 둔다. 공개 노출은 파이의 nftables `spacebio_guard`가 막고 있다(README 상단 참고) |

`server.py`에 명시적 `Lock()`이 없다 — 제어 루프 동기화는 `control_loop.py`
쪽에 있다. FastAPI 핸들러 레이어에 라우트를 추가하는 한 "제어 루프 lock 안에서
HTTP I/O 금지" 제약은 자연히 지켜진다. 다만 `control` 객체의 동기 메서드를
async 핸들러에서 직접 호출하지는 마라.

### `work/static/index.html` (1705줄, DOM id 49개)

| 앵커 | 무엇 | 주의 |
|---|---|---|
| `<script src="...chart.js@4.4.4...">` (7행) | **Chart.js가 이미 로드돼 있다** | 새로 불러오지 말고 재사용해라. 단 CDN이라 **운영자 브라우저에 인터넷이 필요**하다 |
| `id="btnStopAll" onclick="stopAllMotion()"` | STOP ALL 버튼 | 버튼과 핸들러명을 바꾸지 마라 |
| `async function stopAllMotion()` | 정지 핸들러 | **가산적으로** 확장. 기존 클리노스텟 정지 호출을 먼저 하고, 두 결과를 독립적으로 표시 |
| `ws = new WebSocket(...(location.host)/ws)` | 기존 WS 연결 | 수정 금지. `/ws/spacebio`용 연결을 따로 만든다 |
| `</body>` (마지막) | 패널 삽입 위치 | **기존 콘텐츠 전부 뒤에** `SpaceBio Integration` 섹션 하나만 추가 |

새 ID·클래스·함수는 전부 `spacebio` 접두사를 붙여 충돌을 피한다.
기존 DOM id 49개가 각각 정확히 1회씩 남아 있어야 하며
`tests/test_existing_contract.py`가 이를 검증한다.
