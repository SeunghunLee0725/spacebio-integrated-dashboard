import json
import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "deploy" / "capture_pi_baseline.sh"


def test_capture_requires_pi_identity():
    result = subprocess.run(
        ["bash", str(SCRIPT)],
        cwd=ROOT,
        env={"PATH": os.environ["PATH"]},
        text=True,
        capture_output=True,
    )
    assert result.returncode != 0
    assert "PI_HOST and PI_USER are required" in result.stderr


def test_capture_script_is_read_only_and_pins_host_key_checking():
    text = SCRIPT.read_text()
    assert "StrictHostKeyChecking=yes" in text
    forbidden = ("systemctl start", "systemctl restart", "sudo ", "kill ", "reboot", "gpio")
    assert not any(command in text for command in forbidden)


def test_sanitized_contract_artifacts_are_semantic_and_safe():
    baseline = ROOT / "clinostat_extension" / "baseline" / "sanitized"
    captures = sorted(path for path in baseline.iterdir() if path.is_dir())
    assert captures, "a sanitized baseline capture is required"
    capture = captures[-1]

    allowed = {
        "capture.json",
        "openapi.json",
        "dom_ids.json",
        "websocket_schema.json",
        "control_constants.json",
        "control_golden.json",
        "status_samples.sanitized.jsonl",
        "sha256sums.txt",
        "server.py",
        "static",
        "clinostat.service",
    }
    assert {path.name for path in capture.iterdir()} == allowed
    assert {path.name for path in (capture / "static").iterdir()} == {"index.html"}

    metadata = json.loads((capture / "capture.json").read_text())
    assert metadata["remote_actions"] == "read-only"
    assert metadata["source_path"] == "/home/aiworker-1/clinostat"
    assert "password" not in json.dumps(metadata).lower()

    openapi = json.loads((capture / "openapi.json").read_text())
    assert openapi["openapi"].startswith("3.")
    paths = set(openapi["paths"])
    assert {"/", "/api/status", "/ws"}.issubset(paths)

    dom = json.loads((capture / "dom_ids.json").read_text())
    assert all(isinstance(count, int) and count > 0 for count in dom["ids"].values())
    assert {"connectBtn", "status"}.issubset(dom["ids"])

    ws = json.loads((capture / "websocket_schema.json").read_text())
    assert ws["sample_window_seconds"] <= 60
    assert ws["hardware_started"] is False
    assert {"timestamp", "connected"}.issubset(ws["properties"])
    for spec in ws["properties"].values():
        assert set(spec) == {"types", "nullable"}
        assert spec["types"]
        assert all(t in {"boolean", "integer", "number", "string", "array", "object"} for t in spec["types"])
        assert isinstance(spec["nullable"], bool)

    constants = json.loads((capture / "control_constants.json").read_text())
    assert constants["extraction"] == "python-ast-explicit-allowlist"
    assert {"TARGET_RPM", "KP", "KI"}.issubset(constants["constants"])
    assert all(
        isinstance(value, (int, float)) and not isinstance(value, bool)
        for value in constants["constants"].values()
    )

    golden = json.loads((capture / "control_golden.json").read_text())
    assert golden["hardware_imported"] is False
    assert golden["constructor_strategy"] == "ast-pure-calculation"
    assert len(golden["cases"]) >= 3
    assert all({"input", "output"} <= case.keys() for case in golden["cases"])

    samples = [
        json.loads(line)
        for line in (capture / "status_samples.sanitized.jsonl").read_text().splitlines()
        if line
    ]
    assert samples
    assert all(set(sample) <= {"offset_seconds", "schema", "loop_timing"} for sample in samples)
    assert all("value" not in json.dumps(sample).lower() for sample in samples)

    combined = "\n".join(
        path.read_text(errors="replace")
        for path in capture.rglob("*")
        if path.is_file()
    )
    forbidden = (
        "ghp_",
        "github_pat_",
        "authorization:",
        "set-cookie:",
        "BEGIN OPENSSH PRIVATE KEY",
        "://user:password@",
        "server.log",
    )
    assert not any(token.lower() in combined.lower() for token in forbidden)


def test_editable_work_copies_match_captured_sources():
    baseline = sorted(
        path
        for path in (ROOT / "clinostat_extension" / "baseline" / "sanitized").iterdir()
        if path.is_dir()
    )[-1]
    assert (ROOT / "clinostat_extension" / "work" / "server.py").read_bytes() == (
        baseline / "server.py"
    ).read_bytes()
    assert (ROOT / "clinostat_extension" / "work" / "static" / "index.html").read_bytes() == (
        baseline / "static" / "index.html"
    ).read_bytes()
    assert "baseline" in (ROOT / "clinostat_extension" / "README.md").read_text().lower()
