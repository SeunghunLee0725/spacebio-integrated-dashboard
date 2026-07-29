"""Gateway `/ws/status` WebSocket 테스트 (설계 스펙 6장).

사이클 2: 연결 직후 전체 상태 → 이후 sequence 메시지, sequence는 프로세스
수명 동안 단조 증가(연결마다 리셋 아님), 발행 상한 10Hz, 연결 해제 시
정리(task cancel + await), 16 KiB 메시지 상한 가드.
"""

from __future__ import annotations

import time

from fastapi.testclient import TestClient

from gateway.app import _build_status_message
from gateway.api_models import GatewayComponent, GatewayState, GatewayStatus, PumpStatus, SensorStatus, SessionStatus

EXPECTED_TOP_LEVEL_KEYS = {"gateway", "sensor", "pump", "session"}


def _receive_status(ws) -> dict:
    msg = ws.receive_json()
    assert msg["type"] == "status"
    assert isinstance(msg["sequence"], int)
    assert set(msg["data"].keys()) == EXPECTED_TOP_LEVEL_KEYS
    return msg


def test_ws_sends_full_state_immediately_on_connect(client: TestClient):
    with client.websocket_connect("/ws/status") as ws:
        _receive_status(ws)


def test_ws_sequence_is_monotonic_and_not_reset_per_connection(client: TestClient):
    with client.websocket_connect("/ws/status") as ws:
        first = _receive_status(ws)["sequence"]
        second = _receive_status(ws)["sequence"]
    assert second > first

    # 새 연결이어도 sequence가 처음(1)으로 되돌아가지 않는다 — 프로세스 수명 동안 단조 증가.
    with client.websocket_connect("/ws/status") as ws2:
        third = _receive_status(ws2)["sequence"]
    assert third > second


def test_ws_publish_rate_is_capped_at_10hz(client: TestClient):
    with client.websocket_connect("/ws/status") as ws:
        _receive_status(ws)  # 최초 전체 상태 — 타이밍 측정에서 제외한다
        start = time.monotonic()
        for _ in range(4):
            _receive_status(ws)
        elapsed = time.monotonic() - start
    # 4개 메시지 사이 3구간이 10Hz(0.1s) 이하로 발행되면 최소 ~0.3s가 걸려야 한다.
    assert elapsed >= 0.25


def test_ws_disconnect_is_clean_across_repeated_connections(client: TestClient):
    """반복 연결/해제가 예외 없이 끝난다 — task cancel+await 정리의 간접 증거."""
    for _ in range(5):
        with client.websocket_connect("/ws/status") as ws:
            _receive_status(ws)


def test_ws_status_message_size_guard_drops_oversized_payload():
    status = GatewayStatus(
        gateway=GatewayComponent(state=GatewayState.ONLINE),
        sensor=SensorStatus(), pump=PumpStatus(), session=SessionStatus(),
    )
    payload = _build_status_message(1, status)
    assert payload is not None
    assert len(payload.encode("utf-8")) <= 16 * 1024


def test_ws_status_message_size_guard_rejects_when_forced_over_cap(monkeypatch):
    import gateway.app as app_module

    status = GatewayStatus(
        gateway=GatewayComponent(state=GatewayState.ONLINE),
        sensor=SensorStatus(), pump=PumpStatus(), session=SessionStatus(),
    )
    monkeypatch.setattr(app_module, "MAX_WS_MESSAGE_BYTES", 10)
    assert app_module._build_status_message(1, status) is None
