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
    # 세션 시작/종료 버튼은 없앴다 — 측정 시작/정지가 세션을 알아서 연다.
    "spacebioSessionId", "spacebioExperimentName",
    "spacebioSessionStartedAt", "spacebioSessionElapsed",
    "spacebioSessionRecordingState", "spacebioEvents",
    "resistanceStart", "resistanceStop",
    "resistanceValue", "resistanceDelta", "resistanceAdc",
    "resistanceTemperature", "resistanceBattery", "resistanceChart",
    "pumpSimulatedBadge", "pumpState", "pumpEstopLatched",
    "pumpRate", "pumpTargetVolume", "pumpDeliveredVolume",
    "pumpCumulativeVolume", "pumpDispense", "pumpStop",
    "pumpEmergencyStop", "pumpResetAcknowledgement", "pumpResetEmergencyStop",
    "pumpStepCount", "pumpStepSpm", "pumpSendSteps", "pumpPositionSteps",
    "spacebioJumpLink", "resistanceModeBadge", "spacebioSubtitle",
    "spacebioRecordingHint", "spacebioDataPath", "spacebioStorageHint",
    "resistanceRref", "resistanceAvgFactor", "resistanceSettingHint",
]


def test_badges_reflect_actual_mode_not_hardcoded_simulated(work):
    """배지는 status의 실제 mode로 채워야 한다 — SIMULATED 하드코딩 금지."""
    # 배지 요소에 정적 'SIMULATED' 텍스트가 남아 있으면 안 된다
    assert 'id="pumpSimulatedBadge">SIMULATED<' not in work
    assert 'id="resistanceModeBadge">-<' in work or 'id="resistanceModeBadge">' in work
    # 렌더가 mode로 배지를 채운다
    render = work[work.index("function spacebioRenderStatus"):]
    render = render[:render.index("\nfunction ", 1)]
    assert "pumpSimulatedBadge" in render and "WIRELESS" in render
    assert "resistanceModeBadge" in render and "BLE_LIVE" in render


def test_null_raw_adc_shows_dash_not_null(work):
    """실기 센서의 null raw_adc를 'null'이나 0으로 보여주면 안 된다."""
    render = work[work.index("resistanceAdc"):]
    assert "'—'" in render or '"—"' in render


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


# ─────────────────────────── 비상정지 해제 ───────────────────────────

def test_reset_button_is_gated_on_exact_acknowledgement(work):
    assert "RESET_SIMULATED_PUMP_ESTOP" in work
    # 함수 본문만 잘라서 본다. 이전엔 뒤 800자를 훑어 "===" 유무만 봤는데,
    # 실제 비교는 `!==` 라 인접 함수의 "===" 때문에 우연히 통과하던 테스트였다.
    start = work.index("function spacebioSyncResetButton")
    section = work[start:work.index("\n}", start)]
    assert "pumpResetAcknowledgement" in section
    assert "SPACEBIO_ESTOP_ACK" in section
    assert "!==" in section or "===" in section, "정확 일치 비교여야 한다"
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

# ─────────────────────── 실기 BLE 전용 패널 ───────────────────────
#
# 센서 측정 메뉴는 실기 BLE 센서만 다룬다. CSV 재생·합성 신호·USB 시리얼
# 모드와 그 입력들은 화면에서 제거했다(백엔드에는 남아 있다).

@pytest.mark.parametrize("removed", [
    'value="CSV_REPLAY"', 'value="SYNTHETIC"', 'value="SERIAL_LIVE"',
    'id="resistanceMode"', 'id="resistanceDataset"', 'id="resistanceSeed"',
])
def test_non_ble_sensor_controls_are_gone(work, removed):
    assert removed not in work, f"{removed} 가 화면에 남아 있다"


def test_configure_always_sends_ble_live(work):
    start = work.index("async function spacebioConfigureSensor")
    section = work[start:work.index(chr(10) + "}", start)]
    assert "BLE_LIVE" in section
    for other in ("CSV_REPLAY", "SYNTHETIC", "SERIAL_LIVE"):
        assert other not in section, f"{other} 분기가 남아 있다"


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


# ──────────────── 세션·측정 순서 (2026-07-29 혼동·오류 신고) ────────────────
#
# 두 버튼이 대등해 보이지만 하는 일이 다르다. 세션 = 파일 기록 구간,
# 측정 = 센서 스트림. 세션 없이 측정만 하면 화면에는 보여도 저장이 안 되고,
# 이미 도는 상태에서 다시 누르면 백엔드가 409를 냈다. 애초에 못 누르게 막는다.

def test_stale_recovery_reapplies_state_driven_disabling(work):
    """stale 해제 루프가 전부 되살리므로 그 뒤에 다시 입혀야 한다."""
    start = work.index("function spacebioSetStale")
    section = work[start:work.index(chr(10) + "}", start)]
    assert "spacebioSyncActionButtons" in section


def test_conflict_errors_are_shown_in_korean(work):
    start = work.index("async function spacebioFetch")
    section = work[start:work.index(chr(10) + "}", start)]
    assert "sensor_conflict" in section and "측정 정지" in section


