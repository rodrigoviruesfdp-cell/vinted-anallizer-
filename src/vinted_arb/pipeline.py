"""Pipeline de oportunidades: ingesta (M1) → normalización (M2) → valoración (M3) → alertas (M4).

Se engancha al motor de ingesta:

* ``on_polled`` (tras cada consulta, también en la línea base): normaliza todo lo nuevo
  para que el mercado de cada SKU esté al día.
* ``on_new`` (anuncios nuevos y bajadas de precio): valora, guarda la evaluación y alerta.
* ``on_health``: reenvía los avisos de salud (bloqueos, cambios de API) a los canales.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from vinted_arb.alerts.notifiers import Notifier
from vinted_arb.ingestion.engine import PollOutcome
from vinted_arb.models import Listing, SearchQuery
from vinted_arb.normalization.condition import Condition
from vinted_arb.normalization.extractor import NORMALIZER_VERSION, ExtractedEntity, extract
from vinted_arb.storage.repository import UpsertResult, utcnow
from vinted_arb.storage.valuation import EntityRepository, OpportunityRepository
from vinted_arb.valuation.market import (
    MarketEstimate,
    PricePoint,
    estimate_market,
    is_market_eligible,
)
from vinted_arb.valuation.opportunity import (
    Decision,
    EvaluationParams,
    Opportunity,
    evaluate,
    valuation_condition,
)
from vinted_arb.valuation.reference import PriceBook, Reference, resolve_reference

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class MarketParams:
    percentile: float = 0.30
    ask_to_sale: Decimal = Decimal("0.92")
    """κ: fracción del precio pedido de venta rápida que esperas cobrar tras negociar."""
    window_days: int = 30
    half_life_days: float = 14.0
    min_samples: int = 12


class OpportunityPipeline:
    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        evaluation: EvaluationParams | None = None,
        market: MarketParams | None = None,
        price_book: PriceBook | None = None,
        notifier: Notifier | None = None,
        realert_drop: Decimal = Decimal("0.03"),
        max_alerts_per_hour: int = 30,
        cache_ttl_s: float = 600.0,
        now: Callable[[], datetime] = utcnow,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._entities = EntityRepository(conn)
        self._opportunities = OpportunityRepository(conn)
        self._evaluation = evaluation or EvaluationParams()
        self._market = market or MarketParams()
        self._price_book = price_book or PriceBook()
        self._notifier = notifier
        self._realert_drop = realert_drop
        self._max_alerts_per_hour = max_alerts_per_hour
        self._cache_ttl_s = cache_ttl_s
        self._now = now
        self._monotonic = monotonic
        self._points_cache: dict[str, tuple[float, list[PricePoint]]] = {}
        self._alert_times: deque[float] = deque()

    @property
    def opportunities(self) -> OpportunityRepository:
        return self._opportunities

    @property
    def entities(self) -> EntityRepository:
        return self._entities

    # ------------------------------------------------------------------ normalización
    def normalize_pending(self, limit: int = 5000) -> int:
        """Normaliza los anuncios sin entidad (o con una versión antigua del normalizador)."""
        rows = self._entities.pending(NORMALIZER_VERSION, limit)
        now = self._now()
        touched: set[str] = set()
        for row in rows:
            entity = extract(row["title"], status_label=row["status_label"])
            eligible = is_market_eligible(entity)
            self._entities.save(
                int(row["id"]),
                entity,
                version=NORMALIZER_VERSION,
                market_eligible=eligible,
                now=now,
            )
            if eligible and entity.sku:
                touched.add(entity.sku)
        for sku in touched:  # el mercado de esos SKUs ha cambiado
            self._points_cache.pop(sku, None)
        if rows:
            logger.debug("Normalizados %d anuncios (%d SKUs de mercado)", len(rows), len(touched))
        return len(rows)

    # ------------------------------------------------------------------ valoración
    def market_estimate(
        self, sku: str, condition: Condition, *, exclude_id: int | None = None
    ) -> MarketEstimate | None:
        points = [p for p in self._market_points(sku) if p.listing_id != exclude_id]
        return estimate_market(
            points,
            condition=condition,
            now=self._now(),
            percentile=self._market.percentile,
            half_life_days=self._market.half_life_days,
            min_samples=self._market.min_samples,
        )

    def reference_for(
        self, sku: str, condition: Condition, *, exclude_id: int | None = None
    ) -> Reference | None:
        market = self.market_estimate(sku, condition, exclude_id=exclude_id)
        book = self._price_book.quote(sku, condition, today=self._now().date())
        return resolve_reference(market, book, ask_to_sale=self._market.ask_to_sale)

    def evaluate_listing(
        self, listing: Listing, entity: ExtractedEntity | None = None
    ) -> Opportunity:
        entity = entity or extract(listing.title, status_label=listing.status_label)
        reference = None
        if entity.sku is not None:
            reference = self.reference_for(
                entity.sku, valuation_condition(entity.condition), exclude_id=listing.id
            )
        return evaluate(listing, entity, reference, self._evaluation)

    # ------------------------------------------------------------------ ganchos del motor
    async def on_polled(self, query: SearchQuery, outcome: PollOutcome) -> None:
        self.normalize_pending()

    async def on_new(self, query: SearchQuery, result: UpsertResult) -> list[Opportunity]:
        self.normalize_pending()
        candidates = list(result.new) + [c.listing for c in result.price_changes if c.is_drop]
        evaluated: list[Opportunity] = []
        for listing in candidates:
            opp = self.evaluate_listing(listing)
            self._opportunities.save(opp, now=self._now())
            evaluated.append(opp)
            self._log(query, opp)
            if opp.decision is Decision.ALERT:
                await self._maybe_alert(opp)
        return evaluated

    async def on_health(self, message: str) -> None:
        logger.error("SALUD: %s", message)
        if self._notifier is not None:
            await self._notifier.send_text(f"⚠️ {message}")

    # ------------------------------------------------------------------ internos
    def _market_points(self, sku: str) -> list[PricePoint]:
        cached = self._points_cache.get(sku)
        if cached and self._monotonic() - cached[0] < self._cache_ttl_s:
            return cached[1]
        since = self._now() - timedelta(days=self._market.window_days)
        points = self._entities.market_points(sku, since=since)
        self._points_cache[sku] = (self._monotonic(), points)
        return points

    async def _maybe_alert(self, opp: Opportunity) -> None:
        previous = self._opportunities.alerted_price(opp.listing.id)
        if previous is not None and opp.listing.price > previous * (1 - self._realert_drop):
            return  # ya avisado y el precio no ha bajado lo suficiente
        if self._notifier is None:
            return
        now = self._monotonic()
        while self._alert_times and now - self._alert_times[0] > 3600:
            self._alert_times.popleft()
        if len(self._alert_times) >= self._max_alerts_per_hour:
            logger.warning(
                "Límite de %d alertas/hora alcanzado; '%s' queda solo en la base de datos",
                self._max_alerts_per_hour,
                opp.listing.title,
            )
            return
        if await self._notifier.send_opportunity(opp):
            self._alert_times.append(now)
            self._opportunities.mark_alerted(opp.listing.id, opp.listing.price, now=self._now())

    def _log(self, query: SearchQuery, opp: Opportunity) -> None:
        if opp.decision is Decision.REJECT:
            logger.debug(
                "[%s] descartado %s: %s", query.name, opp.listing.title, "; ".join(opp.reasons)
            )
            return
        level = logging.WARNING if opp.decision is Decision.ALERT else logging.INFO
        label = "OPORTUNIDAD" if opp.decision is Decision.ALERT else "vigilar"
        logger.log(
            level,
            "%s [%s] %s € · %s → venta %s € · neto %s € · %s · %s",
            label,
            query.name,
            opp.listing.price,
            opp.listing.title,
            opp.reference.sale_price if opp.reference else "?",
            opp.net_margin if opp.net_margin is not None else "?",
            "; ".join(opp.reasons),
            opp.listing.url,
        )
