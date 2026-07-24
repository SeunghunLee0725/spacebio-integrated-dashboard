#!/usr/bin/env python3
"""Build sanitized clinostat contracts without importing captured application code."""

from __future__ import annotations

import argparse
import ast
import hashlib
import html.parser
import json
import pathlib
import re
import shutil
import sys


class ContractError(RuntimeError):
    pass


def discover_websocket_path(server_path: pathlib.Path, index_path: pathlib.Path) -> str:
    """Return the one literal WS route corroborated by server or browser source."""
    paths: set[str] = set()
    tree = ast.parse(server_path.read_text())
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            if not isinstance(decorator, ast.Call) or not decorator.args:
                continue
            function = decorator.func
            if (
                isinstance(function, ast.Attribute)
                and function.attr == "websocket"
                and isinstance(decorator.args[0], ast.Constant)
                and isinstance(decorator.args[0].value, str)
            ):
                paths.add(decorator.args[0].value)
    browser = index_path.read_text(errors="strict")
    for match in re.finditer(
        r"new\s+WebSocket\s*\(\s*(?:`[^`]*?|['\"][^'\"]*?)(/ws/[A-Za-z0-9_./-]+)",
        browser,
    ):
        paths.add(match.group(1))
    valid = {path for path in paths if re.fullmatch(r"/ws/[A-Za-z0-9_./-]+", path)}
    if len(valid) != 1:
        raise ContractError(
            f"expected exactly one WebSocket path in captured sources, found {sorted(valid)}"
        )
    return next(iter(valid))


CONTROL_CONSTANTS = {
    "GEAR_RATIO",
    "ENCODER_PULSES_PER_REV",
    "CONTROL_LOOP_INTERVAL_S",
    "MAX_MOTOR_RPM",
}


def extract_control_constants(source_paths: list[pathlib.Path]) -> dict:
    found: dict[str, int | float] = {}
    mapping: dict[str, dict] = {}
    for source in source_paths:
        tree = ast.parse(source.read_text())
        for node in tree.body:
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name) and target.id in CONTROL_CONSTANTS:
                    try:
                        value = ast.literal_eval(node.value)
                    except (ValueError, TypeError) as error:
                        raise ContractError(f"{target.id} is not a literal") from error
                    if isinstance(value, bool) or not isinstance(value, (int, float)):
                        raise ContractError(f"{target.id} is not numeric")
                    if target.id in found:
                        raise ContractError(f"duplicate control constant {target.id}")
                    found[target.id] = value
                    mapping[target.id] = {
                        "source_file": source.name,
                        "source_name": target.id,
                        "line": node.lineno,
                    }
    missing = CONTROL_CONSTANTS - found.keys()
    if missing:
        raise ContractError(f"required real control constants absent: {sorted(missing)}")
    return {
        "extraction": "python-ast-explicit-allowlist",
        "constants": dict(sorted(found.items())),
        "mapping": dict(sorted(mapping.items())),
    }


def extract_control_golden(source_paths: list[pathlib.Path], constants: dict) -> dict:
    """Accept only `motor_rpm(axis_rpm): return axis_rpm * GEAR_RATIO`."""
    for source in source_paths:
        text = source.read_text()
        tree = ast.parse(text)
        for node in tree.body:
            if not isinstance(node, ast.FunctionDef) or node.name != "motor_rpm":
                continue
            if [arg.arg for arg in node.args.args] != ["axis_rpm"] or len(node.body) != 1:
                continue
            result = node.body[0]
            if not isinstance(result, ast.Return) or not isinstance(result.value, ast.BinOp):
                continue
            expression = result.value
            if not isinstance(expression.op, ast.Mult):
                continue
            if not (
                isinstance(expression.left, ast.Name)
                and expression.left.id == "axis_rpm"
                and isinstance(expression.right, ast.Name)
                and expression.right.id == "GEAR_RATIO"
            ):
                continue
            ratio = constants["constants"]["GEAR_RATIO"]
            cases = [
                {"input": {"axis_rpm": value}, "output": value * ratio}
                for value in (0.0, 1.0, -1.0, 2.5)
            ]
            return {
                "hardware_imported": False,
                "constructor_strategy": "source-verified-pure-function",
                "formula": "axis_rpm * GEAR_RATIO",
                "source": {
                    "file": source.name,
                    "function": node.name,
                    "line": node.lineno,
                    "sha256": hashlib.sha256(text.encode()).hexdigest(),
                },
                "cases": cases,
            }
    raise ContractError(
        "no safe pure control calculation found; capture a source containing the "
        "allowlisted motor_rpm(axis_rpm) formula"
    )


