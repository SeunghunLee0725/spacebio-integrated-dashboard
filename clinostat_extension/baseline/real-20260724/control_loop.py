"""
속도 함수 엔진 + 제어 루프
- 각 축에 독립적인 속도 함수 적용 (또는 논문 전략)
- 50ms 주기로 모터 명령 전송
- WebSocket 브로드캐스트용 데이터 생성
- 매 틱마다 g_cmd (모터각 기반) 와 g_meas (IMU 기반) 를 메트릭에 push
"""

import math
import time
import threading
from enum import Enum
from dataclasses import dataclass, field

from modbus_rtu import ACServo, BLDCMotor
from sensor import WitMotionSensor
from metrics import GravityMetrics, gravity_vector_from_angles, normalised_imu
from strategies import Strategy, RAD_S_TO_RPM


class FunctionType(str, Enum):
    CONSTANT = "constant"
    SINE = "sine"
    TRIANGLE = "triangle"
    SQUARE = "square"
    SAWTOOTH = "sawtooth"


@dataclass
class SpeedFunctionParams:
    enabled: bool = True
    func_type: FunctionType = FunctionType.CONSTANT
    base_speed: float = 500.0   # rpm
    amplitude: float = 300.0    # rpm
    period: float = 10.0        # seconds

    def calculate(self, t: float) -> float:
        if not self.enabled:
            return 0.0
        if self.period <= 0:
            return self.base_speed

        phase = (t % self.period) / self.period  # 0~1

        if self.func_type == FunctionType.SINE:
            func_val = math.sin(2.0 * math.pi * phase)
        elif self.func_type == FunctionType.TRIANGLE:
            func_val = (4.0 * phase - 1.0) if phase < 0.5 else (3.0 - 4.0 * phase)
        elif self.func_type == FunctionType.SQUARE:
            func_val = 1.0 if phase < 0.5 else -1.0
        elif self.func_type == FunctionType.SAWTOOTH:
            func_val = 2.0 * phase - 1.0
        else:  # CONSTANT
            func_val = 0.0

        speed = self.base_speed + self.amplitude * func_val
        return speed

    def to_dict(self) -> dict:
        return {
            "enabled": self.enabled,
            "func_type": self.func_type.value,
            "base_speed": self.base_speed,
            "amplitude": self.amplitude,
            "period": self.period,
        }


@dataclass
class ControlState:
    """실시간 상태 (WebSocket으로 브로드캐스트). RPM은 출력축(감속기 후) 기준."""
    running: bool = False
    elapsed: float = 0.0
    outer_rpm: float = 0.0          # 목표 (output축 rpm, signed)
    inner_rpm: float = 0.0          # 목표 (output축 rpm, signed)
    outer_actual: float = 0.0       # 실측 (output축 rpm)
    inner_actual: float = 0.0       # 실측 (output축 rpm)
    outer_angle: float = 0.0        # 누적 각도 (출력축, degree) — signed integration
    inner_angle: float = 0.0        # 누적 각도 (출력축, degree)
    sensor: dict = field(default_factory=dict)
    strategy_name: str = ""         # 현재 전략 이름 (또는 "" = 속도함수 모드)
    strategy_params: dict = field(default_factory=dict)
    metrics_cmd: dict = field(default_factory=dict)
    metrics_meas: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "running": self.running,
            "elapsed": round(self.elapsed, 2),
            "outer_rpm": round(self.outer_rpm, 1),
            "inner_rpm": round(self.inner_rpm, 1),
            "outer_actual": round(self.outer_actual, 1),
            "inner_actual": round(self.inner_actual, 1),
            "outer_angle": round(self.outer_angle, 2),
            "inner_angle": round(self.inner_angle, 2),
            "sensor": self.sensor,
            "strategy_name": self.strategy_name,
            "strategy_params": self.strategy_params,
            "metrics_cmd": self.metrics_cmd,
            "metrics_meas": self.metrics_meas,
        }


