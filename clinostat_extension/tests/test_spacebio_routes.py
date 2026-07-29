"""work/server.py에 등록된 SpaceBio 프록시 라우트 검증 (설계 스펙 6.1).

`work/server.py`는 파이 전용 모듈(camera, modbus_rtu, experiment 등)을 import하므로
개발 머신에서 실행할 수 없다. 따라서 소스 정적 검증으로 계약을 못박는다.
런타임 검증은 파이 배포 후 `deploy/verify_pi.sh`가 OpenAPI를 대조해 수행한다.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

WORK = Path(__file__).resolve().parents[1] / "work"
SERVER = WORK / "server.py"
BASELINE = Path(__file__).resolve().parents[1] / "baseline" / "real-20260724" / "server.py"


@pytest.fixture(scope="module")
def source() -> str:
    return SERVER.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def tree(source):
    return ast.parse(source)


def _decorated_routes(tree) -> set[tuple[str, str]]:
    """(method, path) 집합을 AST에서 뽑는다 — 문자열 검색보다 오탐이 적다."""
    routes = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for deco in node.decorator_list:
            if not isinstance(deco, ast.Call) or not isinstance(deco.func, ast.Attribute):
                continue
            target = deco.func.value
            if not (isinstance(target, ast.Name) and target.id == "app"):
                continue
            if deco.args and isinstance(deco.args[0], ast.Constant):
                routes.add((deco.func.attr, deco.args[0].value))
    return routes


# ─────────────────────────── 기존 라우트 보존 ───────────────────────────

def test_every_baseline_route_survives(tree):
    baseline_routes = _decorated_routes(ast.parse(BASELINE.read_text(encoding="utf-8")))
    assert baseline_routes, "baseline에서 라우트를 못 뽑았다"
    missing = baseline_routes - _decorated_routes(tree)
    assert not missing, f"사라진 기존 라우트: {sorted(missing)}"


def test_existing_websocket_and_control_routes_intact(tree):
    routes = _decorated_routes(tree)
    assert ("websocket", "/ws") in routes
    assert ("post", "/api/control/stop") in routes
    assert ("get", "/api/control/status") in routes


# ─────────────────────────── 신규 프록시 라우트 ───────────────────────────

EXPECTED_PROXY_ROUTES = {
    ("get", "/api/spacebio/status"),
    ("get", "/api/spacebio/sensor/status"),
    ("get", "/api/spacebio/pump/status"),
    ("get", "/api/spacebio/session/status"),
    ("get", "/api/spacebio/sensor/datasets"),
    ("post", "/api/spacebio/sensor/configure"),
    ("post", "/api/spacebio/sensor/start"),
    ("post", "/api/spacebio/sensor/stop"),
    ("post", "/api/spacebio/pump/dispense"),
    ("post", "/api/spacebio/pump/stop"),
    ("post", "/api/spacebio/pump/step"),
    ("post", "/api/spacebio/pump/emergency-stop"),
    ("post", "/api/spacebio/pump/reset-emergency-stop"),
    ("post", "/api/spacebio/session/start"),
    ("post", "/api/spacebio/session/update"),
    ("post", "/api/spacebio/session/finish"),
    ("websocket", "/ws/spacebio"),
}


@pytest.mark.parametrize("method,path", sorted(EXPECTED_PROXY_ROUTES))
def test_proxy_route_is_registered(tree, method, path):
    assert (method, path) in _decorated_routes(tree)


def test_browser_routes_match_the_panel_and_proxy_allowlist(tree):
    """HTML이 부르는 경로, server.py가 등록한 경로, 프록시 허용목록이 일치해야 한다."""
    import sys
    sys.path.insert(0, str(WORK))
    import spacebio_proxy

    registered = {p for _m, p in _decorated_routes(tree) if p.startswith("/api/spacebio/")}
    # 기록 관리의 다운로드·삭제만 경로에 session_id가 들어가 정확 일치 허용목록에
    # 담기지 않는다. 대신 `archive_gateway_path`가 session_id를 검증해 경로를
    # 조립한다 — 아래에서 그 검증이 실제로 있는지 확인한다.
    templated = {p for p in registered if "{" in p}
    assert templated == {
        "/api/spacebio/sessions/{session_id}",
        "/api/spacebio/sessions/{session_id}/download",
    }, "새 파라미터 경로를 열었다면 session_id 검증 경로를 함께 확인해야 한다"
    with pytest.raises(spacebio_proxy.ProxyPathError):
        spacebio_proxy.archive_gateway_path("../../etc/passwd")

    assert (registered - templated) <= set(spacebio_proxy.ROUTE_MAP), \
        "프록시 허용목록에 없는 경로를 등록했다"

    html = (WORK / "static" / "index.html").read_text(encoding="utf-8")
    called = set(re.findall(r"'(/api/spacebio/[a-z\-/]+)'", html))
    assert called <= registered, f"HTML이 부르는데 등록 안 된 경로: {called - registered}"


# ─────────────────────────── 안전 경계 ───────────────────────────

def test_proxy_client_is_created_and_closed_in_lifespan(source):
    """단일 httpx.AsyncClient를 lifespan이 소유한다 — 요청마다 만들지 않는다."""
    body = source[source.index("async def lifespan("):]
    body = body[:body.index("\napp = FastAPI")]
    assert "AsyncClient" in body
    assert "aclose" in body or "await" in body
    # 기존 정리 코드가 그대로 남아 있어야 한다
    for existing in ("control.stop()", "camera.stop()"):
        assert existing in body, f"기존 종료 정리 {existing}가 사라졌다"


def test_gateway_address_is_not_hardcoded_in_server(source):
    """주소는 spacebio_proxy가 단독으로 소유한다."""
    assert "8010" not in source


def test_no_blocking_http_inside_control_paths(source):
    """제어 경로에서 동기 HTTP를 부르면 제어 루프가 Gateway에 묶인다."""
    assert "requests.get" not in source
    assert "requests.post" not in source
    assert "httpx.get(" not in source
    assert "httpx.post(" not in source


def test_proxy_errors_are_translated_to_http_responses(source):
    assert "ProxyGatewayError" in source
    assert "ProxyPathError" in source or "gateway_path" in source


def test_stop_all_helper_is_available_to_the_browser(source):
    """브라우저 STOP ALL이 쓰는 펌프 정지 경로가 등록돼 있어야 한다."""
    assert "/api/spacebio/pump/stop" in source
