"""dataset 목록 엔드포인트 (화면의 CSV 모드가 이것 없이는 동작하지 않는다).

패널의 데이터셋 드롭다운은 이 목록으로 채워진다. 목록이 없으면 운영자가
dataset_id를 알 방법이 없어 CSV 재생을 아예 시작할 수 없다.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

WORK = Path(__file__).resolve().parents[1] / "clinostat_extension" / "work"


def test_gateway_exposes_dataset_list(client):
    body = client.get("/api/sensor/datasets").json()
    assert body["schema_version"] == 1
    datasets = body["data"]["datasets"]
    assert datasets, "등록된 dataset이 하나도 없다"
    entry = datasets[0]
    assert {"dataset_id", "sample_count", "provenance"} <= set(entry)


def test_dataset_list_matches_the_manifest(client):
    manifest = json.loads(
        (Path(__file__).resolve().parents[1] / "datasets" / "manifest.json")
        .read_text(encoding="utf-8")
    )
    expected = {d["dataset_id"] for d in manifest["datasets"]}
    live = {d["dataset_id"] for d in client.get("/api/sensor/datasets").json()["data"]["datasets"]}
    assert live == expected


def test_dataset_list_never_leaks_filesystem_paths(client):
    """dataset_id는 allowlist 키다. 경로를 노출하면 traversal 표면이 생긴다."""
    raw = client.get("/api/sensor/datasets").text
    assert "/home/" not in raw
    assert "datasets_dir" not in raw
    for entry in client.get("/api/sensor/datasets").json()["data"]["datasets"]:
        assert "filename" not in entry
        assert "path" not in entry


def test_proxy_allowlists_the_dataset_route():
    import sys
    sys.path.insert(0, str(WORK))
    import spacebio_proxy

    assert "/api/spacebio/sensor/datasets" in spacebio_proxy.ROUTE_MAP
    assert spacebio_proxy.ROUTE_MAP["/api/spacebio/sensor/datasets"] == "/api/sensor/datasets"


def test_clinostat_registers_the_dataset_proxy_route():
    source = (WORK / "server.py").read_text(encoding="utf-8")
    assert '"/api/spacebio/sensor/datasets"' in source


def test_panel_populates_the_dataset_dropdown():
    """빈 드롭다운은 CSV 모드를 쓸 수 없게 만든다 — 실제로 그렇게 배포됐었다."""
    html = (WORK / "static" / "index.html").read_text(encoding="utf-8")
    assert "spacebioLoadDatasets" in html
    section = html[html.index("async function spacebioLoadDatasets"):][:900]
    assert "/api/spacebio/sensor/datasets" in section
    assert "resistanceDataset" in section
    assert re.search(r"createElement\(['\"]option['\"]\)|innerHTML|add\(", section), \
        "option을 실제로 채우는 코드가 없다"


# ─────────────────────────── 실기 배선 (2026-07-25) ───────────────────────────

def test_serial_live_configure_is_accepted(client, monkeypatch):
    """SERIAL_LIVE 설정이 판별 유니온을 통과한다(포트 없이도 configure까지는 됨)."""
    import gateway.runtime as rt

    class _FakeSerialSource:
        def __init__(self, *a, **k): ...
        def start(self): ...
        def tick(self): return None
    monkeypatch.setattr(rt, "SerialSensorSource", _FakeSerialSource)
    data = client.post("/api/sensor/configure", json={"mode": "SERIAL_LIVE"}).json()["data"]
    assert data["mode"] == "SERIAL_LIVE"


def test_pump_step_on_simulated_backend_is_409(client):
    """모의 펌프에는 스텝 개념이 없다 — 실기 백엔드에서만 동작."""
    r = client.post("/api/pump/step", json={"steps": 200, "spm": 600})
    assert r.status_code == 409


def test_pump_step_out_of_range_is_422(client):
    assert client.post("/api/pump/step", json={"steps": 200, "spm": 9999}).status_code == 422


def test_pump_step_works_on_wireless_backend(tmp_path):
    """실기 백엔드면 스텝이 MQTT로 발행되고 상태에 스텝 텔레메트리가 실린다."""
    from fastapi.testclient import TestClient
    from gateway.app import create_app
    from tests.conftest import make_gw_config
    import gateway.runtime as rt

    class FakeClient:
        def __init__(self): self.published = []; self.on_message = None
        def username_pw_set(self, *a, **k): ...
        def connect(self, *a, **k): ...
        def subscribe(self, *a, **k): ...
        def loop_start(self): ...
        def loop_stop(self): ...
        def disconnect(self): ...
        def publish(self, t, p): self.published.append((t, p))

    fake = FakeClient()
    real_init = rt.MqttPump.__init__

    def patched_init(self, **kwargs):
        kwargs["client_factory"] = lambda: fake
        real_init(self, **kwargs)
    rt.MqttPump.__init__ = patched_init
    try:
        cfg = make_gw_config(tmp_path, pump_backend="wireless")
        app = create_app(cfg)
        with TestClient(app) as c:
            data = c.post("/api/pump/step", json={"steps": 200, "spm": 600}).json()["data"]
            assert data["mode"] == "WIRELESS"
        assert ("s25007/board1/cmd", "manualstep:200") in fake.published
    finally:
        rt.MqttPump.__init__ = real_init
