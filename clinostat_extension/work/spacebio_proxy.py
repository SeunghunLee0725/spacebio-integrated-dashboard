"""클리노스텟(:8000) → SpaceBio Gateway(127.0.0.1:8010) 프록시 (설계 스펙 6.1).

이 모듈은 **클리노스텟 프로세스 안에서** 돈다. 브라우저는 Gateway에 직접
접속하지 않고 항상 `:8000`의 `/api/spacebio/*`를 경유한다.

안전 경계 두 가지가 이 파일의 존재 이유다.

1. Gateway 장애가 모터 제어로 전파되면 안 된다. 모든 호출에 시한이 걸려 있고,
   제어 루프 thread나 serial lock 안에서 호출하지 않는다(호출부 책임).
2. 변경 요청(POST)은 **자동 재시도하지 않는다.** 재시도는 중복 주입을 만든다.
   멱등한 GET만 1회 재시도한다.
"""

from __future__ import annotations

import asyncio
import re
import uuid
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

import httpx

#: Gateway는 loopback에만 바인딩된다. 외부 노출 지점은 기존 :8000 하나뿐이다.
GATEWAY_BASE_URL = "http://127.0.0.1:8010"

#: 스펙 6.1 — 연결 200 ms, GET 전체 500 ms, 변경 요청 1초, 재시도 대기 100 ms
CONNECT_TIMEOUT_S = 0.2
GET_TIMEOUT_S = 0.5
MUTATE_TIMEOUT_S = 1.0
GET_RETRY_DELAY_S = 0.1

#: STOP ALL이 펌프 정지에 쓰는 기본 시한. 기존 클리노스텟 정지를 막지 않는다.
STOP_ALL_PUMP_DEADLINE_S = 1.0

#: 브라우저 경로 → Gateway 경로. **허용목록이다** — 여기 없는 경로는 전달하지 않는다.
ROUTE_MAP: dict[str, str] = {
    "/api/spacebio/status": "/api/status",
    "/api/spacebio/sensor/status": "/api/sensor/status",
    "/api/spacebio/sensor/configure": "/api/sensor/configure",
    "/api/spacebio/sensor/start": "/api/sensor/start",
    "/api/spacebio/sensor/stop": "/api/sensor/stop",
    "/api/spacebio/sensor/datasets": "/api/sensor/datasets",
    "/api/spacebio/pump/status": "/api/pump/status",
    "/api/spacebio/pump/dispense": "/api/pump/dispense",
    "/api/spacebio/pump/step": "/api/pump/step",
    "/api/spacebio/pump/stop": "/api/pump/stop",
    "/api/spacebio/pump/emergency-stop": "/api/pump/emergency-stop",
    "/api/spacebio/pump/reset-emergency-stop": "/api/pump/reset-emergency-stop",
    "/api/spacebio/session/status": "/api/session/status",
    "/api/spacebio/session/start": "/api/session/start",
    "/api/spacebio/session/update": "/api/session/update",
    "/api/spacebio/session/finish": "/api/session/finish",
}

_PATH_RE = re.compile(r"(?:[A-Za-z]:)?(?:/[\w.\-]+){2,}")
_TRACEBACK_MARKERS = ("Traceback (most recent call last)", 'File "')
_GENERIC_ERROR = "gateway error"


class ProxyPathError(ValueError):
    """허용목록에 없는 경로 — Gateway로 전달하지 않는다."""


class ProxyGatewayError(Exception):
    """Gateway가 낸 오류 또는 Gateway 도달 실패. HTTP status와 구조화된 error를 보존한다."""

    def __init__(self, status_code: int, error: dict[str, str]) -> None:
        super().__init__(error.get("message", ""))
        self.status_code = status_code
        self.error = error


@dataclass(frozen=True)
class StopResult:
    """STOP ALL 결과 — 두 정지를 독립적으로 보고한다 (스펙 6.1/7)."""

    clinostat_ok: bool
    pump_ok: bool
    clinostat_error: Optional[str] = None
    pump_error: Optional[str] = None


def gateway_path(browser_path: str) -> str:
    """브라우저 경로를 Gateway 경로로 바꾼다. 허용목록 밖이면 거부한다."""
    mapped = ROUTE_MAP.get(browser_path)
    if mapped is None:
        raise ProxyPathError(f"not a proxied SpaceBio route: {browser_path!r}")
    return mapped


def sanitize_message(message: str) -> str:
    """Gateway 내부 traceback·파일 경로가 브라우저로 새지 않게 한다 (스펙 6.1)."""
    if not message:
        return ""
    if any(marker in message for marker in _TRACEBACK_MARKERS):
        return _GENERIC_ERROR
    first_line = message.splitlines()[0]
    return _PATH_RE.sub("<path>", first_line)[:500]


