"""Composición de dependencias a partir de :class:`Settings`."""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from decimal import Decimal

from vinted_arb.alerts.notifiers import DiscordNotifier, MultiNotifier, Notifier, TelegramNotifier
from vinted_arb.config import Settings
from vinted_arb.ingestion.backoff import BackoffPolicy
from vinted_arb.ingestion.circuit_breaker import CircuitBreaker
from vinted_arb.ingestion.engine import (
    HealthHandler,
    IngestionEngine,
    NewListingsHandler,
    PolledHandler,
)
from vinted_arb.ingestion.profiles import BrowserProfile, ProfilePool
from vinted_arb.ingestion.rate_limiter import AsyncRateLimiter
from vinted_arb.ingestion.transport import HttpTransport, build_transport
from vinted_arb.ingestion.vinted_client import VintedClient
from vinted_arb.models import SearchQuery
from vinted_arb.pipeline import MarketParams, OpportunityPipeline
from vinted_arb.storage.repository import ListingRepository, PollRunLog, StateStore
from vinted_arb.valuation.fees import CHANNELS, DealCosts
from vinted_arb.valuation.opportunity import EvaluationParams
from vinted_arb.valuation.reference import PriceBook


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
    on_polled: PolledHandler | None = None,
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
        on_polled=on_polled,
        on_health=on_health,
        interval_jitter=settings.poll_interval_jitter,
    )


def build_deal_costs(settings: Settings) -> DealCosts:
    return DealCosts(
        inbound_shipping=settings.inbound_shipping_eur,
        outbound_shipping=settings.outbound_shipping_eur,
        packaging=settings.packaging_eur,
        promotion=settings.promotion_eur,
        risk_reserve_rate=settings.risk_reserve_rate,
        rebu_vat_rate=Decimal("0.21") if settings.rebu_enabled else None,
    )


def build_evaluation_params(settings: Settings) -> EvaluationParams:
    return EvaluationParams(
        channel=CHANNELS[settings.resale_channel],
        costs=build_deal_costs(settings),
        min_net_margin=settings.min_net_profit_eur,
        min_roi=settings.min_roi,
        min_match_confidence=settings.min_match_confidence,
        min_fmv_confidence=settings.min_fmv_confidence,
        max_risk=settings.max_risk,
    )


def build_notifier(settings: Settings) -> MultiNotifier:
    """Canales configurados (Telegram y/o Discord). Vacío si no hay ninguno."""
    notifiers: list[Notifier] = []
    if settings.telegram_bot_token and settings.telegram_chat_id:
        notifiers.append(
            TelegramNotifier(
                settings.telegram_bot_token.get_secret_value(), settings.telegram_chat_id
            )
        )
    if settings.discord_webhook_url:
        notifiers.append(DiscordNotifier(settings.discord_webhook_url.get_secret_value()))
    return MultiNotifier(notifiers)


def build_pipeline(
    settings: Settings,
    conn: sqlite3.Connection,
    *,
    notifier: Notifier | None = None,
) -> OpportunityPipeline:
    return OpportunityPipeline(
        conn,
        evaluation=build_evaluation_params(settings),
        market=MarketParams(
            percentile=settings.fmv_percentile,
            ask_to_sale=settings.fmv_ask_to_sale,
            window_days=settings.fmv_window_days,
            half_life_days=settings.fmv_half_life_days,
            min_samples=settings.fmv_min_samples,
        ),
        price_book=PriceBook.load(settings.price_book_file),
        notifier=notifier,
        max_alerts_per_hour=settings.alert_max_per_hour,
    )
