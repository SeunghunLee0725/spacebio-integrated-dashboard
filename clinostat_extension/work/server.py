"""
클리노스텟 웹 제어 서버
- FastAPI + WebSocket
- REST API: 연결/해제, 파라미터 설정
- WebSocket: 실시간 상태 브로드캐스트 (20Hz)
"""

import asyncio
import glob
import json
import math
from datetime import datetime
from pathlib import Path
from contextlib import asynccontextmanager
from typing import Any

import serial.tools.list_ports
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import BaseModel

from modbus_rtu import ACServo, BLDCMotor
from sensor import WitMotionSensor
from control_loop import ControlLoop
from strategies import RAD_S_TO_RPM, STRATEGY_REGISTRY, STRATEGY_PRESETS, make_strategy
from camera import camera
from experiment import ExperimentRunner
from optimizer import OptimizerRunner

# SpaceBio 통합 — 기존 클리노스텟 동작에 영향을 주지 않는 가산 확장 (DEC-012)
import httpx
from fastapi.responses import JSONResponse
from fastapi import Request
import spacebio_proxy
from spacebio_proxy import ProxyGatewayError, ProxyPathError

# === 하드웨어 인스턴스 ===
ac_servo = ACServo()
bldc_motor = BLDCMotor()
sensor = WitMotionSensor()
control = ControlLoop(ac_servo, bldc_motor, sensor)

# === WebSocket 클라이언트 관리 ===
ws_clients: set[WebSocket] = set()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Gateway로 가는 단일 HTTP 클라이언트. 요청마다 새로 만들면 연결 비용이
    # 제어 응답성에 영향을 준다.
    app.state.spacebio_client = httpx.AsyncClient(
        base_url=spacebio_proxy.GATEWAY_BASE_URL
    )
    app.state.spacebio = spacebio_proxy.SpaceBioProxy(app.state.spacebio_client)
    yield
    # 종료 시 정리
    # (하드웨어 정지가 항상 먼저다. SpaceBio 클라이언트는 그 뒤에 닫는다.)
    control.stop()
    ac_servo.disconnect()
    bldc_motor.disconnect()
    sensor.disconnect()
    camera.stop()
    await app.state.spacebio_client.aclose()


app = FastAPI(title="Clinostat Controller", lifespan=lifespan)

STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


def _safe_comports():
    try:
        return list(serial.tools.list_ports.comports())
    except Exception:
        ports = []
        for pattern in ("/dev/ttyUSB*", "/dev/ttyACM*", "/dev/ttyS*"):
            for path in sorted(glob.glob(pattern)):
                ports.append(type("PortInfo", (), {
                    "device": path,
                    "vid": None,
                    "pid": None,
                    "location": None,
                })())
        return ports


# === Pydantic 모델 ===

class PortConfig(BaseModel):
    port: str
    baudrate: int = 38400


class SensorPortConfig(BaseModel):
    port: str
    baudrate: int = 115200


class SpeedFuncConfig(BaseModel):
    enabled: bool = True
    func_type: str = "constant"
    base_speed: float = 500
    amplitude: float = 300
    period: float = 10


class ControlConfig(BaseModel):
    outer: SpeedFuncConfig
    inner: SpeedFuncConfig


class StrategyConfig(BaseModel):
    name: str          # e.g. "fibonacci_lattice_dwell"; "" or "none" clears
    params: dict[str, Any] = {}


class StrategyDryRunConfig(BaseModel):
    name: str = "ultra_high_tau_pole_guard"
    params: dict[str, Any] = {}
    duration_s: float = 180.0
    dt_s: float = 0.05
    tau_s: float = 0.325
    theta0_rad: float = 0.0
    sample_period_s: float = 10.0
    command_slew_rpm_s: float = 0.0
    inner_actual_gain: float = 1.0


class StrategyPreflightConfig(BaseModel):
    name: str = "ultra_high_tau_pole_guard"
    params: dict[str, Any] = {}
    duration_s: float = 600.0
    dt_s: float = 0.05
    tau_scenarios: list[float] = [0.325, 0.55, 0.60, 0.65, 0.70, 0.80]
    sample_period_s: float = 120.0
    guard_tau_s: float = 0.55
    tau_min_s: float = 0.20
    tau_max_s: float = 0.90
    tracking_rms_max_rpm: float = 6.0
    microgravity_tracking_rms_max_rpm: float = 7.0
    tracking_peak_max_rpm: float = 28.0
    brake_distance_max_deg: float = 25.0
    final_i5_max: float = 2.0
    hardware_startup_check: bool = True
    startup_duration_s: float = 600.0
    startup_tau_s: float = 0.50
    startup_command_slew_rpm_s: float = 50.0
    startup_inner_actual_gain: float = 3.0
    startup_tau_margin_s: float = 0.10
    hardware_short_check: bool = True
    short_duration_s: float = 180.0
    short_tau_s: float = 0.50
    short_command_slew_rpm_s: float = 50.0
    short_inner_actual_gain: float = 3.0
    short_final_i5_max: float = 3.4
    target_error_bias_G: float = 0.0