# 감속비 (감속기 1차 출력축 기준)
AC_GEAR_RATIO = 200   # AC Servo: 실측 체인비 ~200 (iter781 0.25rad/s, iter837 0.10rad/s accel위상 physical/cmd=0.497/0.503 @ratio100 → 고정 ½, iter837에서 100→200 보정)
BLDC_GEAR_RATIO = 20  # BLDC: 모터축 1회전 → 출력축 1/20 회전
# PR0 속도운전(0x6200=2) 정상화 후 외축은 모터 RPM 명령을 1:1로 추종한다.
# (이전 0.5 보정은 0x6200=3 원점복귀 오설정으로 속도가 ~200rpm에 고정됐을 때의
#  임시방편이었다. 모드 수정 후 실측에서 명령=실제가 ±1% 이내로 일치해 1.0으로 복원.)
AC_COMMAND_SCALE = 1.0
OUTER_MAX_OUTPUT_RPM = 60.0
INNER_MAX_OUTPUT_RPM = 200.0
OUTER_MAX_SLEW_RPM_S = 50.0
INNER_MAX_SLEW_RPM_S = 50.0
FEEDBACK_POLL_INTERVAL = 0.5
# 속도운전 모드에서 PrB.06 피드백 부호는 명령 부호와 일치한다(실측 확인:
# +300 명령→+300 피드백, -300 명령→-300 피드백). 원점복귀 오설정 때의 -1.0에서 복원.
AC_FEEDBACK_SIGN = 1.0


