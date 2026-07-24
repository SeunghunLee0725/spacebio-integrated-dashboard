"""Gateway orchestrator — wires bridge + sensor source + pump + loop + store.

Modes:
    --self-test   Connect to the broker, publish a hello on gateway/status,
                  confirm it round-trips, exit 0/1. This is the S0 gate.
    (default)     Run the gateway: stream sensors, serve pump/loop commands.

Usage:
    python -m gateway.main --self-test
    python -m gateway.main
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import threading
import time
from pathlib import Path

import yaml

from gateway.contracts import (
    Topics, GatewayStatus, PumpCommand, PumpStatus, LoopCommand,
    encode, decode, now_ms,
)
from gateway.mqtt_bridge import MqttBridge
from gateway.pump_actuator import PumpActuator, Limits, NullBackend
from gateway.control_loop import ControlLoop
from gateway.store import Store
from gateway.ble_source import SimulatedSource

logger = logging.getLogger("gateway.main")


def _setup_logging(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def _load_config(path: str) -> dict:
    p = Path(path)
    if not p.is_absolute():
        p = Path(__file__).resolve().parents[1] / path
    if not p.exists():
        logger.warning("config %s not found, using defaults", path)
        return {}
    with open(p, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _build_pump(cfg: dict) -> PumpActuator:
    """Select the pump backend from config: 'null' (default) or 'serial'."""
    pump_cfg = cfg.get("pump", {})
    limits_cfg = pump_cfg.get("limits", {})
    limits = Limits(
        max_volume_ul=float(limits_cfg.get("max_volume_ul", 1000.0)),
        max_rate_ul_s=float(limits_cfg.get("max_rate_ul_s", 200.0)),
    )
    if pump_cfg.get("backend") == "serial":
        from gateway.pump_serial import SerialPumpBackend, PumpSerialError
        port = pump_cfg.get("serial_port", "/dev/ttyUSB0")
        try:
            backend = SerialPumpBackend(port)
            logger.info("pump backend: serial %s", port)
            return PumpActuator(backend, limits)
        except PumpSerialError as exc:
            logger.error("%s — falling back to null pump backend", exc)
    else:
        logger.info("pump backend: null (no hardware)")
    return PumpActuator(NullBackend(), limits)


def self_test(host: str, port: int) -> int:
    """Publish a hello and confirm it comes back. Returns process exit code."""
    got = threading.Event()

    bridge = MqttBridge(host, port, client_id="spacebio-selftest")
    bridge.on(Topics.GATEWAY_STATUS, lambda t, p: got.set())
    try:
        bridge.connect()
    except OSError as exc:
        logger.error("cannot reach broker %s:%d (%s). Is mosquitto running?",
                     host, port, exc)
        return 1
    bridge.loop_start()
    time.sleep(0.5)  # let subscribe settle
    bridge.publish(Topics.GATEWAY_STATUS,
                   encode(GatewayStatus(state="online").to_payload()))
    ok = got.wait(timeout=5.0)
    bridge.stop()
    if ok:
        logger.info("SELF-TEST PASS — hello round-tripped via %s:%d", host, port)
        return 0
    logger.error("SELF-TEST FAIL — no round-trip within 5s")
    return 1


class Gateway:
    def __init__(self, host: str, port: int, config: dict | None = None) -> None:
        config = config or {}
        self._bridge = MqttBridge(host, port)
        self._pump = _build_pump(config)
        self._loop = ControlLoop(self._pump)
        self._store = Store(config.get("store", {}).get("db_path", "./data/gateway.sqlite"))
        self._source = SimulatedSource()
        self._bridge.on(Topics.PUMP_CMD, self._on_pump_cmd)
        self._bridge.on(Topics.LOOP_CMD, self._on_loop_cmd)

    def _publish_pump_status(self, action: str, source: str | None,
                             dispensed_ul: float | None, rate_ul_s: float | None) -> None:
        status = PumpStatus(
            state="running" if self._pump.running else "idle",
            action=action, source=source,
            dispensed_ul=dispensed_ul, rate_ul_s=rate_ul_s,
        )
        self._bridge.publish(Topics.PUMP_STATUS, encode(status.to_payload()),
                             qos=1, retain=True)
        self._store.insert("pump_event", {
            "ts": now_ms(), "action": action, "source": source,
            "volume_ul": dispensed_ul, "rate_ul_s": rate_ul_s,
        })

    def _on_pump_cmd(self, topic: str, raw: bytes) -> None:
        try:
            cmd = PumpCommand.from_payload(decode(raw))
        except ValueError as exc:
            logger.warning("bad pump cmd: %s", exc)
            return
        self._store.insert("command", {"ts": now_ms(), "topic": topic,
                                       "payload": raw.decode("utf-8", "replace")})
        rate = cmd.rate_ul_s or 50.0
        if cmd.action == "dispense":
            dispensed = self._pump.dispense(cmd.volume_ul or 0.0, rate, cmd.source)
            self._publish_pump_status("dispense", cmd.source, dispensed, rate)
        elif cmd.action == "on":
            self._pump.on(rate)
            self._publish_pump_status("on", cmd.source, None, rate)
        else:
            self._pump.off()
            self._publish_pump_status("off", cmd.source, None, None)

    def _on_loop_cmd(self, topic: str, raw: bytes) -> None:
        try:
            cmd = LoopCommand.from_payload(decode(raw))
        except ValueError as exc:
            logger.warning("bad loop cmd: %s", exc)
            return
        if cmd.action == "arm":
            state = self._loop.arm(cmd.trigger or "tension_n",
                                   cmd.threshold or 0.0, cmd.on_trigger)
        elif cmd.action == "estop":
            state = self._loop.estop()
        else:
            state = self._loop.disarm()
        self._bridge.publish(Topics.LOOP_STATE, encode(state.to_payload()),
                             qos=1, retain=True)

    async def _heartbeat(self) -> None:
        while True:
            status = GatewayStatus(
                pump="running" if self._pump.running else "idle",
                loop="armed" if self._loop.state.armed else "disarmed",
            )
            self._bridge.publish(Topics.GATEWAY_STATUS,
                                 encode(status.to_payload()), qos=1, retain=True)
            await asyncio.sleep(2.0)

    async def _sensors(self) -> None:
        async for sample in self._source.stream():
            self._bridge.publish(Topics.LOC_SENSOR, encode(sample.to_payload()), qos=0)
            self._store.insert("sensor", {
                "ts": now_ms(), "tension_n": sample.tension_n,
                "delta_r_over_r0": sample.delta_r_over_r0,
                "temp_c": sample.temp_c, "battery_pct": sample.battery_pct,
            })
            self._loop.on_sample(sample)

    async def run(self) -> None:
        self._bridge.connect()
        self._bridge.loop_start()
        logger.info("gateway online — Ctrl-C to stop")
        await asyncio.gather(self._heartbeat(), self._sensors())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Space-bio integration gateway")
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", type=int, default=1883)
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    _setup_logging(args.verbose)

    if args.self_test:
        return self_test(args.host, args.port)

    config = _load_config(args.config)
    try:
        asyncio.run(Gateway(args.host, args.port, config).run())
    except KeyboardInterrupt:
        logger.info("shutting down")
    return 0


if __name__ == "__main__":
    sys.exit(main())
