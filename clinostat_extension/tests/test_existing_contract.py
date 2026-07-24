import importlib.util
import json
import os
import subprocess
import shutil
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "deploy" / "capture_pi_baseline.sh"
HELPER = ROOT / "deploy" / "build_pi_baseline.py"
spec = importlib.util.spec_from_file_location("baseline_builder", HELPER)
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


def test_capture_requires_pi_identity():
    result = subprocess.run(
        ["bash", str(SCRIPT)], cwd=ROOT, env={"PATH": os.environ["PATH"]},
        text=True, capture_output=True,
    )
    assert result.returncode != 0
    assert "PI_HOST and PI_USER are required" in result.stderr


def test_capture_uses_actual_status_route_and_never_old_route():
    text = SCRIPT.read_text()
    assert "/api/control/status" in text
    assert "8000/api/status" not in text
    assert "timeout 60" not in text
    assert "StrictHostKeyChecking=yes" in text
    forbidden = ("systemctl start", "systemctl restart", "sudo ", "reboot")
    assert not any(command in text for command in forbidden)


def test_capture_publication_is_fail_closed_and_requires_strong_scanner():
    text = SCRIPT.read_text()
    assert "gitleaks" in text and "trufflehog" in text
    assert "no override is supported" in text
    raw_scan = text.index('scan_tree "$raw"')
    build = text.index("--build")
    temp_scan = text.index('scan_tree "$temporary"')
    publish = text.index('mv "$temporary" "$safe"')
    work_copy = text.index('cp "$temporary/server.py"')
    assert raw_scan < build < temp_scan < work_copy < publish
    cleanup = text[text.index("cleanup()"):text.index("trap cleanup")]
    assert 'rm -rf "$temporary" "$work_tmp"' in cleanup
    assert 'rm -rf "$safe"' in cleanup


def test_secret_in_raw_leaves_no_sanitized_tree_or_work_copy(tmp_path):
    repo = tmp_path / "repo"
    (repo / "deploy").mkdir(parents=True)
    (repo / "clinostat_extension/work/static").mkdir(parents=True)
    shutil.copy2(SCRIPT, repo / "deploy/capture_pi_baseline.sh")
    shutil.copy2(HELPER, repo / "deploy/build_pi_baseline.py")
    sentinel = repo / "clinostat_extension/work/server.py"
    sentinel.write_text("unchanged\n")
    (repo / "clinostat_extension/work/static/index.html").write_text("unchanged\n")
    tools = tmp_path / "bin"
    tools.mkdir()

    _executable(tools / "ssh", """#!/usr/bin/env python3
import sys
command = sys.argv[-1]
if "git rev-parse" in command: print("a" * 40)
elif "openapi.json" in command: print('{"openapi":"3.1.0","paths":{"/api/control/status":{}}}')
elif "/api/control/status" in command: print('{"running":false,"loop_timing_ms":10.0}')
elif "find ." in command: print("./control_loop.py")
elif "test -f" in command: raise SystemExit(0 if "control_loop.py" in command else 1)
""")
    _executable(tools / "scp", """#!/usr/bin/env python3
import pathlib, sys
source, destination = sys.argv[-2:]
path = pathlib.Path(destination)
path.parent.mkdir(parents=True, exist_ok=True)
if source.endswith("server.py"):
    path.write_text("API_PASSWORD = 'leak'\\n@app.websocket('/ws/control')\\ndef ws(): pass\\n")
elif source.endswith("index.html"):
    path.write_text("<script>new WebSocket('/ws/control')</script>")
elif source.endswith("control_loop.py"):
    path.write_text("GEAR_RATIO=100.0\\nENCODER_PULSES_PER_REV=12\\nCONTROL_LOOP_INTERVAL_S=.1\\nMAX_MOTOR_RPM=120.0\\ndef motor_rpm(axis_rpm):\\n return axis_rpm * GEAR_RATIO\\n")
else: path.write_text("[Service]\\n")
""")
    _executable(tools / "websocat", """#!/usr/bin/env python3
print('{"running":false}')
print('{"running":false}')
print('{"running":false}', flush=True)
import time
time.sleep(3)
""")
    _executable(tools / "gitleaks", """#!/usr/bin/env python3
import pathlib, sys
root = pathlib.Path(sys.argv[sys.argv.index("--source") + 1])
found = any("API_PASSWORD" in p.read_text(errors="ignore") for p in root.rglob("*") if p.is_file())
if found: print("fixture scanner detected secret", file=sys.stderr)
raise SystemExit(1 if found else 0)
""")
    result = subprocess.run(
        ["bash", str(repo / "deploy/capture_pi_baseline.sh")],
        env={
            **os.environ, "PATH": f"{tools}:{os.environ['PATH']}",
            "PI_HOST": "fixture.invalid", "PI_USER": "fixture",
            "CAPTURE_TIMESTAMP": "secret-case",
            "CAPTURE_TEST_SHORT_WS": "1",
        },
        text=True, capture_output=True, timeout=15,
    )
    assert result.returncode != 0
    assert "fixture scanner detected secret" in result.stderr
    assert not (repo / "clinostat_extension/baseline/sanitized/secret-case").exists()
    assert sentinel.read_text() == "unchanged\n"


