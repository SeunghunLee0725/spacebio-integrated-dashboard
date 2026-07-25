"""대시보드 DOM 회귀 + SpaceBio 패널 계약 (설계 스펙 4장, 계획서 Task 8).

가장 중요한 것은 **기존 클리노스텟 화면이 하나도 사라지지 않는 것**이다.
baseline HTML의 DOM ID 49개가 work HTML에 각각 정확히 1회씩 남아야 한다.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / "baseline" / "real-20260724"
WORK_HTML = ROOT / "work" / "static" / "index.html"

_ID_RE = re.compile(r'\bid="([^"]+)"')


def _ids(html: str) -> Counter:
    return Counter(_ID_RE.findall(html))


@pytest.fixture(scope="module")
def work() -> str:
    return WORK_HTML.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def baseline_ids() -> dict:
    return json.loads((BASELINE / "dom_ids.json").read_text(encoding="utf-8"))["ids"]


# ─────────────────────────── 기존 DOM 보존 (핵심) ───────────────────────────

def test_baseline_has_the_expected_id_count(baseline_ids):
    assert len(baseline_ids) == 49


def test_every_baseline_id_survives_exactly_once(work, baseline_ids):
    """패널을 덧붙이면서 기존 요소를 지우거나 중복시키지 않았는지 확인한다."""
    actual = _ids(work)
    missing = [i for i in baseline_ids if actual[i] == 0]
    duplicated = [i for i in baseline_ids if actual[i] > baseline_ids[i]]
    assert not missing, f"사라진 기존 DOM id: {missing}"
    assert not duplicated, f"중복된 기존 DOM id: {duplicated}"


@pytest.mark.parametrize("fragment", [
    'id="btnStopAll"',
    "async function stopAllMotion()",
    "chart.js@4.4.4",
    "new WebSocket(",
])
def test_existing_controls_and_scripts_are_intact(work, fragment):
    assert fragment in work


def test_existing_websocket_route_is_untouched(work):
    """기존 /ws 연결 코드를 바꾸면 안 된다 — SpaceBio는 별도 소켓을 쓴다."""
    assert "/ws`" in work or "/ws'" in work or '/ws"' in work


def test_chartjs_is_reused_not_reloaded(work):
    """이미 로드된 Chart.js를 재사용한다 — script 태그를 새로 넣지 않는다."""
    assert work.count("chart.js@") == 1
    assert "chart.umd.min.js" in work


# ─────────────────────────── 신규 패널 ID ───────────────────────────

NEW_IDS = [
    "spacebioGatewayState", "spacebioGatewayLastSeen", "spacebioGatewayError",
    "spacebioSessionId", "spacebioExperimentName", "spacebioSessionStart",
    "spacebioSessionFinish", "spacebioSessionStartedAt", "spacebioSessionElapsed",
    "spacebioSessionRecordingState", "spacebioEvents",
    "resistanceMode", "resistanceDataset", "resistanceBaselineOhm",
    "resistanceAmplitudeOhm", "resistancePeriodS", "resistanceNoiseStdOhm",
    "resistanceSeed", "resistanceStart", "resistanceStop",
    "resistanceValue", "resistanceDelta", "resistanceAdc",
    "resistanceTemperature", "resistanceBattery", "resistanceChart",
    "pumpSimulatedBadge", "pumpState", "pumpEstopLatched",
    "pumpRate", "pumpTargetVolume", "pumpDeliveredVolume",
    "pumpCumulativeVolume", "pumpDispense", "pumpStop",
    "pumpEmergencyStop", "pumpResetAcknowledgement", "pumpResetEmergencyStop",
    "pumpStepCount", "pumpStepSpm", "pumpSendSteps", "pumpPositionSteps",
    "spacebioJumpLink",
]


@pytest.mark.parametrize("element_id", NEW_IDS)
def test_new_panel_id_exists_exactly_once(work, element_id):
    assert _ids(work)[element_id] == 1, f"{element_id}가 없거나 중복이다"


def test_new_ids_do_not_collide_with_baseline(baseline_ids):
    assert not (set(NEW_IDS) & set(baseline_ids)), "신규 ID가 기존 ID와 충돌한다"


def test_panel_is_appended_after_all_existing_content(work, baseline_ids):
    """기존 섹션 사이에 끼워 넣지 않고 전부 뒤에 붙였는지 확인한다."""
    panel_at = work.index('id="spacebioPanel"')
    last_baseline_at = max(work.index(f'id="{i}"') for i in baseline_ids)
    assert panel_at > last_baseline_at


# ─────────────────────────── 호출 경로 ───────────────────────────

def test_panel_only_calls_proxied_routes(work):
    """브라우저는 Gateway(:8010)에 직접 접속하지 않는다."""
    assert "8010" not in work
    assert "127.0.0.1" not in work
    for path in ("/api/spacebio/sensor/configure", "/api/spacebio/sensor/start",
                 "/api/spacebio/sensor/stop", "/api/spacebio/pump/dispense",
                 "/api/spacebio/pump/stop", "/api/spacebio/pump/emergency-stop",
                 "/api/spacebio/pump/reset-emergency-stop",
                 "/api/spacebio/session/start", "/api/spacebio/session/finish",
                 "/api/spacebio/status", "/ws/spacebio"):
        assert path in work, f"{path} 호출이 없다"


# ─────────────────────────── 모드 배타 ───────────────────────────

def test_mode_switch_disables_the_other_modes_inputs(work):
    """CSV 모드는 합성 입력을, 합성 모드는 dataset 선택을 비활성화한다."""
    assert "spacebioApplySensorMode" in work
    assert "CSV_REPLAY" in work and "SYNTHETIC" in work
    section = work[work.index("function spacebioApplySensorMode"):][:1200]
    assert "resistanceDataset" in section
    assert "resistanceBaselineOhm" in section
    assert "disabled" in section


# ─────────────────────────── 비상정지 해제 ───────────────────────────

def test_reset_button_is_gated_on_exact_acknowledgement(work):
    assert "RESET_SIMULATED_PUMP_ESTOP" in work
    section = work[work.index("function spacebioSyncResetButton"):][:800]
    assert "pumpResetAcknowledgement" in section
    assert "===" in section, "정확 일치 비교여야 한다"
    assert "pumpResetEmergencyStop" in section


def test_reset_sends_the_operator_typed_value_not_a_hidden_constant(work):
    section = work[work.index("async function spacebioResetEstop"):][:900]
    assert "pumpResetAcknowledgement" in section and ".value" in section
    assert '"RESET_SIMULATED_PUMP_ESTOP"' not in section, \
        "하드코딩된 숨은 값을 보내면 운영자 확인 절차가 무의미해진다"


def test_reset_clears_the_input_after_success(work):
    section = work[work.index("async function spacebioResetEstop"):][:1200]
    assert re.search(r"pumpResetAcknowledgement\w*\.value\s*=\s*['\"]{2}", section) \
        or ".value = ''" in section


# ─────────────────────────── 차트 / 스트림 ───────────────────────────

def test_chart_is_bounded_and_rate_limited(work):
    assert "1800" in work, "차트 포인트 상한 1800이 없다"
    assert "spacebioChart" in work
    assert "200" in work, "5 Hz(200 ms) 갱신 간격이 없다"


def test_stale_detection_and_backoff_schedule(work):
    assert "3000" in work, "3초 stale 판정이 없다"
    section = work[work.index("SPACEBIO_BACKOFF"):][:200]
    for step in ("1000", "2000", "4000", "8000", "10000"):
        assert step in section, f"backoff {step} ms 누락"


def test_sequence_gap_triggers_full_status_refetch(work):
    """sequence가 건너뛰면 중간 상태를 놓친 것이므로 전체 상태를 다시 받아야 한다."""
    assert "spacebioLastSequence" in work
    gap_at = work.index("message.sequence >")
    section = work[gap_at:gap_at + 400]
    assert "spacebioLastSequence" in section
    assert "/api/spacebio/status" in section


def test_only_spacebio_controls_are_disabled_when_stale(work):
    """Gateway가 죽어도 기존 클리노스텟 컨트롤은 계속 쓸 수 있어야 한다."""
    section = work[work.index("function spacebioSetStale"):][:900]
    assert "spacebio" in section
    assert "btnStopAll" not in section, "기존 STOP ALL을 비활성화하면 안 된다"


# ─────────────────────────── STOP ALL 가산 확장 ───────────────────────────

def test_stop_all_keeps_the_existing_clinostat_stop_first(work):
    section = work[work.index("async function stopAllMotion()"):][:1400]
    existing_at = section.index("/api/control/stop")
    pump_at = section.index("/api/spacebio/pump/stop")
    assert existing_at < pump_at, "기존 클리노스텟 정지를 먼저 호출해야 한다"


def test_stop_all_reports_both_results_independently(work):
    section = work[work.index("async function stopAllMotion()"):][:1600]
    assert "spacebioEvents" in section or "spacebioLog" in section


# ─────────────────────────── 데이터셋 드롭다운 (실기 결함 수정) ───────────────────────────

def test_dataset_dropdown_is_populated_at_init(work):
    """resistanceDataset이 빈 <select>로 남지 않도록 초기화 시점에 채워야 한다."""
    assert "async function spacebioLoadDatasets" in work
    assert "/api/spacebio/sensor/datasets" in work
    init_section = work[work.index("async function spacebioInit"):][:2000]
    assert "spacebioLoadDatasets()" in init_section


def test_dataset_dropdown_load_failure_does_not_crash_the_panel(work):
    section = work[work.index("async function spacebioLoadDatasets"):][:700]
    assert "try" in section and "catch" in section


def test_dataset_dropdown_uses_sample_count_in_label(work):
    section = work[work.index("async function spacebioLoadDatasets"):][:700]
    assert "dataset_id" in section
    assert "sample_count" in section


# ─────────────────────────── 실기 센서(SERIAL_LIVE) 모드 ───────────────────────────

def test_serial_live_mode_option_exists(work):
    assert 'value="SERIAL_LIVE"' in work


def test_serial_live_mode_disables_dataset_and_synthetic_inputs(work):
    section = work[work.index("function spacebioApplySensorMode"):][:900]
    assert "SERIAL_LIVE" in section
    assert "resistanceDataset" in section
    assert "resistanceBaselineOhm" in section


def test_serial_live_configure_sends_mode_only(work):
    section = work[work.index("async function spacebioConfigureSensor"):][:900]
    assert "SYNTHETIC" in section, "합성 모드 분기가 명시적이어야 SERIAL_LIVE가 새지 않는다"


# ─────────────────────────── 펌프 스텝 컨트롤 ───────────────────────────

def test_pump_step_controls_send_steps_and_spm(work):
    assert "/api/spacebio/pump/step" in work
    assert "pumpStepCount" in work and "pumpStepSpm" in work
    section = work[work.index("sbEl('pumpSendSteps')"):][:400]
    assert "steps" in section and "spm" in section


def test_pump_micro_controls_preserved(work):
    """기존 µL 컨트롤이 스텝 컨트롤 추가로 지워지지 않았는지 확인한다."""
    assert "pumpRate" in work and "pumpTargetVolume" in work and "pumpDispense" in work


# ─────────────────────────── 상단 이동 링크 ───────────────────────────

def test_jump_link_points_to_the_panel(work):
    header_section = work[work.index("<header>"):work.index("</header>")]
    assert 'href="#spacebioPanel"' in header_section
    assert 'id="spacebioJumpLink"' in header_section