def _sanitized_error(payload: Any, fallback_code: str) -> dict[str, str]:
    error = payload.get("error") if isinstance(payload, dict) else None
    if not isinstance(error, dict):
        return {"code": fallback_code, "message": _GENERIC_ERROR}
    return {
        "code": str(error.get("code", fallback_code)),
        "message": sanitize_message(str(error.get("message", ""))),
    }


class SpaceBioProxy:
    """Gateway로 가는 bounded HTTP 클라이언트. lifespan이 소유하는 단일 인스턴스."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._client = client
        self._sleep = sleep

    async def get(self, path: str, request_id: Optional[str]) -> dict[str, Any]:
        """멱등 GET. 전송 실패 시 100 ms 뒤 **1회만** 재시도한다."""
        target = gateway_path(path)
        rid = request_id or new_request_id()
        try:
            return await self._send("GET", target, None, rid, GET_TIMEOUT_S)
        except _TransportFailure:
            await self._sleep(GET_RETRY_DELAY_S)
            try:
                return await self._send("GET", target, None, rid, GET_TIMEOUT_S)
            except _TransportFailure as exc:
                raise ProxyGatewayError(
                    504, {"code": "gateway_unreachable", "message": str(exc)}
                ) from exc

    async def mutate(
        self, path: str, body: dict[str, Any], request_id: Optional[str]
    ) -> dict[str, Any]:
        """변경 요청. **자동 재시도하지 않는다** — 중복 주입을 만들 수 있다."""
        target = gateway_path(path)
        rid = request_id or new_request_id()
        try:
            return await self._send("POST", target, body, rid, MUTATE_TIMEOUT_S)
        except _TransportFailure as exc:
            raise ProxyGatewayError(
                504, {"code": "gateway_unreachable", "message": str(exc)}
            ) from exc

    async def _send(
        self,
        method: str,
        target: str,
        body: Optional[dict[str, Any]],
        request_id: str,
        timeout_s: float,
    ) -> dict[str, Any]:
        timeout = httpx.Timeout(timeout_s, connect=CONNECT_TIMEOUT_S)
        try:
            response = await self._client.request(
                method, target, json=body,
                headers={"X-Request-ID": request_id}, timeout=timeout,
            )
        except httpx.HTTPError as exc:
            raise _TransportFailure(str(exc) or type(exc).__name__) from exc

        try:
            payload = response.json()
        except ValueError as exc:
            # 본문을 그대로 노출하면 Gateway 내부가 샐 수 있다.
            raise ProxyGatewayError(
                502, {"code": "gateway_bad_response",
                      "message": "gateway returned a non-JSON body"},
            ) from exc

        if response.status_code >= 400:
            raise ProxyGatewayError(
                response.status_code, _sanitized_error(payload, "gateway_error")
            )
        return payload


class _TransportFailure(Exception):
    """Gateway에 닿지 못했다 — 재시도 판단을 위해 응답 오류와 구분한다."""


def new_request_id() -> str:
    return str(uuid.uuid4())


async def stop_all(
    *,
    clinostat_stop: Callable[[], Awaitable[Any]],
    proxy: Any,
    request_id: Optional[str] = None,
    pump_deadline_s: float = STOP_ALL_PUMP_DEADLINE_S,
) -> StopResult:
    """STOP ALL — **기존 클리노스텟 정지를 먼저** 호출하고, 그 결과와 무관하게
    별도 bounded task로 펌프 정지를 요청한다 (스펙 6.1).

    어느 쪽이 실패해도 예외를 올리지 않는다. 두 결과를 독립적으로 보고해
    UI가 각각 표시할 수 있게 한다. 비상 정지 경로가 예외로 죽는 것이 가장
    나쁜 실패 방식이다.
    """
    clinostat_ok, clinostat_error = True, None
    try:
        await clinostat_stop()
    except Exception as exc:  # noqa: BLE001 — 기존 정지 실패가 펌프 정지를 막으면 안 된다
        clinostat_ok, clinostat_error = False, sanitize_message(str(exc))

    pump_ok, pump_error = True, None
    rid = request_id or new_request_id()
    try:
        await asyncio.wait_for(
            proxy.mutate("/api/spacebio/pump/stop", {"request_id": rid}, rid),
            timeout=pump_deadline_s,
        )
    except asyncio.TimeoutError:
        pump_ok, pump_error = False, "pump stop timed out"
    except ProxyGatewayError as exc:
        pump_ok, pump_error = False, exc.error.get("message", _GENERIC_ERROR)
    except Exception as exc:  # noqa: BLE001
        pump_ok, pump_error = False, sanitize_message(str(exc))

    return StopResult(
        clinostat_ok=clinostat_ok, pump_ok=pump_ok,
        clinostat_error=clinostat_error, pump_error=pump_error,
    )