def _executable(path: Path, text: str) -> None:
    path.write_text(text)
    path.chmod(0o755)


def test_websocket_path_discovery_accepts_one_literal_route(tmp_path):
    (tmp_path / "server.py").write_text(
        "from fastapi import FastAPI, WebSocket\napp=FastAPI()\n"
        "@app.websocket('/ws')\nasync def stream(ws: WebSocket): pass\n"
    )
    (tmp_path / "index.html").write_text(
        "<script>new WebSocket(`ws://${location.host}/ws`)</script>"
    )
    assert builder.discover_websocket_path(
        tmp_path / "server.py", tmp_path / "index.html"
    ) == "/ws"


@pytest.mark.parametrize(
    "server,index",
    [
        ("app = object()\n", "<html></html>"),
        (
            "@app.websocket('/ws/a')\ndef a(): pass\n"
            "@app.websocket('/ws/b')\ndef b(): pass\n",
            "<script>new WebSocket('/ws/a');new WebSocket('/ws/b')</script>",
        ),
    ],
)
def test_websocket_path_discovery_rejects_missing_or_ambiguous(tmp_path, server, index):
    (tmp_path / "server.py").write_text(server)
    (tmp_path / "index.html").write_text(index)
    with pytest.raises(builder.ContractError, match="exactly one WebSocket path"):
        builder.discover_websocket_path(tmp_path / "server.py", tmp_path / "index.html")


def test_recursive_control_source_extracts_actual_symbols_and_goldens(tmp_path):
    source = tmp_path / "app/control/control_loop.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        "AC_GEAR_RATIO=200\nBLDC_GEAR_RATIO=20\n"
        "OUTER_MAX_OUTPUT_RPM=60.0\nINNER_MAX_OUTPUT_RPM=200.0\n"
        "OUTER_MAX_SLEW_RPM_S=50.0\nINNER_MAX_SLEW_RPM_S=50.0\n"
        "def _ac_feedback_output_rpm(motor_rpm):\n return motor_rpm / AC_GEAR_RATIO\n"
        "def _ac_command_motor_rpm(output_rpm):\n return output_rpm * AC_GEAR_RATIO\n"
        "def _slew_limit(current, target, max_delta):\n"
        " return max(current-max_delta, min(current+max_delta, target))\n"
    )
    constants = builder.extract_control_constants([source], root=tmp_path)
    assert constants["constants"] == {
        "AC_GEAR_RATIO": 200, "BLDC_GEAR_RATIO": 20,
        "INNER_MAX_OUTPUT_RPM": 200.0, "INNER_MAX_SLEW_RPM_S": 50.0,
        "OUTER_MAX_OUTPUT_RPM": 60.0, "OUTER_MAX_SLEW_RPM_S": 50.0,
    }
    assert all(item["source_file"] == "app/control/control_loop.py"
               for item in constants["mapping"].values())
    golden = builder.extract_control_golden([source], constants, root=tmp_path)
    assert {item["source"]["function"] for item in golden["calculations"]} == {
        "_ac_command_motor_rpm", "_slew_limit"
    }
    command = next(item for item in golden["calculations"]
                   if item["source"]["function"] == "_ac_command_motor_rpm")
    assert command["cases"][1] == {"input": {"output_rpm": 1.0}, "output": 200.0}


def test_control_extraction_fails_without_exact_pure_formula(tmp_path):
    source = tmp_path / "control_loop.py"
    source.write_text(
        "AC_GEAR_RATIO=200\nBLDC_GEAR_RATIO=20\n"
        "OUTER_MAX_OUTPUT_RPM=60.0\nINNER_MAX_OUTPUT_RPM=200.0\n"
        "OUTER_MAX_SLEW_RPM_S=50.0\nINNER_MAX_SLEW_RPM_S=50.0\n"
    )
    constants = builder.extract_control_constants([source])
    with pytest.raises(builder.ContractError, match="safe pure control calculation"):
        builder.extract_control_golden([source], constants)


def test_control_symbol_resolution_rejects_ambiguity_and_missing(tmp_path):
    first = tmp_path / "a.py"
    second = tmp_path / "nested/b.py"
    second.parent.mkdir()
    body = (
        "AC_GEAR_RATIO=200\nBLDC_GEAR_RATIO=20\n"
        "OUTER_MAX_OUTPUT_RPM=60.0\nINNER_MAX_OUTPUT_RPM=200.0\n"
        "OUTER_MAX_SLEW_RPM_S=50.0\nINNER_MAX_SLEW_RPM_S=50.0\n"
    )
    first.write_text(body)
    second.write_text("AC_GEAR_RATIO=201\n")
    with pytest.raises(builder.ContractError, match="ambiguous control constant"):
        builder.extract_control_constants([first, second], root=tmp_path)
    second.write_text("")
    first.write_text(body.replace("BLDC_GEAR_RATIO=20\n", ""))
    with pytest.raises(builder.ContractError, match="required real control constants absent"):
        builder.extract_control_constants([first, second], root=tmp_path)


