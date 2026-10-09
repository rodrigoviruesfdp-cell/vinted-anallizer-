"""Jerarquía de errores del motor de ingesta."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from vinted_arb.ingestion.block_detector import Verdict


class IngestionError(Exception):
    """Base de todos los errores de ingesta."""


class TransportError(IngestionError):
    """Fallo de red: DNS, timeout, TLS, proxy, conexión reiniciada…"""


class BlockedError(IngestionError):
    """El servidor ha servido un desafío antibot o un 403. No se reintenta: se enfría."""

    def __init__(self, verdict: Verdict) -> None:
        super().__init__(f"Bloqueado por el servidor: {verdict.reason}")
        self.verdict = verdict


class CircuitOpenError(IngestionError):
    """Circuit breaker abierto: no salen peticiones hasta que expire el enfriamiento."""

    def __init__(self, retry_in_s: float, reason: str) -> None:
        super().__init__(f"Circuit breaker abierto ({reason}); reintentar en {retry_in_s:.0f}s")
        self.retry_in_s = retry_in_s
        self.reason = reason


class HttpStatusError(IngestionError):
    """Respuesta HTTP no satisfactoria que no es un bloqueo."""

    def __init__(self, verdict: Verdict) -> None:
        super().__init__(f"HTTP {verdict.status}: {verdict.reason}")
        self.verdict = verdict


class RetryExhaustedError(IngestionError):
    """Se agotaron los reintentos para una petición."""


class NotFoundError(IngestionError):
    """El recurso no existe (404/410)."""


class SchemaDriftError(IngestionError):
    """La respuesta no tiene la forma esperada: probable cambio en la API interna."""


class SessionBootstrapError(IngestionError):
    """No se pudo establecer una sesión anónima válida (faltan cookies de sesión)."""
