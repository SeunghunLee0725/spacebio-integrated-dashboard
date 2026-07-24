#!/usr/bin/env bash
# S0 부트스트랩 — Linux 노트북 단독 환경 구축
# 사용: bash integration/deploy/setup.sh
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
echo "==> integration root: $HERE"

echo "==> apt 패키지 설치 (mosquitto, Flutter Linux 빌드 의존성)"
sudo apt-get update
sudo apt-get install -y \
  mosquitto mosquitto-clients \
  python3-venv python3-pip \
  clang cmake ninja-build pkg-config libgtk-3-dev

echo "==> 펌프 시리얼 권한: $USER 를 dialout 그룹에 추가 (재로그인 필요)"
sudo usermod -aG dialout "$USER" || true

echo "==> Python 가상환경 + 의존성"
python3 -m venv "$HERE/.venv"
# shellcheck disable=SC1091
source "$HERE/.venv/bin/activate"
pip install --upgrade pip
pip install -r "$HERE/requirements.txt"

echo "==> Mosquitto 로컬 설정 적용"
sudo cp "$HERE/deploy/mosquitto.conf" /etc/mosquitto/conf.d/spacebio.conf
sudo systemctl enable --now mosquitto

echo
echo "완료. 다음으로 S0 게이트 확인:"
echo "  source $HERE/.venv/bin/activate"
echo "  cd $HERE && python -m gateway.main --self-test"
