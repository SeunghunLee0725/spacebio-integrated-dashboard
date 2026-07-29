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
import subprocess
import sys
import time


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
        r"new\s+WebSocket\s*\(\s*(?:`[^`]*?|['\"][^'\"]*?)(/ws(?:/[A-Za-z0-9_./-]+)?)",
        browser,
    ):
        paths.add(match.group(1))
    valid = {path for path in paths if re.fullmatch(r"/ws(?:/[A-Za-z0-9_./-]+)?", path)}
    if len(valid) != 1:
        raise ContractError(
            f"expected exactly one WebSocket path in captured sources, found {sorted(valid)}"
        )
    return next(iter(valid))


CONTROL_CONSTANTS = {
    "AC_GEAR_RATIO",
    "BLDC_GEAR_RATIO",
    "OUTER_MAX_OUTPUT_RPM",
    "INNER_MAX_OUTPUT_RPM",
    "OUTER_MAX_SLEW_RPM_S",
    "INNER_MAX_SLEW_RPM_S",
}


def _relative(source: pathlib.Path, root: pathlib.Path | None) -> str:
    return source.relative_to(root).as_posix() if root else source.name


def extract_control_constants(
    source_paths: list[pathlib.Path], root: pathlib.Path | None = None
) -> dict:
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
                        raise ContractError(f"ambiguous control constant {target.id}")
                    found[target.id] = value
                    mapping[target.id] = {
                        "source_file": _relative(source, root),
                        "source_name": target.id,
                        "line": node.lineno,
                        "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                    }
    missing = CONTROL_CONSTANTS - found.keys()
    if missing:
        raise ContractError(f"required real control constants absent: {sorted(missing)}")
    return {
        "extraction": "python-ast-explicit-allowlist",
        "constants": dict(sorted(found.items())),
        "mapping": dict(sorted(mapping.items())),
    }


def _function_matches(tree, name):
    return [node for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == name]


def _pure_eval(node, variables, constants):
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.Name):
        if node.id in variables:
            return variables[node.id]
        if node.id in constants:
            return constants[node.id]
    if isinstance(node, ast.BinOp) and type(node.op) in {
        ast.Add, ast.Sub, ast.Mult, ast.Div
    }:
        left = _pure_eval(node.left, variables, constants)
        right = _pure_eval(node.right, variables, constants)
        return {
            ast.Add: lambda: left + right, ast.Sub: lambda: left - right,
            ast.Mult: lambda: left * right, ast.Div: lambda: left / right,
        }[type(node.op)]()
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id in {"min", "max"} and len(node.args) >= 2):
        values = [_pure_eval(arg, variables, constants) for arg in node.args]
        return (min if node.func.id == "min" else max)(values)
    raise ContractError(f"unsupported pure calculation AST node {ast.dump(node)}")


def extract_control_golden(
    source_paths: list[pathlib.Path], constants: dict,
    root: pathlib.Path | None = None,
) -> dict:
    required = {"_ac_command_motor_rpm", "_slew_limit"}
    matches = {}
    for source in source_paths:
        text = source.read_text()
        tree = ast.parse(text)
        for name in required:
            for node in _function_matches(tree, name):
                if name in matches:
                    raise ContractError(f"ambiguous pure control function {name}")
                matches[name] = (source, text, node)
    missing = required - matches.keys()
    if missing:
        raise ContractError(
            f"no safe pure control calculation found; missing {sorted(missing)}"
        )
    calculations = []
    case_inputs = {
        "_ac_command_motor_rpm": [
            {"output_rpm": value} for value in (0.0, 1.0, -1.0, 2.5)
        ],
        "_slew_limit": [
            {"current": 0.0, "target": 20.0, "max_delta": 5.0},
            {"current": 10.0, "target": 7.0, "max_delta": 5.0},
            {"current": -3.0, "target": -20.0, "max_delta": 4.0},
        ],
    }
    for name in sorted(required):
        source, text, node = matches[name]
        arguments = [arg.arg for arg in node.args.args]
        expected = list(case_inputs[name][0])
        if arguments != expected or len(node.body) != 1 or not isinstance(node.body[0], ast.Return):
            raise ContractError(f"unsupported AST for safe pure control calculation {name}")
        cases = []
        for inputs in case_inputs[name]:
            output = _pure_eval(node.body[0].value, inputs, constants["constants"])
            cases.append({"input": inputs, "output": output})
        calculations.append({
            "formula_ast": ast.dump(node.body[0].value, include_attributes=False),
            "source": {
                "file": _relative(source, root),
                "function": name,
                "line": node.lineno,
                "sha256": hashlib.sha256(text.encode()).hexdigest(),
            },
            "cases": cases,
        })
    return {
                "hardware_imported": False,
                "constructor_strategy": "source-verified-pure-function",
                "calculations": calculations,
            }


