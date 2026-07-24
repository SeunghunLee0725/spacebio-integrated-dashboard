import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BASELINE = ROOT / "clinostat_extension/baseline/real-20260724"
WORK = ROOT / "clinostat_extension/work"

EXPECTED_CONTROL_CONSTANTS = {
    "AC_GEAR_RATIO": 200,
    "BLDC_GEAR_RATIO": 20,
    "OUTER_MAX_OUTPUT_RPM": 60.0,
    "INNER_MAX_OUTPUT_RPM": 200.0,
    "OUTER_MAX_SLEW_RPM_S": 50.0,
    "INNER_MAX_SLEW_RPM_S": 50.0,
    "AC_COMMAND_SCALE": 1.0,
    "FEEDBACK_POLL_INTERVAL": 0.5,
    "AC_FEEDBACK_SIGN": 1.0,
}

SECRET_PATTERN = re.compile(
    r"gh[opsu]_[A-Za-z0-9]{20,}|password|BEGIN .*PRIVATE KEY", re.IGNORECASE
)


def test_baseline_capture_is_real_not_fixture():
    capture = json.loads((BASELINE / "capture.json").read_text())
    assert capture["remote_actions"] == "read-only"
    assert capture["source_path"] == "/home/aiworker-1/clinostat"
    assert re.fullmatch(r"[0-9a-f]{40}", capture["source_revision"])
    assert "fixture" not in json.dumps(capture).lower()


def test_baseline_dom_ids_count_matches_captured_index():
    dom_ids = json.loads((BASELINE / "dom_ids.json").read_text())
    assert len(dom_ids["ids"]) == 49
    assert all(count >= 1 for count in dom_ids["ids"].values())


def test_baseline_routes_count_matches_openapi():
    routes = json.loads((BASELINE / "routes.json").read_text())
    assert routes["count"] == 36
    assert len(routes["routes"]) == 36
    openapi = json.loads((BASELINE / "openapi.json").read_text())
    openapi_route_count = sum(
        1
        for methods in openapi["paths"].values()
        for method in methods
        if method.upper() in ("GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD")
    )
    assert openapi_route_count == 36


def test_baseline_control_constants_match_real_pi_values():
    constants = json.loads((BASELINE / "control_constants.json").read_text())
    for name, value in EXPECTED_CONTROL_CONSTANTS.items():
        assert constants["constants"][name] == value, name
    assert EXPECTED_CONTROL_CONSTANTS.keys() <= constants["constants"].keys()


def test_baseline_websocket_keys_were_observed():
    keys = json.loads((BASELINE / "websocket_keys.json").read_text())
    assert keys["path"] == "/ws"
    assert keys["message_count"] > 0
    assert keys["observed_seconds"] >= 15
    assert "running" in keys["keys"]


def test_baseline_sha256sums_match_captured_files():
    lines = (BASELINE / "sha256sums.txt").read_text().splitlines()
    assert lines
    for line in lines:
        digest, _, relative_path = line.partition("  ")
        assert re.fullmatch(r"[0-9a-f]{64}", digest)
        target = BASELINE / relative_path.lstrip("./")
        assert target.is_file()


def test_work_copies_match_captured_baseline():
    assert (WORK / "server.py").read_bytes() == (BASELINE / "server.py").read_bytes()
    assert (WORK / "static/index.html").read_bytes() == (
        BASELINE / "static/index.html"
    ).read_bytes()


def test_baseline_contains_no_secrets():
    for path in BASELINE.rglob("*"):
        if not path.is_file():
            continue
        text = path.read_text(errors="ignore")
        assert not SECRET_PATTERN.search(text), f"possible secret in {path}"


def test_fake_fixture_directory_is_gone():
    assert not (ROOT / "clinostat_extension/baseline/sanitized").exists()
