"""Limitador de ritmo asíncrono: token bucket + jitter aleatorio.

El token bucket fija el presupuesto medio de peticiones; el jitter rompe la periodicidad
(intervalos idénticos entre peticiones son en sí mismos una señal de bot).
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Awaitable, Callable


class AsyncRateLimiter:
    def __init__(
        self,
        rate_per_minute: float,
        *,
        burst: int = 1,
        jitter_ratio: float = 0.3,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        rng: random.Random | None = None,
    ) -> None:
        if rate_per_minute <= 0:
            raise ValueError("rate_per_minute debe ser > 0")
        if burst < 1:
            raise ValueError("burst debe ser >= 1")
        if jitter_ratio < 0:
            raise ValueError("jitter_ratio debe ser >= 0")
        self._rate_per_s = rate_per_minute / 60.0
        self._capacity = float(burst)
        self._jitter_ratio = jitter_ratio
        self._clock = clock
        self._sleep = sleep
        self._rng = rng or random.Random()
        self._tokens = float(burst)
        self._last_refill = clock()
        self._lock = asyncio.Lock()

    @property
    def mean_interval_s(self) -> float:
        return 1.0 / self._rate_per_s

    def _refill(self) -> None:
        now = self._clock()
        elapsed = max(0.0, now - self._last_refill)
        self._tokens = min(self._capacity, self._tokens + elapsed * self._rate_per_s)
        self._last_refill = now

    async def acquire(self) -> float:
        """Espera hasta poder enviar una petición. Devuelve los segundos esperados."""
        async with self._lock:
            self._refill()
            deficit_wait = 0.0
            if self._tokens < 1.0:
                deficit_wait = (1.0 - self._tokens) / self._rate_per_s
            jitter = self._rng.uniform(0.0, self._jitter_ratio * self.mean_interval_s)
            total = deficit_wait + jitter
            if total > 0:
                await self._sleep(total)
                self._refill()
            self._tokens -= 1.0
            return total
