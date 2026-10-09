"""Módulo 1: motor de ingesta de anuncios de Vinted."""

from vinted_arb.ingestion.backoff import BackoffPolicy
from vinted_arb.ingestion.block_detector import Verdict, VerdictKind, classify_response
from vinted_arb.ingestion.circuit_breaker import BreakerState, CircuitBreaker
from vinted_arb.ingestion.engine import IngestionEngine, PollOutcome
from vinted_arb.ingestion.errors import (
    BlockedError,
    CircuitOpenError,
    HttpStatusError,
    IngestionError,
    NotFoundError,
    RetryExhaustedError,
    SchemaDriftError,
    SessionBootstrapError,
    TransportError,
)
from vinted_arb.ingestion.profiles import DEFAULT_PROFILES, BrowserProfile, ProfilePool
from vinted_arb.ingestion.rate_limiter import AsyncRateLimiter
from vinted_arb.ingestion.vinted_client import VintedClient

__all__ = [
    "DEFAULT_PROFILES",
    "AsyncRateLimiter",
    "BackoffPolicy",
    "BlockedError",
    "BreakerState",
    "BrowserProfile",
    "CircuitBreaker",
    "CircuitOpenError",
    "HttpStatusError",
    "IngestionEngine",
    "IngestionError",
    "NotFoundError",
    "PollOutcome",
    "ProfilePool",
    "RetryExhaustedError",
    "SchemaDriftError",
    "SessionBootstrapError",
    "TransportError",
    "Verdict",
    "VerdictKind",
    "VintedClient",
    "classify_response",
]
