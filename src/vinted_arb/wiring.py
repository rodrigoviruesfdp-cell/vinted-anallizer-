"""Composición de dependencias a partir de :class:`Settings`."""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence

from vinted_arb.config import Settings
from vinted_arb.ingestion.backoff import BackoffPolicy
from vinted_arb.ingestion.circuit_breaker import CircuitBreaker
from vinted_arb.ingestion.engine import HealthHandler, IngestionEngine, NewListingsHandler
from vinted_arb.ingestion.profiles import BrowserProfile, ProfilePool
from vinted_arb.ingestion.rate_limiter import AsyncRateLimiter
from vinted_arb.ingestion.transport import HttpTransport, build_transport
from vinted_arb.ingestion.vinted_client import VintedClient
from vinted_arb.models import SearchQuery
from vinted_arb.storage.repository import ListingRepository, PollRunLog, StateStore


def build_client(settings: Settings) -> VintedClient:
    def transport_factory(profile: BrowserProfile) -> HttpTransport:
        return build_transport(
            settings.http_transport,
            profile,
            timeout_s=settings.http_timeout_s,
            proxy=settings.http_proxy_url,
        )

    return VintedClient(
        domain=settings.vinted_domain,
        transport_factory=transport_factory,
        profiles=ProfilePool(),
        rate_limiter=AsyncRateLimiter(
            settings.rate_limit_per_minute,
            burst=settings.rate_limit_burst,
            jitter_ratio=settings.rate_limit_jitter,
        ),
        breaker=CircuitBreaker(
            "vinted",
            failure_threshold=settings.breaker_failure_threshold,
            base_cooldown_s=settings.breaker_cooldown_s,
            max_cooldown_s=max(settings.breaker_max_cooldown_s, settings.breaker_cooldown_s),
        ),
        backoff=BackoffPolicy(
            base_delay_s=settings.retry_base_delay_s,
            max_delay_s=max(settings.retry_max_delay_s, settings.retry_base_delay_s),
            max_attempts=settings.retry_max_attempts,
        ),
        accept_language=settings.accept_language,
        session_max_age_s=settings.session_max_age_s,
    )


def build_engine(
    settings: Settings,
    conn: sqlite3.Connection,
    searches: Sequence[SearchQuery],
    *,
    client: VintedClient | None = None,
    on_new: NewListingsHandler | None = None,
    on_health: HealthHandler | None = None,
) -> IngestionEngine:
    return IngestionEngine(
        client=client or build_client(settings),
        listings=ListingRepository(conn),
        state=StateStore(conn),
        runs=PollRunLog(conn),
        searches=searches,
        domain=settings.vinted_domain,
        on_new=on_new,
        on_health=on_health,
        interval_jitter=settings.poll_interval_jitter,
    )
