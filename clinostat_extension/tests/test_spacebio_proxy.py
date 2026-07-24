"""클리노스텟 → Gateway 프록시 계약 테스트 (설계 스펙 6.1).

실제 네트워크를 쓰지 않는다. httpx.MockTransport로 Gateway를 흉내내고,
지연은 가짜 sleep으로 주입해 테스트가 실시간을 기다리지 않게 한다.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "work"))

import spacebio_proxy as sp  # noqa: E402


def _envelope(data, request_id="rid"):
    return {"schema_version": 1, "request_id": request_id,
            "server_time": "2026-07-24T10:00:00+09:00", "data": data}


def _error(code, message, request_id="rid"):
    return {"schema_version": 1, "request_id": request_id,
            "server_time": "2026-07-24T10:00:00+09:00",
            "error": {"code": code, "message": message}}


def _proxy(handler, **kwargs):
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url=sp.GATEWAY_BASE_URL,
    )
    return sp.SpaceBioProxy(client=client, **kwargs)


# ─────────────────────────── 경로 계약 ───────────────────────────

def test_browser_routes_map_to_gateway_routes():
    """브라우저가 부르는 /api/spacebio/* 가 Gateway 경로로 정확히 대응된다."""
    assert sp.gateway_path("/api/spacebio/status") == "/api/status"
    assert sp.gateway_path("/api/spacebio/sensor/configure") == "/api/sensor/configure"
    assert sp.gateway_path("/api/spacebio/pump/emergency-stop") == "/api/pump/emergency-stop"
    assert sp.gateway_path("/api/spacebio/session/start") == "/api/session/start"


@pytest.mark.parametrize("path", [
    "/api/spacebio/../admin", "/api/spacebio//etc/passwd", "/api/other/status",
    "/api/spacebio/unknown", "",
])
def test_unknown_or_traversal_paths_are_rejected(path):
    """허용목록 밖 경로는 Gateway로 전달하지 않는다."""
    with pytest.raises(sp.ProxyPathError):
        sp.gateway_path(path)


def test_every_spec_route_is_allowlisted():
    expected = {
        "/api/spacebio/status",
        "/api/spacebio/sensor/configure", "/api/spacebio/sensor/start",
        "/api/spacebio/sensor/stop",
        "/api/spacebio/pump/dispense", "/api/spacebio/pump/stop",
        "/api/spacebio/pump/emergency-stop", "/api/spacebio/pump/reset-emergency-stop",
        "/api/spacebio/session/start", "/api/spacebio/session/finish",
    }
    assert expected <= set(sp.ROUTE_MAP)


# ─────────────────────────── 전달과 request id ───────────────────────────

@pytest.mark.asyncio
async def test_get_forwards_request_id_header_and_returns_data():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["rid"] = request.headers.get("X-Request-ID")
        return httpx.Response(200, json=_envelope({"gateway": {"state": "online"}}))

    result = await _proxy(handler).get("/api/spacebio/status", request_id="R-1")
    assert seen["path"] == "/api/status"
    assert seen["rid"] == "R-1"
    assert result["data"]["gateway"]["state"] == "online"


@pytest.mark.asyncio
async def test_mutate_posts_body_and_forwards_request_id():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["body"] = request.read().decode()
        return httpx.Response(200, json=_envelope({"state": "running"}))

    result = await _proxy(handler).mutate(
        "/api/spacebio/pump/dispense",
        {"request_id": "R-2", "rate_ul_s": 50.0, "target_volume_ul": 120.0},
        request_id="R-2",
    )
    assert seen["method"] == "POST"
    assert seen["path"] == "/api/pump/dispense"
    assert "rate_ul_s" in seen["body"]
    assert result["data"]["state"] == "running"


@pytest.mark.asyncio
async def test_missing_request_id_is_generated():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["rid"] = request.headers.get("X-Request-ID")
        return httpx.Response(200, json=_envelope({}))

    await _proxy(handler).get("/api/spacebio/status", request_id=None)
    assert seen["rid"], "request id가 없으면 클리노스텟이 생성해야 한다"
    assert len(seen["rid"]) >= 8


# ─────────────────────────── 오류 전달 ───────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("status,code", [
    (409, "pump_estop_latched"), (422, "validation_error"), (503, "pump_fault"),
])
async def test_gateway_error_status_and_code_are_preserved(status, code):
    def handler(request):
        return httpx.Response(status, json=_error(code, "nope"))

    with pytest.raises(sp.ProxyGatewayError) as exc:
        await _proxy(handler).mutate("/api/spacebio/pump/stop", {}, request_id="R")
    assert exc.value.status_code == status
    assert exc.value.error["code"] == code


@pytest.mark.asyncio
async def test_internal_traceback_is_never_forwarded():
    leak = 'Traceback (most recent call last):\n  File "/srv/gateway/app.py", line 42'

    def handler(request):
        return httpx.Response(503, json=_error("internal_error", leak))

    with pytest.raises(sp.ProxyGatewayError) as exc:
        await _proxy(handler).mutate("/api/spacebio/pump/stop", {}, request_id="R")
    message = exc.value.error["message"]
    assert "Traceback" not in message
    assert "/srv/gateway" not in message


@pytest.mark.asyncio
async def test_malformed_gateway_response_becomes_structured_error():
    def handler(request):
        return httpx.Response(200, content=b"<html>not json</html>")

    with pytest.raises(sp.ProxyGatewayError) as exc:
        await _proxy(handler).get("/api/spacebio/status", request_id="R")
    assert exc.value.status_code == 502
    assert "<html>" not in exc.value.error["message"]


# ─────────────────────────── timeout / retry ───────────────────────────

def test_timeouts_match_spec():
    """연결 200 ms, GET 전체 500 ms, 변경 요청 1초 (스펙 6.1)."""
    assert sp.CONNECT_TIMEOUT_S == pytest.approx(0.2)
    assert sp.GET_TIMEOUT_S == pytest.approx(0.5)
    assert sp.MUTATE_TIMEOUT_S == pytest.approx(1.0)
    assert sp.GET_RETRY_DELAY_S == pytest.approx(0.1)


@pytest.mark.asyncio
async def test_idempotent_get_retries_once_after_100ms():
    calls = {"n": 0}
    slept: list[float] = []

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectTimeout("boom")
        return httpx.Response(200, json=_envelope({"ok": True}))

    async def fake_sleep(seconds):
        slept.append(seconds)

    result = await _proxy(handler, sleep=fake_sleep).get(
        "/api/spacebio/status", request_id="R"
    )
    assert calls["n"] == 2, "GET은 1회 재시도해야 한다"
    assert slept == [pytest.approx(0.1)]
    assert result["data"]["ok"] is True


@pytest.mark.asyncio
async def test_get_gives_up_after_one_retry():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        raise httpx.ConnectTimeout("boom")

    with pytest.raises(sp.ProxyGatewayError) as exc:
        await _proxy(handler, sleep=lambda s: asyncio.sleep(0)).get(
            "/api/spacebio/status", request_id="R"
        )
    assert calls["n"] == 2, "재시도는 1회뿐이어야 한다"
    assert exc.value.status_code == 504


@pytest.mark.asyncio
async def test_mutations_are_never_auto_retried():
    """POST 자동 재시도는 중복 주입을 만들 수 있다 — 절대 금지 (스펙 6.1)."""
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        raise httpx.ConnectTimeout("boom")

    with pytest.raises(sp.ProxyGatewayError):
        await _proxy(handler).mutate(
            "/api/spacebio/pump/dispense", {"rate_ul_s": 50.0}, request_id="R"
        )
    assert calls["n"] == 1, "변경 요청은 단 한 번만 보내야 한다"


# ─────────────────────────── STOP ALL 격리 ───────────────────────────

@pytest.mark.asyncio
async def test_stop_all_calls_clinostat_stop_first_then_pump():
    order: list[str] = []

    def handler(request):
        order.append("pump_stop")
        return httpx.Response(200, json=_envelope({"state": "stopped"}))

    async def clinostat_stop():
        order.append("clinostat_stop")
        return {"stopped": True}

    result = await sp.stop_all(
        clinostat_stop=clinostat_stop, proxy=_proxy(handler), request_id="R",
    )
    assert order == ["clinostat_stop", "pump_stop"]
    assert result.clinostat_ok is True
    assert result.pump_ok is True


@pytest.mark.asyncio
async def test_stop_all_still_stops_pump_when_clinostat_stop_fails():
    """기존 정지가 실패해도 펌프 정지는 반드시 시도한다."""
    called = {"pump": False}

    def handler(request):
        called["pump"] = True
        return httpx.Response(200, json=_envelope({"state": "stopped"}))

    async def failing_stop():
        raise RuntimeError("serial port gone")

    result = await sp.stop_all(
        clinostat_stop=failing_stop, proxy=_proxy(handler), request_id="R",
    )
    assert called["pump"] is True
    assert result.clinostat_ok is False
    assert result.pump_ok is True


@pytest.mark.asyncio
async def test_stop_all_reports_pump_failure_without_raising():
    """Gateway가 죽어 있어도 STOP ALL 자체가 예외로 죽으면 안 된다."""
    def handler(request):
        raise httpx.ConnectError("gateway down")

    async def clinostat_stop():
        return {"stopped": True}

    result = await sp.stop_all(
        clinostat_stop=clinostat_stop, proxy=_proxy(handler), request_id="R",
    )
    assert result.clinostat_ok is True
    assert result.pump_ok is False
    assert result.pump_error is not None


@pytest.mark.asyncio
async def test_gateway_hang_does_not_block_beyond_bounded_deadline():
    """Gateway가 매달려도 STOP ALL은 정해진 시한 안에 돌아온다."""
    async def hanging_stop_pump(*args, **kwargs):
        await asyncio.sleep(5)

    class HangingProxy:
        async def mutate(self, *args, **kwargs):
            await hanging_stop_pump()

    async def clinostat_stop():
        return {"stopped": True}

    result = await asyncio.wait_for(
        sp.stop_all(clinostat_stop=clinostat_stop, proxy=HangingProxy(),
                    request_id="R", pump_deadline_s=0.05),
        timeout=2.0,
    )
    assert result.clinostat_ok is True
    assert result.pump_ok is False


# ─────────────────────────── 바인딩 ───────────────────────────

def test_gateway_base_url_is_loopback_only():
    """브라우저는 Gateway에 직접 접속하지 않는다 — 프록시만 loopback으로 간다."""
    assert sp.GATEWAY_BASE_URL.startswith("http://127.0.0.1:")
    assert "0.0.0.0" not in sp.GATEWAY_BASE_URL