TARGET_ERROR_BIAS_G_BY_STRATEGY = {
    "directional_scheduled_taspg_moon": 0.01736620424279889,
    "directional_scheduled_taspg_mars": 0.09221670851048541,
}

MICROGRAVITY_STRATEGIES = {
    "adaptive_blend",
    "fibonacci_lattice_dwell",
}


class ExperimentRunConfig(BaseModel):
    strategy: str
    params: dict[str, Any] = {}
    duration_s: float = 60.0
    stop_motion_on_finish: bool = True
    preflight: dict[str, Any] = {}


class OptimizerRunConfig(BaseModel):
    strategy: str
    base_params: dict[str, Any] = {}
    param_ranges: dict[str, Any] = {}
    duration_s: float = 60.0
    max_trials: int = 5
    objective_source: str = "auto"  # auto | cmd | meas
    seed: int | None = None
    preflight_enabled: bool = True
    preflight: dict[str, Any] = {}


# 결과 덤프 저장 위치
RUNS_DIR = Path(__file__).parent / "runs"
RUNS_DIR.mkdir(exist_ok=True)
experiment_runner = ExperimentRunner(control, make_strategy, RUNS_DIR)
RESEARCH_STATUS_DIR = Path(__file__).parent / "research_status"


def _optimizer_preflight_runner(
    strategy_name: str,
    params: dict[str, Any],
    options: dict[str, Any],
) -> dict[str, Any]:
    """Return the compact gate result stored in optimizer trial status."""
    cfg_data = dict(options or {})
    cfg_data["name"] = strategy_name
    cfg_data["params"] = params
    try:
        gate = _run_strategy_preflight_gate(StrategyPreflightConfig(**cfg_data))
    except Exception as e:
        return {"passed": False, "error": str(e), "scenarios": []}
    failed = [
        {
            "tau_s": s.get("tau_s"),
            "checks": s.get("checks"),
            "summary": s.get("summary"),
        }
        for s in gate.get("scenarios", [])
        if not s.get("passed")
    ]
    return {
        "passed": bool(gate.get("passed")),
        "strategy": gate.get("strategy"),
        "duration_s": gate.get("duration_s"),
        "dt_s": gate.get("dt_s"),
        "thresholds": gate.get("thresholds"),
        "failed_scenarios": failed,
        "scenarios": gate.get("scenarios", []),
        "error": None if gate.get("passed") else "preflight realism gate failed",
    }


optimizer_runner = OptimizerRunner(
    experiment_runner,
    RUNS_DIR,
    preflight_runner=_optimizer_preflight_runner,
)


# === 페이지 ===

@app.get("/")
async def index():
    return FileResponse(str(STATIC_DIR / "index.html"))


@app.get("/m")
@app.get("/mobile")
async def mobile():
    return FileResponse(str(STATIC_DIR / "mobile.html"))


# === 시리얼 포트 목록 ===

@app.get("/api/ports")
async def list_ports():
    ports = sorted([p.device for p in _safe_comports()])
    return {"ports": ports}


@app.get("/api/ports/detect")
async def detect_ports():
    """USB VID/PID로 장치 자동 감지.
    - AC Servo: FTDI FT232 (0403:6001) RS-485 동글
    - BLDC: Oriental Motor BLVD-KRD (1aa2:1405) - 두 인터페이스 중 intf 0x02가 Modbus
    - 센서: WitMotion (1a86:7523) CH340
    """
    result = {"ac_servo": None, "bldc": None, "sensor": None}
    bldc_candidates = []  # (interface_num, device_path)

    for p in _safe_comports():
        if p.vid is None or p.pid is None:
            continue
        vid, pid = p.vid, p.pid
        if (vid, pid) == (0x0403, 0x6001):
            result["ac_servo"] = p.device
        elif (vid, pid) == (0x1aa2, 0x1405):
            # BLDC 두 인터페이스 중 intf 0x02 우선
            intf_num = None
            try:
                # location 예: "3-1.4.3:1.2" → 마지막 ".N" = interface
                if p.location:
                    intf_num = int(p.location.rsplit(".", 1)[-1])
            except Exception:
                pass
            bldc_candidates.append((intf_num, p.device))
        elif (vid, pid) == (0x1a86, 0x7523):
            result["sensor"] = p.device

    # BLDC 선택: intf=2 우선, 없으면 intf 높은 쪽
    if bldc_candidates:
        bldc_candidates.sort(key=lambda x: (-(x[0] or 0), x[1]))
        for intf, dev in bldc_candidates:
            if intf == 2:
                result["bldc"] = dev
                break
        if not result["bldc"]:
            result["bldc"] = bldc_candidates[0][1]

    return result


# === AC Servo 연결 ===

@app.post("/api/ac/connect")
async def ac_connect(config: PortConfig):
    try:
        ac_servo.connect(config.port, config.baudrate)
        return {"ok": True, "connected": ac_servo.connected}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/api/ac/disconnect")
async def ac_disconnect():
    ac_servo.disconnect()
    return {"ok": True, "connected": False}


