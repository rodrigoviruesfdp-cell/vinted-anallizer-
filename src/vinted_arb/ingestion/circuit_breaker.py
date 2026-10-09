"""Circuit breaker con enfriamiento creciente y estado persistible.

Estados::

    CLOSED ──(N fallos blandos seguidos | 1 fallo duro)──► OPEN
    OPEN ──(expira el enfriamiento)──► HALF_OPEN (deja pasar UNA petición sonda)
    HALF_OPEN ──(éxito)──► CLOSED          HALF_OPEN ──(fallo)──► OPEN (enfriamiento ×multiplier)

Los fallos *duros* (captcha, 403) abren el circuito al instante: insistir tras un desafío
antibot es exactamente lo que convierte un bloqueo temporal en uno permanente.

Se usa reloj de pared (``time.time``) para que el estado pueda guardarse en disco y un
reinicio del proceso no se salte el enfriamiento.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from enum import StrEnum
from typing import Any

logger = logging.getLogger(__name__)


class BreakerState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


StateListener = Callable[[BreakerState, BreakerState, str], None]


class CircuitBreaker:
    def __init__(
        self,
        name: str,
        *,
        failure_threshold: int = 3,
        base_cooldown_s: float = 900.0,
        max_cooldown_s: float = 6 * 3600.0,
        cooldown_multiplier: float = 2.0,
        probe_timeout_s: float = 120.0,
        clock: Callable[[], float] = time.time,
        on_state_change: StateListener | None = None,
    ) -> None:
        if failure_threshold < 1:
            raise ValueError("failure_threshold debe ser >= 1")
        if base_cooldown_s <= 0 or max_cooldown_s < base_cooldown_s:
            raise ValueError("Enfriamientos inválidos")
        if cooldown_multiplier < 1:
            raise ValueError("cooldown_multiplier debe ser >= 1")
        self.name = name
        self.failure_threshold = failure_threshold
        self.base_cooldown_s = base_cooldown_s
        self.max_cooldown_s = max_cooldown_s
        self.cooldown_multiplier = cooldown_multiplier
        self.probe_timeout_s = probe_timeout_s
        self._clock = clock
        self._on_state_change = on_state_change

        self._state = BreakerState.CLOSED
        self._consecutive_failures = 0
        self._consecutive_trips = 0
        self._open_until = 0.0
        self._probe_in_flight = False
        self._probe_started_at = 0.0
        self._last_reason = ""

    # ------------------------------------------------------------------ consultas
    @property
    def state(self) -> BreakerState:
        return self._state

    @property
    def last_reason(self) -> str:
        return self._last_reason

    @property
    def consecutive_trips(self) -> int:
        return self._consecutive_trips

    def retry_in_s(self) -> float:
        """Segundos hasta que se permita la siguiente petición (0 si ya se permite)."""
        if self._state is BreakerState.OPEN:
            return max(0.0, self._open_until - self._clock())
        return 0.0

    def allow_request(self) -> bool:
        """¿Puede salir una petición ahora? En HALF_OPEN solo se permite una sonda.

        Si una sonda no informa de su resultado en ``probe_timeout_s`` (p. ej. murió por una
        excepción inesperada), se permite otra: el breaker nunca debe quedarse atascado.
        """
        now = self._clock()
        if self._state is BreakerState.CLOSED:
            return True
        if self._state is BreakerState.OPEN:
            if now < self._open_until:
                return False
            self._transition(BreakerState.HALF_OPEN, "enfriamiento cumplido; enviando sonda")
        elif self._probe_in_flight and now - self._probe_started_at < self.probe_timeout_s:
            return False
        self._probe_in_flight = True
        self._probe_started_at = now
        return True

    # ------------------------------------------------------------------ registro
    def record_success(self) -> None:
        self._consecutive_failures = 0
        self._probe_in_flight = False
        if self._state is not BreakerState.CLOSED:
            self._consecutive_trips = 0
            self._transition(BreakerState.CLOSED, "sonda correcta")

    def record_failure(self, reason: str, *, hard: bool = False) -> None:
        """Registra un fallo. ``hard=True`` (captcha/403) abre el circuito inmediatamente."""
        self._last_reason = reason
        self._probe_in_flight = False
        if self._state is BreakerState.HALF_OPEN:
            self._open(reason)
            return
        if self._state is BreakerState.OPEN:
            return
        self._consecutive_failures += 1
        if hard or self._consecutive_failures >= self.failure_threshold:
            self._open(reason)

    def trip(self, reason: str) -> None:
        """Atajo para un fallo duro."""
        self.record_failure(reason, hard=True)

    # ------------------------------------------------------------------ persistencia
    def snapshot(self) -> dict[str, Any]:
        return {
            "state": self._state.value,
            "open_until": self._open_until,
            "consecutive_trips": self._consecutive_trips,
            "consecutive_failures": self._consecutive_failures,
            "last_reason": self._last_reason,
        }

    def restore(self, data: dict[str, Any]) -> None:
        """Restaura un snapshot. Un HALF_OPEN guardado se trata como OPEN ya vencido."""
        try:
            state = BreakerState(data.get("state", BreakerState.CLOSED.value))
            self._open_until = float(data.get("open_until", 0.0))
            self._consecutive_trips = int(data.get("consecutive_trips", 0))
            self._consecutive_failures = int(data.get("consecutive_failures", 0))
            self._last_reason = str(data.get("last_reason", ""))
        except (TypeError, ValueError) as exc:
            logger.warning("Snapshot de breaker inválido, se ignora: %s", exc)
            return
        self._state = BreakerState.OPEN if state is BreakerState.HALF_OPEN else state
        self._probe_in_flight = False

    # ------------------------------------------------------------------ internos
    def _open(self, reason: str) -> None:
        self._consecutive_trips += 1
        cooldown = min(
            self.max_cooldown_s,
            self.base_cooldown_s * self.cooldown_multiplier ** (self._consecutive_trips - 1),
        )
        self._open_until = self._clock() + cooldown
        self._consecutive_failures = 0
        self._transition(BreakerState.OPEN, f"{reason} (enfriamiento {cooldown:.0f}s)")

    def _transition(self, new_state: BreakerState, reason: str) -> None:
        old_state = self._state
        self._state = new_state
        if old_state is new_state:
            return
        logger.warning(
            "Breaker '%s': %s → %s: %s", self.name, old_state.value, new_state.value, reason
        )
        if self._on_state_change is not None:
            try:
                self._on_state_change(old_state, new_state, reason)
            except Exception:  # un listener defectuoso no debe tumbar la ingesta
                logger.exception("Listener del breaker '%s' falló", self.name)
