"""데이터셋 목록 + 펌프 스텝 프록시 계약 (실기 배포 결함 수정).

`resistanceDataset` 드롭다운이 비어 CSV 모드를 쓸 수 없었던 결함과, 실기 스텝
펌프 제어가 없던 공백을 메우는 두 라우트를 검증한다. test_spacebio_proxy.py와
같은 방식으로 httpx.MockTransport로 Gateway를 흉내낸다.
"""

from __future__ import annotations

import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "work"))

import spacebio_proxy as sp  # noqa: E402


def _envelope(data, request_id="rid"):
    return {"schema_version": 1, "request_id": request_id,
            "server_time": "2026-07-24T10:00:00+09:00", "data": data}


def _proxy(handler, **kwargs):
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url=sp.GATEWAY_BASE_URL,
    )
    return sp.SpaceBioProxy(client=client, **kwargs)


# ─────────────────────────── 경로 계약 ───────────────────────────

def test_dataset_and_pump_step_routes_are_allowlisted():
    assert sp.gateway_path("/api/spacebio/sensor/datasets") == "/api/sensor/datasets"
    assert sp.gateway_path("/api/spacebio/pump/step") == "/api/pump/step"


# ─────────────────────────── 데이터셋 목록 ───────────────────────────

@pytest.mark.asyncio
async def test_dataset_list_is_forwarded_as_a_get():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/api/sensor/datasets"
        return httpx.Response(200, json=_envelope({"datasets": [
            {"dataset_id": "muscle_baseline_01", "sample_count": 4200,
             "provenance": "ground_truth"},
        ]}))

    result = await _proxy(handler).get("/api/spacebio/sensor/datasets", request_id="R")
    datasets = result["data"]["datasets"]
    assert datasets[0]["dataset_id"] == "muscle_baseline_01"
    assert datasets[0]["sample_count"] == 4200


# ─────────────────────────── 펌프 스텝 ───────────────────────────

@pytest.mark.asyncio
async def test_pump_step_posts_steps_and_spm():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["body"] = request.read().decode()
        return httpx.Response(200, json=_envelope({"state": "stepping", "position_steps": 200}))

    result = await _proxy(handler).mutate(
        "/api/spacebio/pump/step", {"steps": 200, "spm": 600}, request_id="R",
    )
    assert seen["path"] == "/api/pump/step"
    assert "200" in seen["body"] and "600" in seen["body"]
    assert result["data"]["position_steps"] == 200


@pytest.mark.asyncio
async def test_pump_step_errors_are_translated_not_auto_retried():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(422, json={
            "schema_version": 1, "request_id": "R",
            "server_time": "2026-07-24T10:00:00+09:00",
            "error": {"code": "validation_error", "message": "spm out of range"},
        })

    with pytest.raises(sp.ProxyGatewayError) as exc:
        await _proxy(handler).mutate(
            "/api/spacebio/pump/step", {"steps": 200, "spm": 9999}, request_id="R",
        )
    assert calls["n"] == 1, "변경 요청은 재시도하지 않는다"
    assert exc.value.status_code == 422
    assert exc.value.error["code"] == "validation_error"