@app.post("/api/ac/comm-test")
async def ac_comm_test():
    resp = ac_servo.comm_test()
    if resp:
        return {"ok": True, "response": resp.hex(), "length": len(resp)}
    return {"ok": False, "error": "No response"}


@app.get("/api/ac/feedback-debug")
async def ac_feedback_debug():
    """Read-only AC servo feedback diagnostics."""
    if not ac_servo.connected:
        return {"ok": False, "error": "AC servo is not connected"}
    registers = {}
    for address in (0x0B04, 0x0B05, 0x0B06, 0x0B07, 0x0B08):
        try:
            registers[f"0x{address:04X}"] = ac_servo.read_register_signed(address)
        except Exception as e:
            registers[f"0x{address:04X}"] = {"error": str(e)}
    return {
        "ok": True,
        "registers": registers,
        "control_feedback": control.feedback_snapshot(),
    }




@app.post("/api/ac/diagnose")
async def ac_diagnose():
    """포트 심층 진단: 수신 데이터 확인 + 여러 slave ID/baudrate 조합 테스트"""
    import serial as ser
    import time
    from modbus_rtu import crc16
    import struct

    port_name = None
    if ac_servo.connected and ac_servo.port:
        port_name = ac_servo.port.name
        ac_servo.disconnect()

    if not port_name:
        ports = sorted([p.device for p in ser.tools.list_ports.comports()
                        if "ttyACM" in p.device or "ttyUSB" in p.device])
        if not ports:
            return {"ok": False, "error": "No USB serial port found"}
        port_name = ports[0]

    results = []

    # 1단계: 포트 열자마자 장치가 혼자 보내는 데이터 있는지 확인
    passive_data = ""
    try:
        port = ser.Serial(port_name, 115200, timeout=1.0)
        port.dtr = True
        port.rts = True
        time.sleep(0.5)
        incoming = port.read(port.in_waiting or 256)
        if incoming:
            passive_data = incoming.hex()
            try:
                passive_data += f" (ascii: {incoming.decode('ascii', errors='replace')})"
            except Exception:
                pass
        port.close()
    except Exception as e:
        passive_data = f"error: {e}"

    # 2단계: baudrate + slave ID 조합 테스트
    test_baudrates = [115200, 230400, 500000, 9600, 38400, 57600, 460800, 921600]
    test_slave_ids = [1, 0, 2, 3]

    for baud in test_baudrates:
        for sid in test_slave_ids:
            try:
                # FC 03h: Read Holding Register (addr=0x0003, qty=1)
                frame = struct.pack(">BBH H", sid, 0x03, 0x0003, 1)
                crc_val = crc16(frame)
                frame += struct.pack("<H", crc_val)

                port = ser.Serial(port_name, baud, timeout=0.5, write_timeout=0.5)
                port.dtr = True
                port.rts = True
                time.sleep(0.02)
                port.reset_input_buffer()
                port.write(frame)
                port.flush()
                time.sleep(0.15)
                resp = port.read(port.in_waiting or 64)
                port.close()

                if len(resp) > 0:
                    results.append({
                        "baudrate": baud,
                        "slave_id": sid,
                        "response": resp.hex(),
                        "length": len(resp),
                        "ok": True,
                    })
                    # 찾으면 바로 리턴
                    return {
                        "port": port_name,
                        "passive_data": passive_data,
                        "found": True,
                        "match": {"baudrate": baud, "slave_id": sid, "response": resp.hex()},
                        "tested": len(results),
                    }
            except Exception:
                pass

    return {
        "port": port_name,
        "passive_data": passive_data,
        "found": False,
        "tested": len(test_baudrates) * len(test_slave_ids),
        "message": "No Modbus response from any baudrate/slave_id combination",
    }


# === BLDC 연결 ===

@app.post("/api/bldc/connect")
async def bldc_connect(config: PortConfig):
    try:
        bldc_motor.connect(config.port, config.baudrate)
        return {"ok": True, "connected": bldc_motor.connected}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/api/bldc/disconnect")
async def bldc_disconnect():
    bldc_motor.disconnect()
    return {"ok": True, "connected": False}


@app.post("/api/bldc/comm-test")
async def bldc_comm_test():
    resp = bldc_motor.comm_test()
    if resp:
        return {"ok": True, "response": resp.hex()}
    return {"ok": False, "error": "No response"}




# === 센서 연결 ===

@app.post("/api/sensor/connect")
async def sensor_connect(config: SensorPortConfig):
    try:
        sensor.connect(config.port, config.baudrate)
        return {"ok": True, "connected": sensor.connected}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/api/sensor/disconnect")
async def sensor_disconnect():
    sensor.disconnect()
    return {"ok": True, "connected": False}


# === 제어 ===

@app.post("/api/control/start")
async def control_start(config: ControlConfig):
    control.apply_speed_config(config.outer, config.inner)
    if not control.running:
        control.start()
    return {"ok": True, "running": True, "config": control.speed_config_snapshot()}