class ControlLoop:
    INTERVAL = 0.05  # 50ms = 20Hz

    def __init__(
        self,
        ac_servo: ACServo,
        bldc_motor: BLDCMotor,
        sensor: WitMotionSensor,
    ):
        self.ac = ac_servo
        self.bldc = bldc_motor
        self.sensor = sensor

        self.outer_func = SpeedFunctionParams()
        self.inner_func = SpeedFunctionParams()
        self.strategy: Strategy | None = None   # None ⇒ use outer_func/inner_func

        self.state = ControlState()
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._sample_hooks: set = set()
        self._sample_hooks_lock = threading.Lock()
        self._last_ac_feedback_raw: int | None = None
        self._last_ac_feedback_output_rpm: float | None = None
        self._last_ac_feedback_valid = False
        self._last_bldc_feedback_raw: int | None = None

        # 실시간 메트릭 — 두 줄 운영
        # cmd : 모터 명령각 (inner_angle, outer_angle) 으로 계산한 g_cmd
        # meas : IMU 가속도 정규화한 g_meas
        self.metrics_cmd = GravityMetrics(sphere_buffer_max=50000, sphere_decimate=20)
        self.metrics_meas = GravityMetrics(sphere_buffer_max=50000, sphere_decimate=20)
        self._metrics_lock = threading.Lock()
        self._run_start_t: float = 0.0

        # status polling thread (항상 동작, control loop 비활성 중에도 actual RPM/센서 업데이트)
        self._status_thread = threading.Thread(target=self._status_poll, daemon=True)
        self._status_thread.start()

    def _status_poll(self):
        # 제어 루프 비활성 중에는 모터가 정지 상태 → actual=0 유지.
        # AC servo (EL7-RS400P)는 actual rpm 피드백 레지스터 미확정이라
        # 운영 중에는 setpoint를 그대로 actual 로 사용 (C# 원본과 동일).
        # BLDC는 정상적으로 actual을 읽지만, 제어 루프 안에서만 갱신.
        while True:
            try:
                if not self.running:
                    self.state.outer_actual = 0.0
                    self.state.inner_actual = 0.0
                    if self.sensor.connected:
                        self.state.sensor = self.sensor.get_data()
            except Exception:
                pass
            time.sleep(0.2)

    @property
    def running(self) -> bool:
        return self.state.running

    def start(self):
        if self.running:
            return

        # 모터 Enable
        if self.ac.connected:
            self.ac.enable()
        if self.bldc.connected:
            self.bldc.s_on()

        time.sleep(0.2)

        # 각도 누적 + 메트릭 리셋
        self.state.outer_angle = 0.0
        self.state.inner_angle = 0.0
        with self._metrics_lock:
            self.metrics_cmd.reset()
            self.metrics_meas.reset()
        self._run_start_t = time.monotonic()

        # 전략이 있으면 리셋
        if self.strategy is not None:
            self.strategy.reset()
            self.state.strategy_name = self.strategy.name
        else:
            self.state.strategy_name = ""

        self.state.running = True
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self.state.running = False
        self._stop_event.set()

        if self._thread:
            self._thread.join(timeout=2.0)
            self._thread = None

        # 모터 정지 + S-OFF
        if self.ac.connected:
            self.ac.emergency_stop()
            self.ac.disable()
        if self.bldc.connected:
            self.bldc.stop()
            time.sleep(0.3)
            self.bldc.s_off()

        self.state.outer_rpm = 0
        self.state.inner_rpm = 0
        self.state.outer_actual = 0
        self.state.inner_actual = 0
        self._last_ac_feedback_raw = 0
        self._last_ac_feedback_output_rpm = 0.0
        self._last_ac_feedback_valid = False
        self._last_bldc_feedback_raw = 0

    # ─── 전략 / 메트릭 헬퍼 ─────────────────────────────────────────────────
    def set_strategy(self, strategy: Strategy | None) -> None:
        """Replace the active strategy (None → fall back to outer_func/inner_func)."""
        was_running = self.running
        if was_running:
            self.stop()
        self.strategy = strategy
        self.state.strategy_name = strategy.name if strategy is not None else ""
        if strategy is None:
            self.state.strategy_params = {}
        if was_running:
            self.start()

    def set_strategy_params(self, params: dict | None) -> None:
        self.state.strategy_params = dict(params or {})

    @staticmethod
    def _apply_func_config(params: SpeedFunctionParams, cfg) -> None:
        params.enabled = getattr(cfg, "enabled", True)
        params.func_type = FunctionType(cfg.func_type)
        params.base_speed = cfg.base_speed
        params.amplitude = cfg.amplitude
        params.period = cfg.period

    def apply_speed_config(self, outer_cfg, inner_cfg) -> None:
        self._apply_func_config(self.outer_func, outer_cfg)
        self._apply_func_config(self.inner_func, inner_cfg)

    def speed_config_snapshot(self) -> dict:
        return {
            "outer": self.outer_func.to_dict(),
            "inner": self.inner_func.to_dict(),
        }

    def feedback_snapshot(self) -> dict:
        return {
            "outer_actual_source": "drive_feedback" if self._last_ac_feedback_valid else "command_estimate",
            "outer_feedback_raw_motor_rpm": self._last_ac_feedback_raw,
            "outer_feedback_output_rpm": self._last_ac_feedback_output_rpm,
            "outer_feedback_valid": self._last_ac_feedback_valid,
            "inner_actual_source": "drive_feedback" if self._last_bldc_feedback_raw is not None else "command_estimate",
            "inner_feedback_raw_motor_rpm": self._last_bldc_feedback_raw,
        }

    @staticmethod
    def _ac_feedback_output_rpm(raw_motor_rpm: int) -> float:
        return AC_FEEDBACK_SIGN * raw_motor_rpm / AC_GEAR_RATIO

    @staticmethod
    def _ac_command_motor_rpm(output_rpm: float) -> int:
        motor_rpm = output_rpm * AC_GEAR_RATIO * AC_COMMAND_SCALE
        return int(max(-6000.0, min(motor_rpm, 6000.0)))

    def add_sample_hook(self, callback) -> None:
        """Register a callback receiving one dict sample per control tick."""
        with self._sample_hooks_lock:
            self._sample_hooks.add(callback)

    def remove_sample_hook(self, callback) -> None:
        with self._sample_hooks_lock:
            self._sample_hooks.discard(callback)

    def _emit_sample(self, sample: dict) -> None:
        with self._sample_hooks_lock:
            hooks = list(self._sample_hooks)
        for hook in hooks:
            try:
                hook(sample)
            except Exception:
                pass

    def get_sphere_points(self, source: str = "cmd", max_points: int = 800) -> list:
        with self._metrics_lock:
            m = self.metrics_cmd if source == "cmd" else self.metrics_meas
            return m.sphere_points(max_points=max_points)

    def metrics_run_summary(self) -> dict:
        """Full snapshot incl. all bands and settled time — used at run end."""
        target_gravity_G = self._target_gravity_G()
        with self._metrics_lock:
            return {
                "cmd": self.metrics_cmd.snapshot(
                    include_sphere=False,
                    target_gravity_G=target_gravity_G,
                ),
                "meas": self.metrics_meas.snapshot(
                    include_sphere=False,
                    target_gravity_G=target_gravity_G,
                ),
                "strategy": self.state.strategy_name,
                "target_gravity_G": target_gravity_G,
            }

    def _target_gravity_G(self) -> float:
        try:
            target = float((self.state.strategy_params or {}).get("target_gravity_G") or 0.0)
        except (TypeError, ValueError):
            return 0.0
        return target if target > 0.0 else 0.0

    @staticmethod
    def _slew_limit(target: float, current: float, max_rate_rpm_s: float, dt: float) -> float:
        if dt <= 0.0 or max_rate_rpm_s <= 0.0:
            return target
        max_delta = max_rate_rpm_s * dt
        if target > current + max_delta:
            return current + max_delta
        if target < current - max_delta:
            return current - max_delta
        return target

    def _loop(self):
        t0 = time.monotonic()
        last_t = t0
        next_tick_t = t0 + self.INTERVAL

        # Commanded-angle integration (for paper-fidelity g_cmd metric)
        theta_cmd_rad = 0.0
        phi_cmd_rad = 0.0
        theta_exec_rad = 0.0
        phi_exec_rad = 0.0
        outer_cmd_rpm = 0.0
        inner_cmd_rpm = 0.0
        ac_estopped = False
        rpm_to_rad_s = 2.0 * math.pi / 60.0
        next_feedback_poll_t = 0.0
        inner_actual_cached = 0.0

        while not self._stop_event.is_set():
            now = time.monotonic()
            t = now - t0
            dt = now - last_t
            last_t = now

            # 속도 계산 — 전략 우선, 없으면 속도함수
            if self.strategy is not None:
                # Strategy feedback must use the angle actually executed by the mechanism.
                outer_target_rpm, inner_target_rpm = self.strategy.step(t, theta_exec_rad)
            else:
                outer_target_rpm = self.outer_func.calculate(t)
                inner_target_rpm = self.inner_func.calculate(t)

            outer_cmd_rpm = self._slew_limit(
                outer_target_rpm, outer_cmd_rpm, OUTER_MAX_SLEW_RPM_S, dt
            )
            inner_cmd_rpm = self._slew_limit(
                inner_target_rpm, inner_cmd_rpm, INNER_MAX_SLEW_RPM_S, dt
            )
            outer_cmd_rpm = max(-OUTER_MAX_OUTPUT_RPM, min(outer_cmd_rpm, OUTER_MAX_OUTPUT_RPM))
            inner_cmd_rpm = max(-INNER_MAX_OUTPUT_RPM, min(inner_cmd_rpm, INNER_MAX_OUTPUT_RPM))

            ac_motor_rpm = self._ac_command_motor_rpm(outer_cmd_rpm)
            bldc_motor_rpm = int(max(-4000.0, min(inner_cmd_rpm * BLDC_GEAR_RATIO, 4000.0)))

            # 모터 명령 전송
            try:
                if self.ac.connected:
                    if ac_motor_rpm == 0:
                        if not ac_estopped:
                            self.ac.emergency_stop()
                            ac_estopped = True
                    else:
                        if ac_estopped:
                            self.ac.enable()
                            ac_estopped = False
                        self.ac.set_velocity(ac_motor_rpm, 100, 100)
            except Exception:
                pass

            try:
                if self.bldc.connected:
                    self.bldc.set_velocity(bldc_motor_rpm, 100, 100)
            except Exception:
                pass

            # 실기 적용 후 명령각 적분 (signed) — g_cmd 메트릭용
            theta_cmd_rad += inner_cmd_rpm * rpm_to_rad_s * dt
            phi_cmd_rad   += outer_cmd_rpm * rpm_to_rad_s * dt

            # 실측 RPM. AC Servo PrB.06은 모터축 RPM(부호=명령 부호)이라
            # 감속비로 나눠 출력축 RPM으로 변환해 폐루프 검증에 사용한다.
            # BLDC 피드백 read는 50 ms tick을 막지 않도록 저주기 캐시한다.
            outer_actual = outer_cmd_rpm
            outer_actual_source = "command_estimate"
            inner_actual = inner_actual_cached if self.bldc.connected else inner_cmd_rpm
            if t >= next_feedback_poll_t:
                next_feedback_poll_t = t + FEEDBACK_POLL_INTERVAL
                try:
                    if self.ac.connected:
                        ar = self.ac.get_actual_rpm()
                        if ar is not None:
                            self._last_ac_feedback_raw = ar
                            ac_feedback_output_rpm = self._ac_feedback_output_rpm(ar)
                            self._last_ac_feedback_output_rpm = ac_feedback_output_rpm
                            self._last_ac_feedback_valid = True
                except Exception:
                    pass
                try:
                    if self.bldc.connected:
                        br = self.bldc.get_actual_rpm()
                        if br is not None:
                            self._last_bldc_feedback_raw = br
                            inner_actual_cached = br / BLDC_GEAR_RATIO
                            inner_actual = inner_actual_cached
                except Exception:
                    pass

            if self._last_ac_feedback_valid and self._last_ac_feedback_output_rpm is not None:
                outer_actual = self._last_ac_feedback_output_rpm
                outer_actual_source = "drive_feedback"

            self.state.outer_actual = outer_actual
            self.state.inner_actual = inner_actual

            # 각도 적분 (실측 RPM) — 전략 피드백/표시용
            phi_exec_rad += outer_actual * rpm_to_rad_s * dt
            theta_exec_rad += inner_actual * rpm_to_rad_s * dt
            self.state.outer_angle = math.degrees(phi_exec_rad)
            self.state.inner_angle = math.degrees(theta_exec_rad)

            # 메트릭 push — 매 틱
            gx, gy, gz = gravity_vector_from_angles(theta_cmd_rad, phi_cmd_rad)
            sensor_data = self.sensor.get_data() if self.sensor.connected else {}
            g_meas = None
            target_gravity_G = self._target_gravity_G()
            with self._metrics_lock:
                self.metrics_cmd.push(gx, gy, gz, t)
                if sensor_data:
                    mx, my, mz = normalised_imu(
                        sensor_data.get("ax", 0.0),
                        sensor_data.get("ay", 0.0),
                        sensor_data.get("az", 0.0),
                    )
                    if (mx, my, mz) != (0.0, 0.0, 0.0):
                        g_meas = (mx, my, mz)
                        self.metrics_meas.push(mx, my, mz, t)
                # WS 브로드캐스트용 스냅샷 (sphere 제외 — 별도 엔드포인트로)
                self.state.metrics_cmd = self.metrics_cmd.snapshot(
                    include_sphere=False,
                    target_gravity_G=target_gravity_G,
                )
                if sensor_data:
                    self.state.metrics_meas = self.metrics_meas.snapshot(
                        include_sphere=False,
                        target_gravity_G=target_gravity_G,
                    )
                else:
                    self.state.metrics_meas = {}

            if self.strategy is not None and hasattr(self.strategy, "observe_gravity_vector"):
                try:
                    self.strategy.observe_gravity_vector(
                        t,
                        g_meas=g_meas,
                        g_cmd=(gx, gy, gz),
                        current_phi_rad=phi_cmd_rad,
                    )
                except Exception:
                    pass
            strategy_debug = {}
            if self.strategy is not None and hasattr(self.strategy, "debug_state"):
                try:
                    strategy_debug = self.strategy.debug_state()
                except Exception:
                    strategy_debug = {}

            # 상태 업데이트 (signed RPM 그대로 보고)
            self.state.elapsed = t
            self.state.outer_rpm = outer_cmd_rpm
            self.state.inner_rpm = inner_cmd_rpm
            self.state.sensor = sensor_data

            self._emit_sample({
                "wall_time": time.time(),
                "elapsed": t,
                "dt": dt,
                "running": self.state.running,
                "ac_connected": self.ac.connected,
                "bldc_connected": self.bldc.connected,
                "sensor_connected": self.sensor.connected,
                "outer_rpm": outer_cmd_rpm,
                "inner_rpm": inner_cmd_rpm,
                "outer_actual": outer_actual,
                "inner_actual": inner_actual,
                "outer_actual_source": outer_actual_source,
                "inner_actual_source": "drive_feedback" if self._last_bldc_feedback_raw is not None else "command_estimate",
                "outer_feedback_raw_motor_rpm": self._last_ac_feedback_raw,
                "outer_feedback_output_rpm": self._last_ac_feedback_output_rpm,
                "outer_feedback_valid": self._last_ac_feedback_valid,
                "inner_feedback_raw_motor_rpm": self._last_bldc_feedback_raw,
                "outer_tracking_error_rpm": outer_actual - outer_cmd_rpm,
                "inner_tracking_error_rpm": inner_actual - inner_cmd_rpm,
                "outer_angle": self.state.outer_angle,
                "inner_angle": self.state.inner_angle,
                "sensor": sensor_data,
                "strategy_name": self.state.strategy_name,
                "strategy_params": self.state.strategy_params,
                "strategy_debug": strategy_debug,
                "metrics_cmd": self.state.metrics_cmd,
                "metrics_meas": self.state.metrics_meas,
                "g_cmd": (gx, gy, gz),
                "g_meas": g_meas,
            })

            # 다음 틱까지 대기. 작업 시간이 짧으면 20 Hz에 맞추고,
            # 통신 지연으로 뒤처지면 추가 대기 없이 다음 tick으로 넘어간다.
            sleep_s = next_tick_t - time.monotonic()
            if sleep_s > 0:
                self._stop_event.wait(sleep_s)
                next_tick_t += self.INTERVAL
            else:
                next_tick_t = time.monotonic() + self.INTERVAL
