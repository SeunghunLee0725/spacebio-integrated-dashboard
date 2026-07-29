"""request_id 결과 캐시 — 24시간 보존 (설계 스펙 6.4 멱등성).

같은 request_id의 재요청은 저장된 원 응답을 반환하고 새 실행을 만들지 않는다.
만료 후에는 캐시가 비어 있는 것처럼 동작해 새 실행을 허용한다.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

TTL_SECONDS = 24 * 60 * 60


@dataclass(frozen=True)
class _CacheEntry:
    stored_at_s: float
    value: Any


class IdempotencyCache:
    """request_id -> 원 응답 캐시. 시각 소스는 주입 가능 (테스트에서 가짜 clock 사용)."""

    def __init__(self, *, ttl_s: float = TTL_SECONDS,
                 now_fn: Callable[[], float] = time.time) -> None:
        self._ttl_s = ttl_s
        self._now_fn = now_fn
        self._entries: dict[str, _CacheEntry] = {}

    def get(self, request_id: str) -> Optional[Any]:
        entry = self._entries.get(request_id)
        if entry is None:
            return None
        if self._now_fn() - entry.stored_at_s > self._ttl_s:
            del self._entries[request_id]
            return None
        return entry.value

    def set(self, request_id: str, value: Any) -> None:
        self._entries[request_id] = _CacheEntry(stored_at_s=self._now_fn(), value=value)
