"""HTTP 계층 공용 헬퍼 — request_id 추출과 success envelope 조립 (설계 스펙 6.1).

`X-Request-ID` 헤더는 선택이다: 없으면 서버가 UUID를 새로 만든다. 이 request_id는
응답 envelope 전용이며, 요청 본문의 `request_id`(멱등성 키)와는 별개다.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import Request

from gateway.api_models import success_envelope


def request_id(request: Request) -> str:
    return request.headers.get("x-request-id") or str(uuid.uuid4())


def envelope(request: Request, data: Any) -> dict[str, Any]:
    return success_envelope(request_id=request_id(request), data=data)
