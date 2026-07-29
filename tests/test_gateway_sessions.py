"""기록 관리 REST — `GET /api/sessions`, `.../download`, `DELETE /api/sessions/{id}`.

다운로드만 envelope가 아니다(파일이다). 나머지는 기존 계약대로 envelope를 쓴다.
"""

from __future__ import annotations

import io
import tarfile

import pytest
from fastapi.testclient import TestClient

from gateway.app import create_app
from tests.conftest import make_gw_config

SESSION_ID = "spacebio_20260724_120000_ab12"


def _start_payload(session_id: str = SESSION_ID, request_id: str = "s-1") -> dict:
    return {
        "request_id": request_id, "session_id": session_id,
        "experiment_name": "archive-test", "started_at": "2026-07-24T12:00:00+09:00",
    }


def _record_session(client: TestClient, session_id: str = SESSION_ID) -> None:
    """세션을 하나 만들고 바로 끝낸다 — 디스크에 폴더가 남는다."""
    assert client.post("/api/session/start", json=_start_payload(session_id)).status_code == 200
    finish = client.post("/api/session/finish", json={
        "request_id": f"f-{session_id}", "session_id": session_id,
        "finished_at": "2026-07-24T12:05:00+09:00",
    })
    assert finish.status_code == 200


# ─────────────────────────── 목록 ───────────────────────────

def test_list_is_empty_before_any_session(client: TestClient):
    resp = client.get("/api/sessions")
    assert resp.status_code == 200
    body = resp.json()
    assert "error" not in body
    assert body["data"]["sessions"] == []


def test_list_reports_a_finished_session(client: TestClient):
    _record_session(client)

    (found,) = client.get("/api/sessions").json()["data"]["sessions"]

    assert found["session_id"] == SESSION_ID
    assert found["experiment_name"] == "archive-test"
    assert found["status"] == "completed"
    assert found["active"] is False
    assert found["bytes"] > 0


def test_list_marks_the_session_that_is_still_recording(client: TestClient):
    assert client.post("/api/session/start", json=_start_payload()).status_code == 200

    (found,) = client.get("/api/sessions").json()["data"]["sessions"]

    assert found["active"] is True


# ─────────────────────────── 다운로드 ───────────────────────────

def test_download_returns_a_tar_gz_named_after_the_session(client: TestClient):
    _record_session(client)

    resp = client.get(f"/api/sessions/{SESSION_ID}/download")

    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/gzip"
    assert f'filename="{SESSION_ID}.tar.gz"' in resp.headers["content-disposition"]
    with tarfile.open(fileobj=io.BytesIO(resp.content), mode="r:gz") as tar:
        assert f"{SESSION_ID}/manifest.json" in tar.getnames()


def test_download_sets_content_length_so_the_browser_can_show_progress(client: TestClient):
    _record_session(client)

    resp = client.get(f"/api/sessions/{SESSION_ID}/download")

    assert int(resp.headers["content-length"]) == len(resp.content)


def test_download_of_a_missing_session_is_404(client: TestClient):
    resp = client.get(f"/api/sessions/{SESSION_ID}/download")

    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"


@pytest.mark.parametrize("session_id", ["not-a-session", "spacebio_1_1_x"])
def test_download_rejects_a_malformed_session_id(client: TestClient, session_id: str):
    assert client.get(f"/api/sessions/{session_id}/download").status_code == 404


# ─────────────────────────── 삭제 ───────────────────────────

def test_delete_removes_the_session_and_it_leaves_the_list(client: TestClient):
    _record_session(client)

    resp = client.request("DELETE", f"/api/sessions/{SESSION_ID}")

    assert resp.status_code == 200
    assert resp.json()["data"] == {"session_id": SESSION_ID, "deleted": True}
    assert client.get("/api/sessions").json()["data"]["sessions"] == []


def test_delete_refuses_the_session_that_is_still_recording(client: TestClient):
    assert client.post("/api/session/start", json=_start_payload()).status_code == 200

    resp = client.request("DELETE", f"/api/sessions/{SESSION_ID}")

    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "session_active"
    assert client.get("/api/sessions").json()["data"]["sessions"] != []


def test_delete_of_a_missing_session_is_404(client: TestClient):
    resp = client.request("DELETE", f"/api/sessions/{SESSION_ID}")

    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"


@pytest.mark.parametrize("session_id", ["not-a-session", "..", "spacebio_1_1_x"])
def test_delete_rejects_a_malformed_session_id(client: TestClient, session_id: str):
    assert client.request("DELETE", f"/api/sessions/{session_id}").status_code in (404, 405)


def test_delete_cannot_reach_outside_the_data_root(tmp_path):
    """경로 탈출 시도가 data_root 밖 파일을 건드리면 안 된다."""
    outside = tmp_path / "precious.txt"
    outside.write_text("keep me", encoding="utf-8")
    app = create_app(make_gw_config(tmp_path))

    with TestClient(app) as client:
        client.request("DELETE", "/api/sessions/..%2F..%2Fprecious.txt")

    assert outside.exists()