# ───────── 측정 시작/정지가 세션을 자동으로 연다 (2026-07-29 단순화) ─────────
#
# 세션 시작/종료 버튼을 따로 두니 순서를 헷갈리고 잘못 누르면 409/422가 났다.
# 이제 운영자가 하는 일은 "실험명 입력 → 측정 시작"뿐이다.

def test_session_buttons_are_gone(work):
    for removed in ('id="spacebioSessionStart"', 'id="spacebioSessionFinish"'):
        assert removed not in work, f"{removed} 가 남아 있다"


def test_measure_start_opens_a_session(work):
    start = work.index("sbEl('resistanceStart').onclick")
    section = work[start:start + 600]
    assert "spacebioStartSession" in section
    assert "recording" in section, "이미 열린 세션이면 새로 만들지 않아야 한다"


def test_measure_stop_closes_the_session(work):
    start = work.index("sbEl('resistanceStop').onclick")
    section = work[start:start + 600]
    assert "sensor/stop" in section
    assert "spacebioFinishSession" in section


def test_finish_session_ignores_missing_session(work):
    """열린 세션이 없을 때 화면의 '-'를 보내 422가 나던 것을 막는다."""
    start = work.index("async function spacebioFinishSession")
    section = work[start:work.index(chr(10) + "}", start)]
    assert "'-'" in section and "return null" in section


def test_session_id_matches_the_server_pattern(work):
    """서버는 ^spacebio_\d{8}_\d{6}_[A-Za-z0-9]+$ 를 강제한다. 실험명을 그대로
    넣으면 하이픈 때문에 422가 난다 — 영숫자만 남겨 꼬리표로 써야 한다."""
    start = work.index("async function spacebioStartSession")
    section = work[start:work.index(chr(10) + "}", start)]
    assert "'spacebio_'" in section, "접두사를 지켜야 한다"
    assert "spacebioExperimentName" in section
    assert "[^A-Za-z0-9]" in section, "영숫자 외 문자를 걸러야 한다"


def test_panel_shows_where_data_is_saved(work):
    """저항 값이 어디에 쌓이는지 화면에 나와야 한다 — 경로는 서버가 준다."""
    assert 'id="spacebioDataPath"' in work
    assert "session.data_dir" in work
    assert "sensor_samples.csv" in work
    assert "/home/aiworker-1" not in work, "화면이 파이 경로를 하드코딩하면 안 된다"


# ───── 저항 측정 설정: Rref · 시간평균 (기존 앱 기능 이식, 2026-07-29) ─────


def test_rref_and_avg_inputs_exist_with_original_ranges(work):
    """원본 frontend_ble_web.py 의 허용 범위와 같아야 한다."""
    assert 'id="resistanceRref"' in work
    assert 'min="100"' in work and 'max="1000000"' in work
    assert 'id="resistanceAvgFactor"' in work
    assert 'max="200"' in work


def test_configure_sends_rref_and_avg_factor(work):
    start = work.index("async function spacebioConfigureSensor")
    section = work[start:work.index(chr(10) + "}", start)]
    assert "rref_ohm" in section and "avg_factor" in section
    assert "100" in section and "1000000" in section, "범위 밖 값을 보내면 422 가 난다"


def test_settings_are_locked_while_measuring(work):
    """Rref 를 바꾸면 펌웨어가 baseline 을 재설정한다 — 측정 중 변경은 기록을
    불연속하게 만든다."""
    start = work.index("function spacebioSyncActionButtons")
    section = work[start:work.index(chr(10) + "}", start)]
    assert "resistanceRref" in section and "resistanceAvgFactor" in section
    assert "measuring" in section


def test_panel_reflects_server_applied_settings(work):
    """화면이 자기가 보낸 값을 되뇌지 않고 서버가 쓰는 값을 보여줘야 한다."""
    assert "sensorStatus.rref_ohm" in work
    assert "sensorStatus.avg_factor" in work


# ───── 측정 정지 시 차트도 멈춘다 (2026-07-29 신고) ─────
#
# 정지해도 마지막 샘플이 status 에 남아 있어, 상태 메시지가 올 때마다 같은 점을
# 계속 찍으며 선이 옆으로 늘어났다. 정지하면 그래프도 멈춰야 한다.

def test_chart_only_grows_while_measuring(work):
    start = work.index("function spacebioPushPoint")
    section = work[start:work.index(chr(10) + "}", start)]
    assert "running" in section, "센서가 도는 동안에만 점을 추가해야 한다"


def test_chart_does_not_replot_the_same_sample(work):
    """상태 스트림(10Hz)이 같은 샘플을 여러 번 전해도 한 번만 찍어야 한다.
    평균 N을 올리면 기록이 7.8Hz 라 중복이 더 잦아진다 — 중복을 찍으면 x축이 뭉갠다."""
    start = work.index("function spacebioPushPoint")
    section = work[start:work.index(chr(10) + "}", start)]
    assert "spacebioLastPlottedTs" in section
    assert "source_timestamp_ms" in section


def test_chart_resets_when_a_new_measurement_starts(work):
    """새 측정은 빈 그래프에서 시작해야 이전 구간과 섞이지 않는다."""
    start = work.index("sbEl('resistanceStart').onclick")
    section = work[start:start + 900]
    assert "spacebioResetChart" in section
