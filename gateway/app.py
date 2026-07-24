"""SpaceBio Gateway FastAPI 앱 조립 (설계 스펙 6장).

라우트 계약은 `gateway/routes.py`, 상태 전이는 `gateway/runtime.py`가 맡는다.
여기서는 앱 생성, lifespan(재시작 복구 + 종료 정리), 오류 -> HTTP 상태 코드
매핑, 요청 본문 크기 상한, `/ws/status` WebSocket만 다룬다.

⚠ 바인딩 안전 제약: 이 모듈은 host/port를 직접 고르지 않는다 — 항상
`GatewayConfig`(기본은 config.yaml)에서 읽고, `load_config`가 all-interfaces
바인딩을 거부한다. `__main__`도 그 값을 그대로 uvicorn에 넘긴다.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.exceptions import RequestValidationError
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse
from starlette.types import ASGIApp

from gateway.api_models import GatewayStatus, StatusMessage, error_envelope
from gateway.http_support import request_id as extract_request_id
from gateway.routes import router
from gateway.runtime import GatewayConfig, GatewayRuntime, SensorConflictError, load_config
from gateway.sensor_source import SensorSourceError
from gateway.session_store import (
    InsufficientSpaceError,
    SessionConflictError,
    SessionIoError,
    SessionNotActiveError,
)
from gateway.simulated_pump import PumpConflictError, PumpFaultError

logger = logging.getLogger("gateway.app")

#: 요청 본문 크기 상한 (설계 스펙 6장).
MAX_BODY_BYTES = 64 * 1024

#: WebSocket 상태 메시지 크기 상한 (설계 스펙 6장).
MAX_WS_MESSAGE_BYTES = 16 * 1024


def _error_response(request: Request, status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content=error_envelope(request_id=extract_request_id(request), code=code, message=message),
    )


class BodySizeLimitMiddleware(BaseHTTPMiddleware):
    """본문이 64 KiB를 넘으면 413으로 거부한다."""

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(self, request: Request, call_next):
        content_length = request.headers.get("content-length")
        if content_length is not None and int(content_length) > MAX_BODY_BYTES:
            return _error_response(
                request, 413, "payload_too_large", "request body exceeds 64 KiB limit",
            )
        body = await request.body()
        if len(body) > MAX_BODY_BYTES:
            return _error_response(
                request, 413, "payload_too_large", "request body exceeds 64 KiB limit",
            )
        return await call_next(request)


# ─────────────────────────── 오류 -> 상태 코드 매핑 (설계 스펙 6장) ───────────────────────────
# 잘못된 상태 전이 -> 409, 유효성 오류 -> 422, 내부 장치 오류 -> 503.

async def _handle_validation_error(request: Request, exc: Exception) -> JSONResponse:
    return _error_response(request, 422, "validation_error", str(exc))


async def _handle_conflict(request: Request, exc: Exception) -> JSONResponse:
    code = getattr(exc, "code", "conflict")
    return _error_response(request, 409, code, str(exc))


async def _handle_device_fault(request: Request, exc: Exception) -> JSONResponse:
    return _error_response(request, 503, "device_fault", str(exc))


async def _handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("unhandled gateway error")
    return _error_response(request, 500, "internal_error", "internal error")


def _register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(RequestValidationError, _handle_validation_error)
    app.add_exception_handler(SensorSourceError, _handle_validation_error)

    app.add_exception_handler(PumpConflictError, _handle_conflict)
    app.add_exception_handler(SensorConflictError, _handle_conflict)
    app.add_exception_handler(SessionConflictError, _handle_conflict)
    app.add_exception_handler(SessionNotActiveError, _handle_conflict)
    app.add_exception_handler(InsufficientSpaceError, _handle_conflict)

    app.add_exception_handler(PumpFaultError, _handle_device_fault)
    app.add_exception_handler(SessionIoError, _handle_device_fault)

    app.add_exception_handler(Exception, _handle_unexpected)


# ─────────────────────────── WebSocket /ws/status ───────────────────────────

def _build_status_message(sequence: int, status: GatewayStatus) -> Optional[str]:
    """직렬화하고 16 KiB 상한을 넘으면 보내지 않는다(로그만 남긴다)."""
    payload = StatusMessage(sequence=sequence, data=status).model_dump_json()
    if len(payload.encode("utf-8")) > MAX_WS_MESSAGE_BYTES:
        logger.warning("status message seq=%d exceeds %d bytes; dropping",
                       sequence, MAX_WS_MESSAGE_BYTES)
        return None
    return payload


async def _send_status(websocket: WebSocket, sequence: int, status: GatewayStatus) -> None:
    payload = _build_status_message(sequence, status)
    if payload is not None:
        await websocket.send_text(payload)


async def _publish_status_loop(websocket: WebSocket, runtime: GatewayRuntime) -> None:
    """연결 직후 전체 상태를 낸 뒤, 최대 10Hz로 계속 발행한다."""
    try:
        await _send_status(websocket, runtime.next_sequence(), await runtime.status())
        async for status in runtime.subscribe():
            await _send_status(websocket, runtime.next_sequence(), status)
    except (WebSocketDisconnect, RuntimeError):
        pass


def _register_websocket(app: FastAPI) -> None:
    @app.websocket("/ws/status")
    async def ws_status(websocket: WebSocket) -> None:
        runtime: GatewayRuntime = websocket.app.state.runtime
        await websocket.accept()
        publish_task = asyncio.create_task(_publish_status_loop(websocket, runtime))
        try:
            while True:
                await websocket.receive_text()
        except WebSocketDisconnect:
            pass
        finally:
            publish_task.cancel()
            with suppress(asyncio.CancelledError):
                await publish_task


# ─────────────────────────── 앱 조립 ───────────────────────────

def create_app(config: GatewayConfig) -> FastAPI:
    runtime = GatewayRuntime(config)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        await runtime.startup()
        try:
            yield
        finally:
            await runtime.shutdown()

    app = FastAPI(title="SpaceBio Gateway", lifespan=lifespan)
    app.state.runtime = runtime
    app.add_middleware(BodySizeLimitMiddleware)
    _register_exception_handlers(app)
    app.include_router(router)
    _register_websocket(app)
    return app


def _default_config_path() -> Path:
    return Path(__file__).resolve().parents[1] / "config.yaml"


_default_config = load_config(_default_config_path())
app = create_app(_default_config)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=_default_config.host, port=_default_config.port)
