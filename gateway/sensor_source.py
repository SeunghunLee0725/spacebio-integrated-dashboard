"""CSV 재생과 합성 신호가 공유하는 센서 소스 인터페이스 (설계 스펙 6.3/6.7).

두 구현 모두 `time.monotonic` 계열 clock을 주입받아 폴링 방식으로 동작한다.
매 tick 호출은 clock을 한 번 읽어 새 샘플이 준비됐으면 SensorSample을,
아니면 None을 반환한다 — 테스트는 이 clock을 가짜로 주입해 실제 sleep 없이
스케줄링을 검증한다.
"""

from __future__ import annotations

from typing import Callable, Optional, Protocol

from gateway.api_models import SensorSample

#: 단조 증가 시각(초)을 반환하는 함수. 기본은 time.monotonic이며 테스트에서 주입한다.
Clock = Callable[[], float]


class SensorSourceError(ValueError):
    """CSV 파싱, dataset allowlist, 경로/무결성 검증 실패를 나타낸다."""


class SensorSource(Protocol):
    """폴링 기반 센서 소스 공통 인터페이스 — CSV 재생과 합성 신호가 구현한다."""

    def start(self) -> None:
        """세션을 index 0에서 결정적으로 (재)시작한다."""

    def tick(self) -> Optional[SensorSample]:
        """주입된 clock 기준으로 새 샘플이 준비됐으면 반환하고, 아니면 None."""
