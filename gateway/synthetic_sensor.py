"""결정적 합성 저항 신호 생성기 (설계 스펙 6.3).

R = R0 + amplitude*sin(2πt/period) + seeded Gaussian noise. 같은 config+seed+
sample index는 항상 byte-identical 샘플을 낸다: noise는 (seed, index) 튜플로
매번 새로 시드된 random.Random의 순수 함수라서 인스턴스의 호출 이력(tick
빈도, 순서)에 의존하지 않는다 — 전역 random이나 wall clock으로 값을 계산하지
않는다. CSV 재생과 동일하게 clock을 주입받아 tick()으로 폴링한다.
"""

from __future__ import annotations

import math
import random
import time
from typing import Optional

from gateway.api_models import (
    SYNTHETIC_ADC_FULL_SCALE,
    SYNTHETIC_REFERENCE_RESISTOR_OHM,
    SensorSample,
)
from gateway.sensor_source import Clock

#: reference resistor / ADC full-scale 기본값 (설계 스펙 6.3) — 공용 스키마
#: (api_models.RawAdc)와는 별개로, 합성 생성기의 divider 역변환 clamp에만
#: 쓰는 config 기본값이다. 생성자 인자로 얼마든지 다른 값을 주입할 수 있다.
DEFAULT_REFERENCE_RESISTOR_OHM = SYNTHETIC_REFERENCE_RESISTOR_OHM
DEFAULT_ADC_FULL_SCALE = SYNTHETIC_ADC_FULL_SCALE

#: 기본 샘플 주기 (10Hz) — 클라이언트 설정 스키마엔 없는 게이트웨이 내부 값.
DEFAULT_SAMPLE_INTERVAL_MS = 100.0


class SyntheticSensorSource:
    """sine + seeded noise 모델로 index 기반 SensorSample을 생성한다."""

    def __init__(
        self,
        *,
        baseline_resistance_ohm: float,
        amplitude_ohm: float,
        period_s: float,
        noise_std_ohm: float,
        seed: int,
        temperature_c: float = 25.0,
        battery_pct: int = 100,
        sample_interval_ms: float = DEFAULT_SAMPLE_INTERVAL_MS,
        reference_resistor_ohm: float = DEFAULT_REFERENCE_RESISTOR_OHM,
        adc_full_scale: int = DEFAULT_ADC_FULL_SCALE,
        clock: Clock = time.monotonic,
    ) -> None:
        self._baseline = baseline_resistance_ohm
        self._amplitude = amplitude_ohm
        self._period_s = period_s
        self._noise_std = noise_std_ohm
        self._seed = seed
        self._temperature_c = temperature_c
        self._battery_pct = battery_pct
        self._sample_interval_ms = sample_interval_ms
        self._reference_resistor_ohm = reference_resistor_ohm
        self._adc_full_scale = adc_full_scale
        self._clock = clock

        self._start_ref: Optional[float] = None
        self._next_index = 0

    def start(self) -> None:
        """index 0에서 결정적으로 (재)시작한다."""
        self._start_ref = self._clock()
        self._next_index = 0

    def tick(self) -> Optional[SensorSample]:
        if self._start_ref is None:
            return None

        virtual_ms = (self._clock() - self._start_ref) * 1000.0
        if virtual_ms < self._next_index * self._sample_interval_ms:
            return None

        sample = self.sample_at_index(self._next_index)
        self._next_index += 1
        return sample

    def sample_at_index(self, index: int) -> SensorSample:
        """index만의 순수 함수 — 같은 config+seed+index는 항상 동일한 값을 낸다."""
        elapsed_ms = round(index * self._sample_interval_ms)
        t = elapsed_ms / 1000.0
        noise = random.Random(f"{self._seed}:{index}").gauss(0.0, 1.0) * self._noise_std
        resistance_ohm = (
            self._baseline
            + self._amplitude * math.sin(2 * math.pi * t / self._period_s)
            + noise
        )
        delta_r_over_r0 = (resistance_ohm - self._baseline) / self._baseline

        return SensorSample(
            source_timestamp_ms=elapsed_ms,
            session_elapsed_ms=elapsed_ms,
            loop_count=0,
            raw_adc=self._raw_adc_for(resistance_ohm),
            resistance_ohm=resistance_ohm,
            delta_r_over_r0=delta_r_over_r0,
            temperature_c=self._temperature_c,
            battery_pct=self._battery_pct,
        )

    def _raw_adc_for(self, resistance_ohm: float) -> int:
        """전압분배기 역변환: raw_adc = full_scale * R / (R + Rref), 0..full_scale로 clamp."""
        ratio = 0.0 if resistance_ohm <= 0 else (
            resistance_ohm / (resistance_ohm + self._reference_resistor_ohm)
        )
        raw = round(ratio * self._adc_full_scale)
        return max(0, min(self._adc_full_scale, raw))
