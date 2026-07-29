"""모의 펌프 안전 상태기계 테스트 (설계 스펙 6.4/6.5).

가짜 clock을 주입해 sleep 없이 경과 시간을 제어한다. 동작 그룹별로 묶어
전체 상태기계를 한 번에 검증한다.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gateway.api_models import (  # noqa: E402
    ESTOP_RESET_ACKNOWLEDGEMENT,
    PumpDispenseRequest,
    PumpEmergencyStopRequest,
    PumpResetEmergencyStopRequest,
    PumpState,
    PumpStopRequest,
)
from gateway.idempotency import IdempotencyCache  # noqa: E402
from gateway.simulated_pump import (  # noqa: E402
    FileEstopLatchPersistence,
    PumpConflictError,
    PumpEvent,
    PumpFaultError,
    SimulatedPump,
)


class FakeMonotonicClock:
    """time.monotonic_ns() 대체 — 테스트가 시각을 직접 전진시킨다."""

    def __init__(self, start_ns: int = 0) -> None:
        self._now_ns = start_ns

    def __call__(self) -> int:
        return self._now_ns

    def advance_s(self, seconds: float) -> None:
        self._now_ns += int(seconds * 1_000_000_000)


class FakeWallClock:
    """idempotency 캐시의 time.time() 대체."""

    def __init__(self, start_s: float = 0.0) -> None:
        self._now_s = start_s

    def __call__(self) -> float:
        return self._now_s

    def advance_s(self, seconds: float) -> None:
        self._now_s += seconds


class EventRecorder:
    def __init__(self) -> None:
        self.events: list[PumpEvent] = []

    def __call__(self, event: PumpEvent) -> None:
        self.events.append(event)


def _make_pump(**kwargs) -> tuple[SimulatedPump, FakeMonotonicClock]:
    clock = FakeMonotonicClock()
    pump = SimulatedPump(monotonic_ns=clock, **kwargs)
    return pump, clock


def _dispense_request(request_id: str = "r1", rate: float = 10.0,
                       target: float = 100.0) -> PumpDispenseRequest:
    return PumpDispenseRequest(request_id=request_id, rate_ul_s=rate, target_volume_ul=target)


# ─────────────────────────── 기본 주입 수명주기 ───────────────────────────

@pytest.mark.asyncio
async def test_dispense_runs_then_completes_and_caps_exactly_at_target():
    events = EventRecorder()
    pump, clock = _make_pump(event_sink=events)

    status = await pump.dispense(_dispense_request(rate=10.0, target=100.0))
    assert status.state is PumpState.RUNNING
    assert status.delivered_volume_ul == 0.0

    clock.advance_s(5.0)  # 50 / 100 delivered — well within progress
    mid = await pump.status()
    assert mid.state is PumpState.RUNNING
    assert mid.delivered_volume_ul == pytest.approx(50.0)

    clock.advance_s(5.0)  # 100 / 100 — exactly on target
    done = await pump.status()
    assert done.state is PumpState.COMPLETED
    assert done.delivered_volume_ul == 100.0

    causes = [e.cause for e in events.events]
    assert causes == ["dispense_start", "dispense_completed"]


@pytest.mark.asyncio
async def test_completion_tolerance_caps_exactly_and_never_overshoots():
    """허용오차: max(0.1 uL, 목표의 0.1%) 이내면 완료, 값은 정확히 목표로 cap."""
    pump, clock = _make_pump()
    await pump.dispense(_dispense_request(rate=10.0, target=100.0))
    # tolerance = max(0.1, 100*0.001) = 0.1 -> delivered >= 99.9 triggers completion
    clock.advance_s(9.985)  # delivered = 99.85 -> still short of 99.9
    still_running = await pump.status()
    assert still_running.state is PumpState.RUNNING
    assert still_running.delivered_volume_ul == pytest.approx(99.85)

    clock.advance_s(1000.0)  # far overshoot in raw elapsed*rate terms
    completed = await pump.status()
    assert completed.state is PumpState.COMPLETED
    assert completed.delivered_volume_ul == 100.0  # capped, never exceeds target


@pytest.mark.asyncio
async def test_concurrent_dispense_is_rejected_with_conflict():
    pump, clock = _make_pump()
    await pump.dispense(_dispense_request(request_id="r1"))
    with pytest.raises(PumpConflictError):
        await pump.dispense(_dispense_request(request_id="r2"))


@pytest.mark.asyncio
async def test_stop_preserves_delivered_volume_and_does_not_latch():
    events = EventRecorder()
    pump, clock = _make_pump(event_sink=events)
    await pump.dispense(_dispense_request(rate=10.0, target=100.0))
    clock.advance_s(3.0)  # 30 delivered, well short of completion

    stopped = await pump.stop(PumpStopRequest(request_id="s1"))
    assert stopped.state is PumpState.STOPPED
    assert stopped.delivered_volume_ul == pytest.approx(30.0)
    assert stopped.estop_latched is False

    # stop is idle-safe / idempotent when nothing is running
    again = await pump.stop(PumpStopRequest(request_id="s2"))
    assert again.state is PumpState.STOPPED
    assert again.delivered_volume_ul == pytest.approx(30.0)
    assert [e.cause for e in events.events] == ["dispense_start", "stop"]


# ─────────────────────────── 세션 누적량 분리 ───────────────────────────

@pytest.mark.asyncio
async def test_session_cumulative_resets_on_new_session_and_stays_separate():
    pump, clock = _make_pump()

    # no session active yet -> session_cumulative stays 0 even after a full dispense
    await pump.dispense(_dispense_request(target=50.0, rate=50.0))
    clock.advance_s(1.0)
    status = await pump.status()
    assert status.state is PumpState.COMPLETED
    assert status.session_cumulative_volume_ul == 0.0

    await pump.begin_session()
    await pump.dispense(_dispense_request(request_id="r2", target=20.0, rate=20.0))
    clock.advance_s(1.0)
    status = await pump.status()
    assert status.session_cumulative_volume_ul == pytest.approx(20.0)

    await pump.dispense(_dispense_request(request_id="r3", target=10.0, rate=10.0))
    clock.advance_s(1.0)
    status = await pump.status()
    assert status.session_cumulative_volume_ul == pytest.approx(30.0)

    await pump.begin_session()  # new session -> reset to 0
    status = await pump.status()
    assert status.session_cumulative_volume_ul == 0.0


# ─────────────────────────── 멱등성 ───────────────────────────

@pytest.mark.asyncio
async def test_duplicate_request_id_returns_stored_response_without_new_run():
    events = EventRecorder()
    wall_clock = FakeWallClock()
    pump, clock = _make_pump(
        event_sink=events, idempotency_cache=IdempotencyCache(now_fn=wall_clock)
    )

    first = await pump.dispense(_dispense_request(request_id="dup", rate=10.0, target=100.0))
    clock.advance_s(1.0)
    second = await pump.dispense(_dispense_request(request_id="dup", rate=200.0, target=1000.0))

    assert second is first  # exact stored response, no new run
    assert [e.cause for e in events.events] == ["dispense_start"]


@pytest.mark.asyncio
async def test_idempotency_cache_expires_after_24_hours():
    wall_clock = FakeWallClock()
    pump, clock = _make_pump(idempotency_cache=IdempotencyCache(now_fn=wall_clock))

    await pump.dispense(_dispense_request(request_id="dup", rate=10.0, target=10.0))
    clock.advance_s(10.0)
    await pump.status()  # let it complete so a repeat dispense isn't a 409

    wall_clock.advance_s(24 * 60 * 60 + 1)  # just past 24h TTL
    fresh = await pump.dispense(_dispense_request(request_id="dup", rate=5.0, target=5.0))
    assert fresh.state is PumpState.RUNNING
    assert fresh.rate_ul_s == 5.0  # treated as a brand-new dispense, not the cached one


# ─────────────────────────── 비상정지 래치 ───────────────────────────

@pytest.mark.asyncio
async def test_emergency_stop_latches_and_persists_across_restart(tmp_path):
    latch_path = tmp_path / "estop_latch.json"
    persistence = FileEstopLatchPersistence(latch_path)
    pump, clock = _make_pump(estop_persistence=persistence)

    await pump.dispense(_dispense_request(rate=10.0, target=100.0))
    clock.advance_s(2.0)
    stopped = await pump.emergency_stop(PumpEmergencyStopRequest(request_id="e1"))
    assert stopped.state is PumpState.EMERGENCY_STOPPED
    assert stopped.estop_latched is True

    # simulate a Gateway/Pi restart: new SimulatedPump reading the same persisted path
    restarted, _ = _make_pump(estop_persistence=FileEstopLatchPersistence(latch_path))
    status = await restarted.status()
    assert status.state is PumpState.EMERGENCY_STOPPED
    assert status.estop_latched is True


@pytest.mark.asyncio
async def test_latch_blocks_dispense_but_allows_status_stop_reset(tmp_path):
    persistence = FileEstopLatchPersistence(tmp_path / "latch.json")
    pump, clock = _make_pump(estop_persistence=persistence)
    await pump.emergency_stop(PumpEmergencyStopRequest(request_id="e1"))

    with pytest.raises(PumpConflictError):
        await pump.dispense(_dispense_request(request_id="r1"))

    # exempt while latched: status / stop / reset must not raise
    await pump.status()
    await pump.stop(PumpStopRequest(request_id="s1"))


@pytest.mark.asyncio
async def test_repeat_emergency_stop_is_idempotent_not_a_conflict(tmp_path):
    """정지·비상정지는 멱등이어야 한다(스펙 6장) — 6.5의 허용목록보다 우선한다.

    운영자가 estop을 두 번 누르거나 UI의 STOP ALL이 재시도해도 에러 없이 현재
    상태 스냅샷을 반환해야 한다.
    """
    events = EventRecorder()
    persistence = FileEstopLatchPersistence(tmp_path / "latch.json")
    pump, clock = _make_pump(estop_persistence=persistence, event_sink=events)

    first = await pump.emergency_stop(PumpEmergencyStopRequest(request_id="e1"))
    second = await pump.emergency_stop(PumpEmergencyStopRequest(request_id="e2"))

    assert second.state is PumpState.EMERGENCY_STOPPED
    assert second.estop_latched is True
    assert second.delivered_volume_ul == first.delivered_volume_ul
    assert [e.cause for e in events.events] == ["emergency_stop", "emergency_stop_repeat"]


@pytest.mark.asyncio
async def test_dispense_with_cached_request_id_is_still_rejected_while_latched(tmp_path):
    """멱등 캐시가 래치 검사보다 먼저면, 래치 중 과거 성공 request_id 재전송이 stale한
    RUNNING/COMPLETED 스냅샷을 재생해버린다 — UI가 실제로는 emergency_stopped인 펌프를
    running으로 오인하게 되므로 래치 검사가 캐시 조회보다 먼저여야 한다."""
    persistence = FileEstopLatchPersistence(tmp_path / "latch.json")
    pump, clock = _make_pump(estop_persistence=persistence)

    first = await pump.dispense(_dispense_request(request_id="dup", rate=10.0, target=100.0))
    assert first.state is PumpState.RUNNING

    await pump.emergency_stop(PumpEmergencyStopRequest(request_id="e1"))

    with pytest.raises(PumpConflictError):
        await pump.dispense(_dispense_request(request_id="dup", rate=10.0, target=100.0))


@pytest.mark.asyncio
async def test_emergency_stop_while_running_adds_delivered_to_session_cumulative(tmp_path):
    """비상정지 전까지 실제로 전달된 양은 세션 누적에 반영되어야 한다 — 그렇지 않으면
    약물이 실제로 들어갔는데 세션 총량이 과소 보고되는 안전상 반대 방향 오류가 생긴다."""
    persistence = FileEstopLatchPersistence(tmp_path / "latch.json")
    pump, clock = _make_pump(estop_persistence=persistence)
    await pump.begin_session()

    await pump.dispense(_dispense_request(rate=10.0, target=100.0))
    clock.advance_s(3.0)  # 30 uL delivered before the operator hits e-stop

    stopped = await pump.emergency_stop(PumpEmergencyStopRequest(request_id="e1"))
    assert stopped.state is PumpState.EMERGENCY_STOPPED
    assert stopped.estop_latched is True
    assert stopped.delivered_volume_ul == pytest.approx(30.0)
    assert stopped.session_cumulative_volume_ul == pytest.approx(30.0)


@pytest.mark.asyncio
async def test_reset_with_correct_acknowledgement_clears_latch(tmp_path):
    persistence = FileEstopLatchPersistence(tmp_path / "latch.json")
    pump, clock = _make_pump(estop_persistence=persistence)
    await pump.emergency_stop(PumpEmergencyStopRequest(request_id="e1"))

    result = await pump.reset_emergency_stop(
        PumpResetEmergencyStopRequest(
            request_id="rst1", acknowledgement=ESTOP_RESET_ACKNOWLEDGEMENT,
        )
    )
    assert result.previous_state is PumpState.EMERGENCY_STOPPED
    assert result.state is PumpState.IDLE
    assert result.estop_latched is False
    assert result.accepted is True

    status = await pump.status()
    assert status.state is PumpState.IDLE
    assert status.estop_latched is False
    assert persistence.load() is False  # cleared on disk too


@pytest.mark.asyncio
async def test_reset_without_a_latch_is_an_idempotent_success():
    pump, clock = _make_pump()
    result = await pump.reset_emergency_stop(
        PumpResetEmergencyStopRequest(
            request_id="rst1", acknowledgement=ESTOP_RESET_ACKNOWLEDGEMENT,
        )
    )
    assert result.previous_state is PumpState.IDLE
    assert result.state is PumpState.IDLE
    assert result.estop_latched is False
    assert result.accepted is True


def test_reset_acknowledgement_must_match_exactly_or_422():
    """acknowledgement 불일치는 api_models의 pydantic Literal 검증에서 422로 거부된다."""
    with pytest.raises(ValidationError):
        PumpResetEmergencyStopRequest(request_id="rst1", acknowledgement="not_it")


# ─────────────────────────── fault ───────────────────────────

@pytest.mark.asyncio
async def test_persistence_failure_transitions_to_fault():
    class BrokenPersistence:
        def load(self) -> bool:
            return False

        def save(self, latched: bool) -> None:
            raise OSError("disk full")

    events = EventRecorder()
    pump, clock = _make_pump(estop_persistence=BrokenPersistence(), event_sink=events)

    with pytest.raises(PumpFaultError):
        await pump.emergency_stop(PumpEmergencyStopRequest(request_id="e1"))

    status = await pump.status()
    assert status.state is PumpState.FAULT
    assert [e.cause for e in events.events] == ["fault"]