@app.post("/api/control/config")
async def control_config(config: ControlConfig):
    control.apply_speed_config(config.outer, config.inner)
    return {"ok": True, "running": control.running, "config": control.speed_config_snapshot()}


@app.post("/api/control/stop")
async def control_stop():
    control.stop()
    return {"ok": True, "running": False}


@app.get("/api/control/status")
async def control_status():
    return {
        "running": control.running,
        "ac_connected": ac_servo.connected,
        "bldc_connected": bldc_motor.connected,
        "sensor_connected": sensor.connected,
        "strategy": control.state.strategy_name,
        "strategy_params": control.state.strategy_params,
        "config": control.speed_config_snapshot(),
    }


# === 전략 (논문 기반) ===

@app.get("/api/strategies")
async def list_strategies():
    """등록된 전략 + 권장 파라미터 + 한글 설명."""
    items = []
    for name in STRATEGY_REGISTRY.keys():
        preset = STRATEGY_PRESETS.get(name, {})
        items.append({
            "name": name,
            "description": preset.get("description", ""),
            "params": preset.get("params", {}),
            "formula": preset.get("formula", []),
        })
    return {"strategies": items, "current": control.state.strategy_name}


@app.post("/api/strategy")
async def set_strategy(cfg: StrategyConfig):
    """전략을 설정. name='' 또는 'none' 이면 속도함수 모드로 복귀."""
    if not cfg.name or cfg.name.lower() == "none":
        control.set_strategy(None)
        control.set_strategy_params({})
        return {"ok": True, "strategy": ""}
    if cfg.name not in STRATEGY_REGISTRY:
        return {"ok": False, "error": f"Unknown strategy: {cfg.name}"}
    try:
        # 프리셋 기본값 + 사용자 override
        preset = STRATEGY_PRESETS.get(cfg.name, {}).get("params", {})
        params = {**preset, **(cfg.params or {})}
        strat = make_strategy(cfg.name, **params)
        control.set_strategy(strat)
        control.set_strategy_params(params)
        return {"ok": True, "strategy": cfg.name, "params": params}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.get("/api/strategy/debug")
async def strategy_debug():
    """Read-only diagnostic snapshot for the active strategy."""
    strategy = control.strategy
    debug = strategy.debug_state() if strategy is not None else {}
    return {
        "ok": True,
        "running": control.running,
        "strategy": control.state.strategy_name,
        "strategy_params": control.state.strategy_params,
        "debug": debug,
    }


def _simulate_strategy_debug_dry_run(cfg: StrategyDryRunConfig) -> dict[str, Any]:
    if cfg.name not in STRATEGY_REGISTRY:
        raise ValueError(f"Unknown strategy: {cfg.name}")
    if cfg.duration_s <= 0 or cfg.duration_s > 3600:
        raise ValueError("duration_s must be in (0, 3600]")
    if cfg.dt_s <= 0 or cfg.dt_s > 1.0:
        raise ValueError("dt_s must be in (0, 1.0]")
    if cfg.tau_s <= 0 or cfg.tau_s > 2.0:
        raise ValueError("tau_s must be in (0, 2.0]")
    if cfg.command_slew_rpm_s < 0 or cfg.command_slew_rpm_s > 500.0:
        raise ValueError("command_slew_rpm_s must be in [0, 500]")
    if cfg.inner_actual_gain <= 0 or cfg.inner_actual_gain > 5.0:
        raise ValueError("inner_actual_gain must be in (0, 5]")

    preset = STRATEGY_PRESETS.get(cfg.name, {}).get("params", {})
    params = {**preset, **(cfg.params or {})}
    strategy = make_strategy(cfg.name, **params)
    theta = cfg.theta0_rad
    cmd_inner_rpm = 0.0
    actual_inner_rpm = 0.0
    tracking_sq = 0.0
    tracking_peak = 0.0
    n = max(1, int(cfg.duration_s / cfg.dt_s))
    sample_every = max(1, int(cfg.sample_period_s / cfg.dt_s))
    trace = []
    max_brake_deg = 0.0
    max_i5 = 0.0
    guard_seen = False

    for i in range(n):
        t = i * cfg.dt_s
        _, target_inner_rpm = strategy.step(t, theta)
        if cfg.command_slew_rpm_s > 0.0:
            max_delta = cfg.command_slew_rpm_s * cfg.dt_s
            delta = max(-max_delta, min(max_delta, target_inner_rpm - cmd_inner_rpm))
            cmd_inner_rpm += delta
        else:
            cmd_inner_rpm = target_inner_rpm
        alpha = 1.0 - math.exp(-cfg.dt_s / cfg.tau_s)
        actual_inner_rpm += alpha * (cfg.inner_actual_gain * cmd_inner_rpm - actual_inner_rpm)
        theta += (actual_inner_rpm / RAD_S_TO_RPM) * cfg.dt_s

        err = actual_inner_rpm - cmd_inner_rpm
        tracking_sq += err * err
        tracking_peak = max(tracking_peak, abs(err))

        debug = strategy.debug_state()
        max_brake_deg = max(max_brake_deg, float(debug.get("brake_distance_deg", 0.0)))
        max_i5 = max(max_i5, float(debug.get("current_i5", 0.0)))
        guard_seen = guard_seen or bool(debug.get("guard_active", False))
        if i % sample_every == 0 or i == n - 1:
            trace.append({
                "t": round(t, 3),
                "theta_deg": math.degrees(theta),
                "cmd_inner_rpm": cmd_inner_rpm,
                "actual_inner_rpm": actual_inner_rpm,
                "tracking_error_rpm": err,
                "debug": debug,
            })

    final_debug = strategy.debug_state()
    return {
        "strategy": cfg.name,
        "params": params,
        "duration_s": cfg.duration_s,
        "dt_s": cfg.dt_s,
        "tau_s": cfg.tau_s,
        "command_slew_rpm_s": cfg.command_slew_rpm_s,
        "inner_actual_gain": cfg.inner_actual_gain,
        "samples": n,
        "tracking_rms_rpm": math.sqrt(tracking_sq / n),
        "tracking_peak_rpm": tracking_peak,
        "max_brake_distance_deg": max_brake_deg,
        "max_i5": max_i5,
        "guard_seen": guard_seen,
        "final_debug": final_debug,
        "trace": trace,
    }


