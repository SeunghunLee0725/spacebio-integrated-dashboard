"""프록시의 기록 관리 통로 — 목록/삭제 중계와 아카이브 스트리밍.

`ROUTE_MAP`은 정확 일치 허용목록이라 경로에 `session_id`가 들어가는 다운로드·
삭제를 담지 못한다. 그래서 `session_id`를 검증해 경로를 **조립**하는 별도
함수를 둔다 — 브라우저가 준 문자열을 그대로 이어붙이면 안 된다.
"""

from __future__ import annotations

import sys
from pathlib import Path

import httpx
import pytest

WORK = Path(__file__).resolve().parents[1] / "work"
if str(WORK) not in sys.path:
    sys.path.insert(0, str(WORK))

import spacebio_proxy  # noqa: E402
from spacebio_proxy import (  # noqa: E402
    ProxyGatewayError,
    ProxyPathError,
    SpaceBioProxy,
    archive_gateway_path,
)

VALID_ID = "spacebio_20260729_221916_resistancerun"


def _proxy(handler) -> SpaceBioProxy:
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8010")
    return SpaceBioProxy(client)


# ─────────────────────────── 경로 조립 ───────────────────────────

def test_archive_path_is_built_from_a_validated_session_id():
    assert archive_gateway_path(VALID_ID) == f"/api/sessions/{VALID_ID}"
    assert archive_gateway_path(VALID_ID, download=True) == f"/api/sessions/{VALID_ID}/download"


@pytest.mark.parametrize("session_id", [
    "../../../etc/passwd",
    "spacebio_20260729_221916_ok/../../escape",
    "not-a-session",
    "",
    "spacebio_20260729_221916_ok?x=1",
    "spacebio_20260729_221916_ok#frag",
])
def test_archive_path_refuses_anything_that_is_not_a_session_id(session_id: str):
    with pytest.raises(ProxyPathError):
        archive_gateway_path(session_id)


def test_session_list_is_on_the_allowlist():
    assert spacebio_proxy.gateway_path("/api/spacebio/sessions") == "/api/sessions"


# ─────────────────────────── 삭제 중계 ───────────────────────────

@pytest.mark.asyncio
async def test_delete_relays_the_method_and_request_id():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["url"] = str(request.url)
        seen["rid"] = request.headers.get("X-Request-ID")
        return httpx.Response(200, json={"data": {"deleted": True}})

    result = await _proxy(handler).delete_session(VALID_ID, "rid-1")

    assert seen["method"] == "DELETE"
    assert seen["url"].endswith(f"/api/sessions/{VALID_ID}")
    assert seen["rid"] == "rid-1"
    assert result == {"data": {"deleted": True}}


@pytest.mark.asyncio
async def test_delete_does_not_retry():
    """삭제는 비가역이다 — 전송이 실패해도 다시 보내지 않는다."""
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        raise httpx.ConnectError("boom")

    with pytest.raises(ProxyGatewayError) as excinfo:
        await _proxy(handler).delete_session(VALID_ID, "rid-1")

    assert len(calls) == 1
    assert excinfo.value.status_code == 504


@pytest.mark.asyncio
async def test_delete_preserves_the_gateway_status_code():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"error": {"code": "session_active", "message": "no"}})

    with pytest.raises(ProxyGatewayError) as excinfo:
        await _proxy(handler).delete_session(VALID_ID, "rid-1")

    assert excinfo.value.status_code == 409
    assert excinfo.value.error["code"] == "session_active"


@pytest.mark.asyncio
async def test_delete_rejects_a_bad_id_before_touching_the_gateway():
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("gateway must not be called")

    with pytest.raises(ProxyPathError):
        await _proxy(handler).delete_session("../../etc", "rid-1")


# ─────────────────────────── 아카이브 스트리밍 ───────────────────────────

@pytest.mark.asyncio
async def test_archive_streams_the_body_and_forwards_download_headers():
    payload = b"\x1f\x8b" + b"x" * 4096

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/api/sessions/{VALID_ID}/download"
        return httpx.Response(
            200, content=payload,
            headers={
                "Content-Type": "application/gzip",
                "Content-Length": str(len(payload)),
                "Content-Disposition": f'attachment; filename="{VALID_ID}.tar.gz"',
                "X-Internal-Secret": "must-not-leak",
            },
        )

    async with _proxy(handler).open_archive(VALID_ID, "rid-1") as archive:
        body = b"".join([chunk async for chunk in archive.chunks])

    assert body == payload
    assert archive.media_type == "application/gzip"
    assert archive.headers["Content-Length"] == str(len(payload))
    assert f'filename="{VALID_ID}.tar.gz"' in archive.headers["Content-Disposition"]
    # 게이트웨이 헤더를 통째로 흘려보내지 않는다 — 허용한 것만 넘긴다.
    assert "X-Internal-Secret" not in archive.headers


@pytest.mark.asyncio
async def test_archive_maps_a_gateway_error_to_a_proxy_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": {"code": "not_found", "message": "nope"}})

    with pytest.raises(ProxyGatewayError) as excinfo:
        async with _proxy(handler).open_archive(VALID_ID, "rid-1"):
            pass  # pragma: no cover

    assert excinfo.value.status_code == 404
    assert excinfo.value.error["code"] == "not_found"


@pytest.mark.asyncio
async def test_archive_maps_an_unreachable_gateway_to_504():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    with pytest.raises(ProxyGatewayError) as excinfo:
        async with _proxy(handler).open_archive(VALID_ID, "rid-1"):
            pass  # pragma: no cover

    assert excinfo.value.status_code == 504