def _typename(value):
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    if value is None:
        return None
    raise ContractError(f"unsupported JSON type {type(value).__name__}")


def sanitize_status_samples(lines: list[str], interval_seconds: float, minimum: int) -> list[dict]:
    parsed = []
    for line_number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            sample = json.loads(line)
        except json.JSONDecodeError as error:
            raise ContractError(f"status sample {line_number} is invalid JSON") from error
        if not isinstance(sample, dict):
            raise ContractError(f"status sample {line_number} must be a JSON object")
        schema = {
            key: {"type": _typename(value), "nullable": value is None}
            for key, value in sorted(sample.items())
        }
        timing_keys = {
            key: spec for key, spec in schema.items()
            if "tim" in key.lower() or "interval" in key.lower()
        }
        parsed.append({
            "offset_seconds": len(parsed) * interval_seconds,
            "schema": schema,
            "loop_timing": timing_keys,
        })
    if len(parsed) < minimum:
        raise ContractError(f"expected at least {minimum} parsed status samples, found {len(parsed)}")
    return parsed


class _IDs(html.parser.HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids: dict[str, int] = {}

    def handle_starttag(self, _tag, attrs):
        for key, value in attrs:
            if key == "id" and value:
                self.ids[value] = self.ids.get(value, 0) + 1


def _json_schema(messages: list[dict]) -> dict:
    keys = set().union(*messages)
    return {
        key: {
            "types": sorted({_typename(m[key]) for m in messages if key in m and m[key] is not None}),
            "nullable": any(key not in m or m[key] is None for m in messages),
        }
        for key in sorted(keys)
    }


def build(raw: pathlib.Path, temporary: pathlib.Path, stamp: str) -> None:
    server = raw / "server.py"
    index = raw / "static/index.html"
    ws_path = discover_websocket_path(server, index)
    source_paths = [
        path for name in ("control_loop.py", "strategies.py", "metrics.py")
        if (path := raw / name).is_file()
    ]
    constants = extract_control_constants(source_paths)
    golden = extract_control_golden(source_paths, constants)
    status = sanitize_status_samples(
        (raw / "status_samples.jsonl").read_text().splitlines(), 1.0, 3
    )
    messages = []
    for line in (raw / "websocket_samples.jsonl").read_text().splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError as error:
            raise ContractError("invalid JSON in WebSocket sample") from error
        if not isinstance(value, dict):
            raise ContractError("WebSocket samples must be JSON objects")
        messages.append(value)
    if not messages:
        raise ContractError("no WebSocket messages captured in bounded 60-second sample")
    openapi = json.loads((raw / "openapi.json").read_text())
    if "/api/control/status" not in openapi.get("paths", {}):
        raise ContractError("OpenAPI lacks required /api/control/status route")

    parser = _IDs()
    parser.feed(index.read_text())
    temporary.mkdir()
    (temporary / "static").mkdir()
    shutil.copy2(raw / "openapi.json", temporary / "openapi.json")
    shutil.copy2(server, temporary / "server.py")
    shutil.copy2(index, temporary / "static/index.html")
    shutil.copy2(raw / "clinostat.service", temporary / "clinostat.service")
    artifacts = {
        "dom_ids.json": {"ids": parser.ids},
        "control_constants.json": constants,
        "control_golden.json": golden,
        "websocket_schema.json": {
            "path": ws_path, "sample_window_seconds": 60,
            "hardware_started": False, "message_count": len(messages),
            "properties": _json_schema(messages),
        },
        "capture.json": {
            "captured_at": stamp, "source_path": "/home/aiworker-1/clinostat",
            "source_revision": (raw / "git_head.txt").read_text().strip(),
            "remote_actions": "read-only", "status_path": "/api/control/status",
            "status_interval_seconds": 1, "status_minimum_samples": 3,
        },
    }
    for name, value in artifacts.items():
        (temporary / name).write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    (temporary / "status_samples.sanitized.jsonl").write_text(
        "".join(json.dumps(sample, sort_keys=True) + "\n" for sample in status)
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--discover-ws", nargs=2, metavar=("SERVER", "INDEX"))
    parser.add_argument("--build", nargs=3, metavar=("RAW", "TEMP", "STAMP"))
    args = parser.parse_args()
    try:
        if args.discover_ws:
            print(discover_websocket_path(*(pathlib.Path(p) for p in args.discover_ws)))
        elif args.build:
            build(pathlib.Path(args.build[0]), pathlib.Path(args.build[1]), args.build[2])
        else:
            parser.error("one operation is required")
    except ContractError as error:
        print(f"contract capture failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