@app.post("/api/strategy/debug/dry-run")
async def strategy_debug_dry_run(cfg: StrategyDryRunConfig):
    """Simulate one strategy instance without touching active control state."""
    try:
        dry_run = _simulate_strategy_debug_dry_run(cfg)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    return {
        "ok": True,
        "running": control.running,
        "active_strategy": control.state.strategy_name,
        "dry_run": dry_run,
    }


def _run_strategy_preflight_gate(cfg: StrategyPreflightConfig) -> dict[str, Any]:
    if cfg.name not in STRATEGY_REGISTRY:
        raise ValueError(f"Unknown strategy: {cfg.name}")
    if not cfg.tau_scenarios or len(cfg.tau_scenarios) > 12:
        raise ValueError("tau_scenarios must contain 1-12 values")

    scenarios = []
    gate_pass = True
    is_microgravity_strategy = cfg.name in MICROGRAVITY_STRATEGIES

    def append_scenario(kind: str, tau_s: float, dry: dict[str, Any]) -> None:
        nonlocal gate_pass
        final_debug = dry["final_debug"]
        # Strategy-class awareness: the tau-guard / pole-i5 realism checks only
        # apply to strategies that expose that machinery (taSPG / pole-guard
        # family). Microgravity strategies (e.g. fibonacci_lattice_dwell) return
        # an empty debug dict, so those checks are not-applicable for them. The
        # MECHANICAL SAFETY checks (tracking_rms/peak/brake) are ALWAYS enforced.
        has_tau_guard = "tau_est_s" in final_debug
        has_pole_i5 = "current_i5" in final_debug
        final_tau = float(final_debug.get("tau_est_s", 0.0))
        final_i5 = float(final_debug.get("current_i5", 999.0))
        guard_active = bool(final_debug.get("guard_active", False))
        expected_guard = final_tau >= cfg.guard_tau_s
        tau_min_s = cfg.tau_min_s
        if kind in ("hardware_startup_transient", "hardware_short_transient"):
            tau_min_s += cfg.startup_tau_margin_s
        i5_max = cfg.final_i5_max
        if kind == "hardware_short_transient":
            i5_max = cfg.short_final_i5_max
        target_error_bias_G = cfg.target_error_bias_G
        if target_error_bias_G == 0.0:
            target_error_bias_G = TARGET_ERROR_BIAS_G_BY_STRATEGY.get(cfg.name, 0.0)
        target_error_G = final_debug.get("target_error_G")
        calibrated_target_error_G = None
        if target_error_G is not None:
            calibrated_target_error_G = float(target_error_G) + target_error_bias_G
        tracking_rms_max_rpm = (
            cfg.microgravity_tracking_rms_max_rpm
            if is_microgravity_strategy else cfg.tracking_rms_max_rpm
        )
        checks = {
            "tracking_rms": dry["tracking_rms_rpm"] <= tracking_rms_max_rpm,
            "tracking_peak": dry["tracking_peak_rpm"] <= cfg.tracking_peak_max_rpm,
            "brake_distance": dry["max_brake_distance_deg"] <= cfg.brake_distance_max_deg,
        }
        if has_tau_guard:
            checks["tau_in_range"] = tau_min_s <= final_tau <= cfg.tau_max_s
            checks["guard_matches_tau"] = guard_active == expected_guard
        if has_pole_i5:
            checks["final_i5"] = final_i5 <= i5_max
        passed = all(checks.values())
        gate_pass = gate_pass and passed
        scenarios.append({
            "kind": kind,
            "tau_s": tau_s,
            "passed": passed,
            "checks": checks,
            "summary": {
                "final_tau_est_s": final_tau,
                "guard_active": guard_active,
                "tracking_rms_rpm": dry["tracking_rms_rpm"],
                "tracking_rms_max_rpm": tracking_rms_max_rpm,
                "tracking_peak_rpm": dry["tracking_peak_rpm"],
                "max_brake_distance_deg": dry["max_brake_distance_deg"],
                "final_i5": final_i5,
                "tau_guard_checks_applicable": has_tau_guard,
                "pole_i5_check_applicable": has_pole_i5,
                "tau_i5_checks_skipped_for_microgravity": bool(
                    is_microgravity_strategy and not has_tau_guard and not has_pole_i5
                ),
                "effective_w_max_rad_s": final_debug.get("effective_w_max_rad_s"),
                "target_error_deg": final_debug.get("target", {}).get("error_deg"),
                "target_error_G": target_error_G,
                "target_error_bias_G": target_error_bias_G,
                "hardware_calibrated_target_error_G": calibrated_target_error_G,
                "bias_fraction": final_debug.get("bias_fraction"),
                "mode": final_debug.get("mode"),
                "command_slew_rpm_s": dry.get("command_slew_rpm_s"),
                "inner_actual_gain": dry.get("inner_actual_gain"),
                "tau_min_with_margin_s": tau_min_s,
                "i5_max_for_scenario": i5_max,
            },
        })

    for tau_s in cfg.tau_scenarios:
        dry = _simulate_strategy_debug_dry_run(StrategyDryRunConfig(
            name=cfg.name,
            params=cfg.params,
            duration_s=cfg.duration_s,
            dt_s=cfg.dt_s,
            tau_s=tau_s,
            sample_period_s=cfg.sample_period_s,
        ))
        append_scenario("lag_scenario", tau_s, dry)

    if cfg.hardware_startup_check:
        startup = _simulate_strategy_debug_dry_run(StrategyDryRunConfig(
            name=cfg.name,
            params=cfg.params,
            duration_s=cfg.startup_duration_s,
            dt_s=cfg.dt_s,
            tau_s=cfg.startup_tau_s,
            sample_period_s=cfg.sample_period_s,
            command_slew_rpm_s=cfg.startup_command_slew_rpm_s,
            inner_actual_gain=cfg.startup_inner_actual_gain,
        ))
        append_scenario("hardware_startup_transient", cfg.startup_tau_s, startup)

    if cfg.hardware_short_check:
        short = _simulate_strategy_debug_dry_run(StrategyDryRunConfig(
            name=cfg.name,
            params=cfg.params,
            duration_s=cfg.short_duration_s,
            dt_s=cfg.dt_s,
            tau_s=cfg.short_tau_s,
            sample_period_s=cfg.sample_period_s,
            command_slew_rpm_s=cfg.short_command_slew_rpm_s,
            inner_actual_gain=cfg.short_inner_actual_gain,
        ))
        append_scenario("hardware_short_transient", cfg.short_tau_s, short)

    return {
        "passed": gate_pass,
        "strategy": cfg.name,
        "strategy_family": "microgravity" if is_microgravity_strategy else "tau_i5",
        "duration_s": cfg.duration_s,
        "dt_s": cfg.dt_s,
        "thresholds": {
            "guard_tau_s": cfg.guard_tau_s,
            "tau_min_s": cfg.tau_min_s,
            "tau_max_s": cfg.tau_max_s,
            "tracking_rms_max_rpm": cfg.tracking_rms_max_rpm,
            "microgravity_tracking_rms_max_rpm": cfg.microgravity_tracking_rms_max_rpm,
            "tracking_peak_max_rpm": cfg.tracking_peak_max_rpm,
            "brake_distance_max_deg": cfg.brake_distance_max_deg,
            "final_i5_max": cfg.final_i5_max,
            "hardware_startup_check": cfg.hardware_startup_check,
            "startup_duration_s": cfg.startup_duration_s,
            "startup_tau_s": cfg.startup_tau_s,
            "startup_command_slew_rpm_s": cfg.startup_command_slew_rpm_s,
            "startup_inner_actual_gain": cfg.startup_inner_actual_gain,
            "startup_tau_margin_s": cfg.startup_tau_margin_s,
            "hardware_short_check": cfg.hardware_short_check,
            "short_duration_s": cfg.short_duration_s,
            "short_tau_s": cfg.short_tau_s,
            "short_command_slew_rpm_s": cfg.short_command_slew_rpm_s,
            "short_inner_actual_gain": cfg.short_inner_actual_gain,
            "short_final_i5_max": cfg.short_final_i5_max,
            "target_error_bias_G": cfg.target_error_bias_G,
            "default_target_error_biases_G": TARGET_ERROR_BIAS_G_BY_STRATEGY,
            "microgravity_strategies_skip_tau_i5_checks": sorted(MICROGRAVITY_STRATEGIES),
        },
        "scenarios": scenarios,
    }


