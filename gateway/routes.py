"""REST 라우트 — 검증·위임·envelope 포장만 한다 (설계 스펙 6장).

실제 상태 전이는 전부 `gateway.runtime.GatewayRuntime`이 한다. 여기서는 요청
본문을 pydantic 모델로 받고(검증 실패는 FastAPI가 422로 처리), 런타임에
위임한 뒤 결과를 `success_envelope`로 감싼다.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Optional

from fastapi import APIRouter, Depends, Request
from starlette.responses import StreamingResponse

from gateway.api_models import (
    PumpDispenseRequest,
    PumpStepRequest,
    PumpEmergencyStopRequest,
    PumpResetEmergencyStopRequest,
    PumpStopRequest,
    SensorConfigureRequest,
    SensorStartRequest,
    SensorStopRequest,
    SessionFinishRequest,
    SessionStartRequest,
    SessionUpdateRequest,
)
from gateway.http_support import envelope
from gateway.runtime import GatewayRuntime
from gateway.session_archive import archive_filename

router = APIRouter()

#: 아카이브를 브라우저로 흘려보내는 단위. 전체를 메모리에 올리지 않는다.
DOWNLOAD_CHUNK_BYTES = 64 * 1024


def get_runtime(request: Request) -> GatewayRuntime:
    return request.app.state.runtime


# ─────────────────────────── 상태 조회 ───────────────────────────

@router.get("/health")
async def health(request: Request, runtime: GatewayRuntime = Depends(get_runtime)):
    data = {"status": "ok", "uptime_s": runtime.uptime_s()}
    return envelope(request, data)


@router.get("/api/status")
async def get_status(request: Request, runtime: GatewayRuntime = Depends(get_runtime)):
    return envelope(request, await runtime.status())


@router.get("/api/sensor/status")
async def get_sensor_status(request: Request, runtime: GatewayRuntime = Depends(get_runtime)):
    status = await runtime.status()
    return envelope(request, status.sensor)


@router.get("/api/pump/status")
async def get_pump_status(request: Request, runtime: GatewayRuntime = Depends(get_runtime)):
    status = await runtime.status()
    return envelope(request, status.pump)


@router.get("/api/session/status")
async def get_session_status(
    request: Request,
    session_id: Optional[str] = None,
    request_id: Optional[str] = None,
    runtime: GatewayRuntime = Depends(get_runtime),
):
    """`session_id`/`request_id` 쿼리로 reconciliation 조회를 지원한다."""
    status = await runtime.session_status_for_request(
        session_id=session_id, request_id=request_id,
    )
    return envelope(request, status)


# ─────────────────────────── 센서 ───────────────────────────

@router.post("/api/sensor/configure")
async def configure_sensor(
    request: Request, body: SensorConfigureRequest,
    runtime: GatewayRuntime = Depends(get_runtime),
):
    status = await runtime.configure_sensor(body)
    return envelope(request, status)


@router.post("/api/sensor/start")
async def start_sensor(
    request: Request, body: SensorStartRequest,
    runtime: GatewayRuntime = Depends(get_runtime),
):
    status = await runtime.start_sensor(body.request_id)
    return envelope(request, status)


@router.post("/api/sensor/stop")
async def stop_sensor(
    request: Request, body: SensorStopRequest,
    runtime: GatewayRuntime = Depends(get_runtime),
):
    status = await runtime.stop_sensor(body.request_id)
    return envelope(request, status)


@router.get("/api/sensor/datasets")
async def list_datasets(request: Request, runtime: GatewayRuntime = Depends(get_runtime)):
    """CSV 재생 dataset 목록 — 화면 드롭다운이 이걸로 채워진다."""
    return envelope(request, {"datasets": runtime.list_datasets()})


# ─────────────────────────── 펌프 ───────────────────────────

@router.post("/api/pump/dispense")
async def pump_dispense(
    request: Request, body: PumpDispenseRequest,
    runtime: GatewayRuntime = Depends(get_runtime),
):
    return envelope(request, await runtime.pump_command(body))


@router.post("/api/pump/step")
async def pump_step(
    request: Request, body: PumpStepRequest,
    runtime: GatewayRuntime = Depends(get_runtime),
):
    """실기 무선 펌프의 스텝 명령. 모의 백엔드면 409."""
    return envelope(request, await runtime.pump_step(body))


@router.post("/api/pump/stop")
async def pump_stop(
    request: Request, body: PumpStopRequest,
    runtime: GatewayRuntime = Depends(get_runtime),
):
    return envelope(request, await runtime.pump_command(body))


@router.post("/api/pump/emergency-stop")
async def pump_emergency_stop(
    request: Request, body: PumpEmergencyStopRequest,
    runtime: GatewayRuntime = Depends(get_runtime),
):
    return envelope(request, await runtime.pump_command(body))


@router.post("/api/pump/reset-emergency-stop")
async def pump_reset_emergency_stop(
    request: Request, body: PumpResetEmergencyStopRequest,
    runtime: GatewayRuntime = Depends(get_runtime),
):
    return envelope(request, await runtime.pump_command(body))


# ─────────────────────────── 세션 ───────────────────────────

@router.post("/api/session/start")
async def session_start(
    request: Request, body: SessionStartRequest,
    runtime: GatewayRuntime = Depends(get_runtime),
):
    return envelope(request, await runtime.session_command(body))


@router.post("/api/session/update")
async def session_update(
    request: Request, body: SessionUpdateRequest,
    runtime: GatewayRuntime = Depends(get_runtime),
):
    return envelope(request, await runtime.session_command(body))


@router.post("/api/session/finish")
async def session_finish(
    request: Request, body: SessionFinishRequest,
    runtime: GatewayRuntime = Depends(get_runtime),
):
    return envelope(request, await runtime.session_command(body))


# ─────────────────────────── 기록 관리 ───────────────────────────
# 목록/삭제는 envelope를 쓰고, 다운로드만 파일 본문을 그대로 낸다.

@router.get("/api/sessions")
async def list_archived_sessions(
    request: Request, runtime: GatewayRuntime = Depends(get_runtime),
):
    sessions = [asdict(s) for s in runtime.list_archived_sessions()]
    return envelope(request, {"sessions": sessions})


@router.get("/api/sessions/{session_id}/download")
async def download_session(
    session_id: str, runtime: GatewayRuntime = Depends(get_runtime),
):
    """세션 폴더를 `.tar.gz`로 내보낸다. 기록 중이어도 받을 수 있다(읽기만 한다)."""
    stream, size = runtime.open_session_archive(session_id)
    filename = archive_filename(session_id)

    def _chunks():
        try:
            while chunk := stream.read(DOWNLOAD_CHUNK_BYTES):
                yield chunk
        finally:
            stream.close()

    return StreamingResponse(
        _chunks(),
        media_type="application/gzip",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Length": str(size),
        },
    )


@router.delete("/api/sessions/{session_id}")
async def delete_archived_session(
    request: Request, session_id: str, runtime: GatewayRuntime = Depends(get_runtime),
):
    """비가역이다. 기록 중인 세션은 409로 거부한다."""
    runtime.delete_archived_session(session_id)
    return envelope(request, {"session_id": session_id, "deleted": True})
