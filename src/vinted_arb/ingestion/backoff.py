"""Política de reintentos con backoff exponencial y jitter.

Se usa *equal jitter* por defecto: la espera es ``cap/2 + U(0, cap/2)``. A diferencia del
*full jitter* (``U(0, cap)``), garantiza una espera mínima, que es lo prudente frente a un
servidor que nos está limitando: reintentar casi inmediatamente tras un 429 empeora la
reputación de la sesión.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Literal

JitterMode = Literal["equal", "full", "none"]

# Evita desbordar el float con exponentes absurdos si alguien configura muchos intentos.
_MAX_EXPONENT = 32


@dataclass(frozen=True, slots=True)
class BackoffPolicy:
    base_delay_s: float = 3.0
    max_delay_s: float = 90.0
    multiplier: float = 2.0
    max_attempts: int = 4
    """Número total de intentos, incluido el primero."""
    jitter: JitterMode = "equal"
    max_retry_after_s: float = 600.0
    """Tope para la cabecera ``Retry-After`` (un servidor no debe poder dormirnos horas)."""

    def __post_init__(self) -> None:
        if self.base_delay_s <= 0 or self.max_delay_s <= 0:
            raise ValueError("Los retardos deben ser positivos")
        if self.max_delay_s < self.base_delay_s:
            raise ValueError("max_delay_s debe ser >= base_delay_s")
        if self.multiplier < 1:
            raise ValueError("multiplier debe ser >= 1")
        if self.max_attempts < 1:
            raise ValueError("max_attempts debe ser >= 1")

    def cap_for(self, retry_number: int) -> float:
        """Techo de espera (sin jitter) para el reintento ``retry_number`` (empieza en 1)."""
        if retry_number < 1:
            raise ValueError("retry_number empieza en 1")
        exponent = min(retry_number - 1, _MAX_EXPONENT)
        return float(min(self.max_delay_s, self.base_delay_s * self.multiplier**exponent))

    def delay_for(self, retry_number: int, rng: random.Random | None = None) -> float:
        """Espera antes del reintento ``retry_number``."""
        cap = self.cap_for(retry_number)
        r = rng or random
        if self.jitter == "none":
            return cap
        if self.jitter == "full":
            return r.uniform(0.0, cap)
        return cap / 2 + r.uniform(0.0, cap / 2)

    def delay_with_hint(
        self,
        retry_number: int,
        retry_after_s: float | None,
        rng: random.Random | None = None,
    ) -> float:
        """Como :meth:`delay_for` pero respetando ``Retry-After`` si el servidor lo envía."""
        delay = self.delay_for(retry_number, rng)
        if retry_after_s is not None and retry_after_s > 0:
            delay = max(delay, min(retry_after_s, self.max_retry_after_s))
        return delay