@app.post("/api/strategy/debug/preflight")
async def strategy_debug_preflight(cfg: StrategyPreflightConfig):
    """Run a non-motion realism gate across lag scenarios for optimizers/LLMs."""
    try:
        gate = _run_strategy_preflight_gate(cfg)
    except ValueError as e:
        return {"ok": False, "error": str(e)}

    return {
        "ok": True,
        "running": control.running,
        "active_strategy": control.state.strategy_name,
        "gate": gate,
    }


# === 메트릭 / 단위구 산점 ===

@app.get("/api/sphere")
async def sphere_points(source: str = "cmd", max_points: int = 800):
    """단위구 산점도용 다운샘플 점 목록. source ∈ {cmd, meas}."""
    pts = control.get_sphere_points(source=source, max_points=max_points)
    return {"source": source, "n": len(pts), "points": pts}


# === 카메라 (USB 웹캠) ===

@app.get("/api/camera/status")
async def camera_status():
    return camera.status()


@app.get("/api/camera/snapshot")
async def camera_snapshot():
    """현재 프레임 1장을 JPEG으로 반환."""
    jpeg = camera.snapshot(timeout=3.0)
    if jpeg is None:
        return Response(content=b"camera unavailable", status_code=503,
                        media_type="text/plain")
    return Response(content=jpeg, media_type="image/jpeg",
                    headers={"Cache-Control": "no-store"})


