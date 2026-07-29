#!/usr/bin/env bash
# 게이트웨이 + 클리노스텟 확장을 파이로 배포한다.
#
#   PI_HOST=100.77.1.42 PI_USER=aiworker-1 bash deploy/deploy_to_pi.sh
#   DRY_RUN=1 bash deploy/deploy_to_pi.sh      # 무엇이 바뀌는지만 본다
#
# 인증은 SSH가 알아서 한다(키 또는 ssh-agent). **암호를 인자로 받지 않는다.**
#
# ⚠ config.yaml 은 절대 덮어쓰지 않는다. 파이의 config.yaml 에는 MQTT 자격증명과
#   pump.backend=wireless 같은 그 기계 고유 설정이 들어 있고, 저장소 것은 모의
#   백엔드 기준이다. 설정 키가 늘었으면 DEPLOY.md 를 보고 손으로 더해라.
#
# ⚠ 측정 중이면 배포하지 않는다. 게이트웨이를 재시작하면 BLE 연결이 끊기고
#   기록 중인 세션이 잘린다. --force 로 넘길 수 있지만 그럴 이유는 거의 없다.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PI_HOST="${PI_HOST:-100.77.1.42}"
PI_USER="${PI_USER:-aiworker-1}"
TARGET="$PI_USER@$PI_HOST"
DRY_RUN="${DRY_RUN:-0}"
FORCE="${FORCE:-0}"

GATEWAY_ROOT="/home/$PI_USER/spacebio-gateway"
CLINOSTAT_ROOT="/home/$PI_USER/clinostat"
STAMP="$(date +%Y%m%d_%H%M%S)"
BACKUP_ROOT="/home/$PI_USER/spacebio-backups/$STAMP"

#: 게이트웨이 패키지. config.yaml 은 의도적으로 빠져 있다.
GATEWAY_FILES=(gateway/*.py)
#: 클리노스텟 프로세스 안에서 도는 확장. work/ 가 편집 가능한 원본이다.
declare -a EXTENSION_PAIRS=(
  "clinostat_extension/work/server.py:$CLINOSTAT_ROOT/server.py"
  "clinostat_extension/work/spacebio_proxy.py:$CLINOSTAT_ROOT/spacebio_proxy.py"
  "clinostat_extension/work/static/index.html:$CLINOSTAT_ROOT/static/index.html"
)

say() { printf '==> %s\n' "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
run_remote() { ssh -o BatchMode=yes "$TARGET" "$@"; }

# ─────────────────────────── 사전 점검 ───────────────────────────

say "대상: $TARGET"
run_remote true || die "SSH 접속 실패. 키가 등록돼 있는지 확인해라 (ssh $TARGET)"

for path in "$GATEWAY_ROOT/gateway" "$CLINOSTAT_ROOT/static"; do
  run_remote "test -d '$path'" \
    || die "$path 가 없다. 새 파이라면 DEPLOY.md 의 '처음 설치'를 먼저 해라"
done

say "측정 상태 확인"
SENSOR_STATE="$(run_remote "curl -sf --max-time 5 http://127.0.0.1:8010/api/status \
  | python3 -c 'import sys,json; print(json.load(sys.stdin)[\"data\"][\"sensor\"][\"state\"])' \
  2>/dev/null || echo unreachable")"
say "센서 상태: $SENSOR_STATE"
if [ "$SENSOR_STATE" != "stopped" ] && [ "$SENSOR_STATE" != "unreachable" ] && [ "$FORCE" != "1" ]; then
  die "측정 중($SENSOR_STATE)이다. 측정을 정지하고 다시 실행해라 (정말 필요하면 FORCE=1)"
fi

say "로컬 테스트"
if [ -x "$HERE/.venv/bin/python" ]; then
  (cd "$HERE" && .venv/bin/python -m pytest -q) || die "테스트가 깨진 채로 배포하지 않는다"
else
  echo "    (.venv 가 없어 건너뛴다 — bash deploy/setup.sh 로 만들 수 있다)"
fi

if [ "$DRY_RUN" = "1" ]; then
  say "DRY_RUN — 아래 파일을 보낼 예정이다"
  printf '    %s\n' "${GATEWAY_FILES[@]}"
  for pair in "${EXTENSION_PAIRS[@]}"; do printf '    %s\n' "${pair%%:*}"; done
  exit 0
fi

# ─────────────────────────── 백업 ───────────────────────────

say "백업: $BACKUP_ROOT"
run_remote "mkdir -p '$BACKUP_ROOT/gateway' '$BACKUP_ROOT/clinostat/static'"
run_remote "cp -a '$GATEWAY_ROOT/gateway/.' '$BACKUP_ROOT/gateway/'"
for pair in "${EXTENSION_PAIRS[@]}"; do
  remote="${pair#*:}"
  run_remote "test -f '$remote' && cp -a '$remote' '$BACKUP_ROOT/clinostat/${remote#$CLINOSTAT_ROOT/}' || true"
done

# ─────────────────────────── 전송 ───────────────────────────

say "게이트웨이 전송"
(cd "$HERE" && scp -q "${GATEWAY_FILES[@]}" "$TARGET:$GATEWAY_ROOT/gateway/")

say "클리노스텟 확장 전송"
for pair in "${EXTENSION_PAIRS[@]}"; do
  scp -q "$HERE/${pair%%:*}" "$TARGET:${pair#*:}"
done

# ─────────────────────────── 재시작 · 검증 ───────────────────────────

say "서비스 재시작"
run_remote "sudo systemctl restart spacebio-gateway.service clinostat.service"
sleep 6

say "검증"
FAILED=0
check() {
  local label="$1" cmd="$2"
  if run_remote "$cmd" >/dev/null 2>&1; then
    printf '    ok   %s\n' "$label"
  else
    printf '    FAIL %s\n' "$label"; FAILED=1
  fi
}
check "gateway /health"        "curl -sf --max-time 5 http://127.0.0.1:8010/health"
check "gateway /api/sessions"  "curl -sf --max-time 5 http://127.0.0.1:8010/api/sessions"
check "패널 :8000"             "curl -sf --max-time 5 http://127.0.0.1:8000/ -o /dev/null"
check "프록시 기록 목록"        "curl -sf --max-time 5 http://127.0.0.1:8000/api/spacebio/sessions"

if [ "$FAILED" != "0" ]; then
  cat >&2 <<EOF

배포 후 검증에 실패했다. 되돌리려면:

  ssh $TARGET "cp -a $BACKUP_ROOT/gateway/. $GATEWAY_ROOT/gateway/ \\
    && cp -a $BACKUP_ROOT/clinostat/server.py $BACKUP_ROOT/clinostat/spacebio_proxy.py $CLINOSTAT_ROOT/ \\
    && cp -a $BACKUP_ROOT/clinostat/static/index.html $CLINOSTAT_ROOT/static/ \\
    && sudo systemctl restart spacebio-gateway.service clinostat.service"

로그: ssh $TARGET 'sudo journalctl -u spacebio-gateway -u clinostat -n 60 --no-pager'
EOF
  exit 1
fi

say "완료. 백업은 $BACKUP_ROOT 에 있다"
say "화면: http://$PI_HOST:8000/"