def test_ws_duration_validation_rejects_early_exit_and_short_capture():
    with pytest.raises(builder.ContractError, match="exited before observation deadline"):
        builder.validate_ws_capture(
            elapsed_seconds=2.0, target_seconds=60.0, minimum_seconds=55.0,
            message_count=10, minimum_messages=3, exited_early=True,
        )
    with pytest.raises(builder.ContractError, match="shorter than required"):
        builder.validate_ws_capture(
            elapsed_seconds=54.9, target_seconds=60.0, minimum_seconds=55.0,
            message_count=10, minimum_messages=3, exited_early=False,
        )
    builder.validate_ws_capture(
        elapsed_seconds=55.0, target_seconds=60.0, minimum_seconds=55.0,
        message_count=3, minimum_messages=3, exited_early=False,
    )


def test_ws_supervisor_uses_injected_clock_and_rejects_early_normal_exit(tmp_path):
    class EarlyProcess:
        def poll(self): return 0

    clock = iter((0.0, 1.0, 1.0))
    with pytest.raises(builder.ContractError, match="exited before observation deadline"):
        builder.capture_websocket(
            tmp_path / "messages.jsonl", tmp_path / "metadata.json",
            "ws://fixture/ws", 60.0, 55.0, 3,
            _clock=lambda: next(clock), _sleep=lambda _seconds: None,
            _popen=lambda _args, stdout: EarlyProcess(),
        )
    assert not (tmp_path / "metadata.json").exists()


def test_ws_supervisor_records_injected_full_duration(tmp_path):
    class TimedProcess:
        def __init__(self, stream):
            stream.write('{"a":1}\n{"a":2}\n{"a":3}\n')
            self.running = True
        def poll(self): return None if self.running else -15
        def terminate(self): self.running = False
        def wait(self, timeout=None): return -15
        def kill(self): self.running = False

    clock = iter((0.0, 60.0, 60.001))
    metadata = tmp_path / "metadata.json"
    builder.capture_websocket(
        tmp_path / "messages.jsonl", metadata, "ws://fixture/ws", 60.0, 55.0, 3,
        _clock=lambda: next(clock), _sleep=lambda _seconds: None,
        _popen=lambda _args, stdout: TimedProcess(stdout),
    )
    recorded = json.loads(metadata.read_text())
    assert recorded["actual_duration_seconds"] == 60.001
    assert recorded["message_count"] == 3


def test_status_sanitization_requires_count_and_object_schema():
    good = [
        '{"running":false,"loop_timing_ms":10.2}',
        '{"running":false,"loop_timing_ms":10.3}',
        '{"running":false,"loop_timing_ms":10.1}',
    ]
    samples = builder.sanitize_status_samples(good, interval_seconds=1, minimum=3)
    assert len(samples) == 3
    assert samples[0]["offset_seconds"] == 0
    assert "running" in samples[0]["schema"]
    assert "10.2" not in json.dumps(samples)
    with pytest.raises(builder.ContractError, match="at least 3"):
        builder.sanitize_status_samples(good[:2], interval_seconds=1, minimum=3)
    with pytest.raises(builder.ContractError, match="JSON object"):
        builder.sanitize_status_samples(["[]"] * 3, interval_seconds=1, minimum=3)


def test_committed_fixture_models_known_contract():
    capture = ROOT / "clinostat_extension/baseline/sanitized/fixture-20260724"
    openapi = json.loads((capture / "openapi.json").read_text())
    assert "/api/control/status" in openapi["paths"]
    assert "/api/status" not in openapi["paths"]
    ws = json.loads((capture / "websocket_schema.json").read_text())
    assert ws["path"] == "/ws"
    assert ws["actual_duration_seconds"] >= ws["minimum_duration_seconds"]
    constants = json.loads((capture / "control_constants.json").read_text())
    assert {
        "AC_GEAR_RATIO", "BLDC_GEAR_RATIO", "OUTER_MAX_OUTPUT_RPM",
        "INNER_MAX_OUTPUT_RPM", "OUTER_MAX_SLEW_RPM_S", "INNER_MAX_SLEW_RPM_S",
    } == set(constants["constants"])
    assert constants["constants"]["AC_GEAR_RATIO"] == 200
    assert not {"TARGET_RPM", "KP", "KI"} & set(constants["constants"])
    golden = json.loads((capture / "control_golden.json").read_text())
    assert {c["source"]["function"] for c in golden["calculations"]} == {
        "_ac_command_motor_rpm", "_slew_limit"
    }
    samples = (capture / "status_samples.sanitized.jsonl").read_text().splitlines()
    assert len(samples) >= 3
    assert json.loads((capture / "capture.json").read_text())["status_minimum_samples"] == 3


def test_editable_work_copies_match_captured_sources():
    capture = ROOT / "clinostat_extension/baseline/sanitized/fixture-20260724"
    assert (ROOT / "clinostat_extension/work/server.py").read_bytes() == (
        capture / "server.py").read_bytes()
    assert (ROOT / "clinostat_extension/work/static/index.html").read_bytes() == (
        capture / "static/index.html").read_bytes()