@app.get("/api/camera/stream")
async def camera_stream():
    """multipart/x-mixed-replace MJPEG 스트림 — <img src=...> 가 곧장 표시."""
    gen = camera.mjpeg_generator()
    return StreamingResponse(
        gen,
        media_type="multipart/x-mixed-replace; boundary=frame",
        headers={"Cache-Control": "no-store"},
    )


@app.post("/api/run/save")
async def save_run():
    """현재 메트릭 누적값을 JSON 으로 덤프."""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = RUNS_DIR / f"run_{ts}.json"
    summary = control.metrics_run_summary()
    summary["timestamp"] = ts
    with open(path, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)
    return {"ok": True, "path": str(path), "summary": summary}


# === 자동 실험 실행 / 검증 ===

@app.post("/api/experiment/run")
async def experiment_run(cfg: ExperimentRunConfig):
    if cfg.strategy not in STRATEGY_REGISTRY:
        return {"ok": False, "error": f"Unknown strategy: {cfg.strategy}"}
    preflight = _optimizer_preflight_runner(cfg.strategy, cfg.params, cfg.preflight)
    if not preflight.get("passed"):
        return {"ok": False, "error": "experiment strategy preflight failed", "preflight": preflight}
    return experiment_runner.start(
        strategy_name=cfg.strategy,
        params=cfg.params,
        duration_s=cfg.duration_s,
        stop_motion_on_finish=cfg.stop_motion_on_finish,
    )


@app.post("/api/experiment/stop")
async def experiment_stop():
    return experiment_runner.stop()


@app.get("/api/experiment/status")
async def experiment_status():
    return experiment_runner.status()


# === 조건 최적화 ===

@app.post("/api/optimizer/start")
async def optimizer_start(cfg: OptimizerRunConfig):
    if cfg.strategy not in STRATEGY_REGISTRY:
        return {"ok": False, "error": f"Unknown strategy: {cfg.strategy}"}
    if cfg.preflight_enabled is not True:
        return {
            "ok": False,
            "error": "optimizer preflight cannot be disabled",
            "preflight_required": True,
        }
    return optimizer_runner.start(
        strategy=cfg.strategy,
        base_params=cfg.base_params,
        param_ranges=cfg.param_ranges,
        duration_s=cfg.duration_s,
        max_trials=cfg.max_trials,
        objective_source=cfg.objective_source,
        seed=cfg.seed,
        preflight_enabled=cfg.preflight_enabled,
        preflight_options=cfg.preflight,
    )


@app.post("/api/optimizer/stop")
async def optimizer_stop():
    return optimizer_runner.stop()


@app.get("/api/optimizer/status")
async def optimizer_status():
    return optimizer_runner.status()


# === 연구 플랫폼 상태 (read-only JSON bundle) ===

@app.get("/api/research/{route_name:path}")
async def research_status_route(route_name: str):
    """Serve precomputed research-status JSON without touching hardware state."""
    safe_name = route_name.strip("/")
    if not safe_name or "/" in safe_name or safe_name in {".", ".."}:
        raise HTTPException(status_code=404, detail="unknown research route")
    path = RESEARCH_STATUS_DIR / f"api__research__{safe_name}.json"
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail="unknown research route")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        raise HTTPException(status_code=500, detail="invalid research status payload")


# === WebSocket ===

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    try:
        while True:
            data = control.state.to_dict()
            if not control.running and sensor.connected:
                data["sensor"] = sensor.get_data()
            await ws.send_json(data)
            await asyncio.sleep(0.1)
    except (WebSocketDisconnect, Exception):
        pass


# ============================================================
#  SpaceBio 통합 프록시 (설계 스펙 6.1)
#  브라우저는 Gateway에 직접 접속하지 않고 항상 여기를 경유한다.
#  Gateway 장애는 아래 경로에만 갇히고 모터 제어로 전파되지 않는다.
# ============================================================

def _spacebio_envelope_error(request_id: str, code: str, message: str) -> dict:
    return {
        "schema_version": 1,
        "request_id": request_id,
        "server_time": datetime.now().astimezone().isoformat(),
        "error": {"code": code, "message": message},
    }


