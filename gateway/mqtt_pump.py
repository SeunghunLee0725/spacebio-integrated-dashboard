"""무선 실기 펌프 백엔드 — ESP32-C6 보드를 MQTT로 제어 (실기 연동).

보드 펌웨어는 오직 MQTT로만 명령을 받는다(USB는 로그 전용). 명령은
`s25007/board1/cmd`로 발행하고 상태는 `s25007/board1/status`를 구독한다.

    상태: ip=..,rssi=..,run=on/off,mode=IDLE|SPINUP|RUN|MANUAL,current_spm=..,
          spin_rem=..,manual_rem=..,spinsteps=..,spinspm=..,spm=..,pos=..

⚠ 보드에는 비상정지 하드웨어가 없다. 그래서 estop은 **게이트웨이 측 소프트웨어
래치**로 구현한다 — 래치 중에는 명령 발행을 막고, 안전을 위해 정지 명령은 낸다.

⚠ µL 보정이 없으므로 부피 명령(dispense)은 지원하지 않는다. 스텝만 노출한다.

paho 클라이언트는 자체 네트워크 스레드에서 콜백을 돌린다. 최신 상태는 스레드
안전하게 보관하고 읽기만 한다. 테스트는 `client_factory`로 가짜 클라이언트를
주입해 브로커 없이 검증한다.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Callable, Optional

from gateway.api_models import (
    PUMP_MODE_WIRELESS,
    PumpEmergencyStopRequest,
    PumpResetEmergencyStopRequest,
    PumpState,
    PumpStatus,
    PumpStepRequest,
    PumpStopRequest,
)
from gateway.simulated_pump import ResetEmergencyStopResult

logger = logging.getLogger("gateway.mqtt_pump")

CMD_TOPIC = "s25007/board1/cmd"
STATUS_TOPIC = "s25007/board1/status"


class PumpConflictError(Exception):
    """simulated_pump와 동일한 의미 — 라우트가 409로 매핑한다."""

    def __init__(self, message: str, *, code: str = "pump_conflict") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class BoardStatus:
    run: bool
    mode: str
    spm: int
    position_steps: int
    online: bool = True


def parse_board_status(payload: str) -> Optional[BoardStatus]:
    """`k=v,k=v` 상태 라인을 파싱한다. 필수 키가 없으면 None."""
    fields = {}
    for part in payload.strip().split(","):
        if "=" in part:
            k, v = part.split("=", 1)
            fields[k.strip()] = v.strip()
    if "run" not in fields or "pos" not in fields:
        return None
    try:
        return BoardStatus(
            run=fields.get("run") == "on",
            mode=fields.get("mode", "IDLE"),
            spm=int(fields.get("spm", "0")),
            position_steps=int(fields["pos"]),
        )
    except ValueError:
        return None


class MqttPump:
    """무선 펌프 보드의 게이트웨이 측 대리자."""

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 1883,
        username: Optional[str] = None,
        password: Optional[str] = None,
        client_factory: Optional[Callable[[], object]] = None,
    ) -> None:
        self._host = host
        self._port = port
        self._username = username
        self._password = password
        self._client_factory = client_factory
        self._client = None

        self._guard = threading.Lock()
        self._board: Optional[BoardStatus] = None
        self._estop_latched = False
        self._session_start_pos: Optional[int] = None

    # ─────────────────────────── 연결 ───────────────────────────

    def connect(self) -> None:
        self._client = self._make_client()
        self._client.on_message = self._on_message
        try:
            self._client.connect(self._host, self._port)
            self._client.subscribe(STATUS_TOPIC)
            self._client.loop_start()
        except Exception:  # noqa: BLE001 — 브로커 부재가 게이트웨이 기동을 막으면 안 된다
            logger.exception("mqtt pump connect failed")

    def _make_client(self):
        if self._client_factory is not None:
            return self._client_factory()
        import paho.mqtt.client as mqtt

        try:
            client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        except (AttributeError, TypeError):  # pragma: no cover — paho v1 호환
            client = mqtt.Client()
        if self._username is not None:
            client.username_pw_set(self._username, self._password)
        return client

    def disconnect(self) -> None:
        if self._client is not None:
            try:
                self._client.loop_stop()
                self._client.disconnect()
            except Exception:  # noqa: BLE001
                logger.warning("mqtt pump disconnect failed")

    def _on_message(self, _client, _userdata, message) -> None:
        payload = message.payload.decode("utf-8", errors="replace")
        board = parse_board_status(payload)
        if board is not None:
            with self._guard:
                self._board = board

    def feed_status(self, payload: str) -> None:
        """테스트·주입용 — 상태 라인을 직접 넣는다."""
        board = parse_board_status(payload)
        if board is not None:
            with self._guard:
                self._board = board

    def _publish(self, command: str) -> None:
        if self._client is None:
            raise PumpConflictError("pump broker not connected", code="pump_offline")
        self._client.publish(CMD_TOPIC, command)

    # ─────────────────────────── 세션 ───────────────────────────

    async def begin_session(self) -> None:
        with self._guard:
            self._session_start_pos = self._board.position_steps if self._board else 0

    async def end_session(self) -> None:
        with self._guard:
            self._session_start_pos = None

    # ─────────────────────────── 조회 ───────────────────────────

    async def status(self) -> PumpStatus:
        with self._guard:
            board = self._board
            latched = self._estop_latched
        # 래치는 보드 텔레메트리보다 우선한다 — 상태를 아직 못 받았어도 래치돼
        # 있으면 반드시 emergency_stopped를 보고해야 안전하다.
        if latched:
            base = PumpStatus(mode=PUMP_MODE_WIRELESS,
                              state=PumpState.EMERGENCY_STOPPED, estop_latched=True)
            if board is None:
                return base
            return base.model_copy(update={
                "position_steps": board.position_steps, "spm": board.spm})
        if board is None:
            return PumpStatus(mode=PUMP_MODE_WIRELESS, state=PumpState.IDLE)
        state = PumpState.RUNNING if board.run else PumpState.IDLE
        return PumpStatus(
            mode=PUMP_MODE_WIRELESS, state=state, estop_latched=latched,
            position_steps=board.position_steps, spm=board.spm,
        )

    # ─────────────────────────── 명령 ───────────────────────────

    async def step(self, request: PumpStepRequest) -> PumpStatus:
        with self._guard:
            if self._estop_latched:
                raise PumpConflictError(
                    "pump is emergency-stop latched", code="pump_estop_latched")
        self._publish(f"spm:{request.spm}")
        self._publish(f"manualstep:{request.steps}")
        return await self.status()

    async def stop(self, request: PumpStopRequest) -> PumpStatus:
        del request
        self._publish("stop")          # 정지는 래치 여부와 무관하게 항상 허용
        return await self.status()

    async def emergency_stop(self, request: PumpEmergencyStopRequest) -> PumpStatus:
        del request
        self._publish("stop")          # 하드웨어 정지를 먼저 낸다
        with self._guard:
            self._estop_latched = True
        return await self.status()

    async def reset_emergency_stop(
        self, request: PumpResetEmergencyStopRequest
    ) -> ResetEmergencyStopResult:
        del request
        with self._guard:
            previous = (PumpState.EMERGENCY_STOPPED if self._estop_latched
                        else PumpState.IDLE)
            self._estop_latched = False
        return ResetEmergencyStopResult(
            previous_state=previous, state=PumpState.IDLE,
            estop_latched=False, accepted=True,
        )