def validate_ws_capture(
    *, elapsed_seconds: float, target_seconds: float, minimum_seconds: float,
    message_count: int, minimum_messages: int, exited_early: bool,
) -> None:
    if exited_early:
        raise ContractError("WebSocket client exited before observation deadline")
    if elapsed_seconds < minimum_seconds:
        raise ContractError(
            f"WebSocket observation {elapsed_seconds:.3f}s was shorter than required "
            f"{minimum_seconds:.3f}s"
        )
    if message_count < minimum_messages:
        raise ContractError(
            f"expected at least {minimum_messages} WebSocket messages, found {message_count}"
        )


def capture_websocket(
    output: pathlib.Path, metadata: pathlib.Path, url: str,
    target_seconds: float, minimum_seconds: float, minimum_messages: int,
    *, _clock=time.monotonic, _sleep=time.sleep, _popen=subprocess.Popen,
) -> None:
    started = _clock()
    with output.open("w") as stream:
        process = _popen(["websocat", url], stdout=stream)
        exited_early = False
        while _clock() - started < target_seconds:
            if process.poll() is not None:
                exited_early = True
                break
            _sleep(min(0.1, target_seconds / 10))
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        else:
            # Only supervisor termination at the target deadline is accepted.
            exited_early = True
    elapsed = _clock() - started
    count = sum(1 for line in output.read_text().splitlines() if line.strip())
    validate_ws_capture(
        elapsed_seconds=elapsed, target_seconds=target_seconds,
        minimum_seconds=minimum_seconds, message_count=count,
        minimum_messages=minimum_messages, exited_early=exited_early,
    )
    metadata.write_text(json.dumps({
        "target_duration_seconds": target_seconds,
        "minimum_duration_seconds": minimum_seconds,
        "actual_duration_seconds": elapsed,
        "minimum_messages": minimum_messages,
        "message_count": count,
    }, indent=2, sort_keys=True) + "\n")


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
    sources_root = raw / "sources"
    source_paths = sorted(sources_root.rglob("*.py"))
    constants = extract_control_constants(source_paths, root=sources_root)
    golden = extract_control_golden(source_paths, constants, root=sources_root)
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
    ws_capture = json.loads((raw / "ws_capture.json").read_text())
    validate_ws_capture(
        elapsed_seconds=ws_capture["actual_duration_seconds"],
        target_seconds=ws_capture["target_duration_seconds"],
        minimum_seconds=ws_capture["minimum_duration_seconds"],
        message_count=len(messages),
        minimum_messages=ws_capture["minimum_messages"],
        exited_early=False,
    )
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
            "path": ws_path,
            "sample_window_seconds": ws_capture["target_duration_seconds"],
            "target_duration_seconds": ws_capture["target_duration_seconds"],
            "minimum_duration_seconds": ws_capture["minimum_duration_seconds"],
            "actual_duration_seconds": ws_capture["actual_duration_seconds"],
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
    parser.add_argument("--capture-ws", nargs=6, metavar=(
        "OUTPUT", "METADATA", "URL", "TARGET_SECONDS", "MINIMUM_SECONDS",
        "MINIMUM_MESSAGES",
    ))
    args = parser.parse_args()
    try:
        if args.discover_ws:
            print(discover_websocket_path(*(pathlib.Path(p) for p in args.discover_ws)))
        elif args.build:
            build(pathlib.Path(args.build[0]), pathlib.Path(args.build[1]), args.build[2])
        elif args.capture_ws:
            capture_websocket(
                pathlib.Path(args.capture_ws[0]), pathlib.Path(args.capture_ws[1]),
                args.capture_ws[2], float(args.capture_ws[3]), float(args.capture_ws[4]),
                int(args.capture_ws[5]),
            )
        else:
            parser.error("one operation is required")
    except ContractError as error:
        print(f"contract capture failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