def _spacebio_request_id(request: Request) -> str:
    return request.headers.get("X-Request-ID") or spacebio_proxy.new_request_id()


async def _spacebio_relay(request: Request, path: str, body: Any = None) -> Any:
    """Gateway 응답을 그대로 돌려주되, 오류는 status와 code를 보존해 옮긴다."""
    request_id = _spacebio_request_id(request)
    proxy = request.app.state.spacebio
    try:
        if body is None:
            return await proxy.get(path, request_id)
        return await proxy.mutate(path, body, request_id)
    except ProxyPathError:
        return JSONResponse(
            status_code=404,
            content=_spacebio_envelope_error(request_id, "not_proxied", "unknown route"),
        )
    except ProxyGatewayError as exc:
        return JSONResponse(
            status_code=exc.status_code,
            content=_spacebio_envelope_error(
                request_id, exc.error.get("code", "gateway_error"),
                exc.error.get("message", ""),
            ),
        )


@app.get("/api/spacebio/status")
async def spacebio_status(request: Request):
    return await _spacebio_relay(request, "/api/spacebio/status")


@app.get("/api/spacebio/sensor/status")
async def spacebio_sensor_status(request: Request):
    return await _spacebio_relay(request, "/api/spacebio/sensor/status")


@app.get("/api/spacebio/pump/status")
async def spacebio_pump_status(request: Request):
    return await _spacebio_relay(request, "/api/spacebio/pump/status")


@app.get("/api/spacebio/sensor/datasets")
async def spacebio_sensor_datasets(request: Request):
    return await _spacebio_relay(request, "/api/spacebio/sensor/datasets")


@app.get("/api/spacebio/session/status")
async def spacebio_session_status(request: Request):
    return await _spacebio_relay(request, "/api/spacebio/session/status")


@app.post("/api/spacebio/sensor/configure")
async def spacebio_sensor_configure(request: Request):
    return await _spacebio_relay(
        request, "/api/spacebio/sensor/configure", await request.json()
    )


@app.post("/api/spacebio/sensor/start")
async def spacebio_sensor_start(request: Request):
    return await _spacebio_relay(request, "/api/spacebio/sensor/start", {})


@app.post("/api/spacebio/sensor/stop")
async def spacebio_sensor_stop(request: Request):
    return await _spacebio_relay(request, "/api/spacebio/sensor/stop", {})


@app.post("/api/spacebio/pump/dispense")
async def spacebio_pump_dispense(request: Request):
    return await _spacebio_relay(
        request, "/api/spacebio/pump/dispense", await request.json()
    )


@app.post("/api/spacebio/pump/stop")
async def spacebio_pump_stop(request: Request):
    return await _spacebio_relay(request, "/api/spacebio/pump/stop", {})


@app.post("/api/spacebio/pump/step")
async def spacebio_pump_step(request: Request):
    return await _spacebio_relay(
        request, "/api/spacebio/pump/step", await request.json()
    )


@app.post("/api/spacebio/pump/emergency-stop")
async def spacebio_pump_emergency_stop(request: Request):
    return await _spacebio_relay(request, "/api/spacebio/pump/emergency-stop", {})


@app.post("/api/spacebio/pump/reset-emergency-stop")
async def spacebio_pump_reset_emergency_stop(request: Request):
    return await _spacebio_relay(
        request, "/api/spacebio/pump/reset-emergency-stop", await request.json()
    )


@app.post("/api/spacebio/session/start")
async def spacebio_session_start(request: Request):
    return await _spacebio_relay(
        request, "/api/spacebio/session/start", await request.json()
    )


@app.post("/api/spacebio/session/update")
async def spacebio_session_update(request: Request):
    return await _spacebio_relay(
        request, "/api/spacebio/session/update", await request.json()
    )


@app.post("/api/spacebio/session/finish")
async def spacebio_session_finish(request: Request):
    return await _spacebio_relay(
        request, "/api/spacebio/session/finish", await request.json()
    )


@app.websocket("/ws/spacebio")
async def spacebio_ws(ws: WebSocket):
    """Gateway 상태를 브라우저로 중계한다. 기존 /ws 는 건드리지 않는다."""
    await ws.accept()
    proxy = ws.app.state.spacebio
    sequence = 0
    try:
        while True:
            # proxy.get은 envelope 전체({schema_version, request_id, data:{...}})를
            # 돌려준다. 브라우저는 message.data.sensor를 기대하므로 내부 data만
            # 꺼내 보낸다 — envelope째 감싸면 data.data.sensor로 이중 중첩돼
            # 화면 차트가 샘플을 못 받는다(2026-07-25 실제 발생).
            envelope = await proxy.get("/api/spacebio/status", None)
            data = envelope.get("data", envelope) if isinstance(envelope, dict) else envelope
            sequence += 1
            await ws.send_json({"type": "status", "sequence": sequence, "data": data})
            await asyncio.sleep(0.2)          # 브라우저 중계 5 Hz 상한
    except (WebSocketDisconnect, Exception):
        pass


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
