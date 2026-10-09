"""Repositorios de entidades normalizadas (mercado) y oportunidades valoradas."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from decimal import Decimal

from vinted_arb.normalization.condition import Condition
from vinted_arb.normalization.extractor import ExtractedEntity
from vinted_arb.storage.repository import from_cents, iso, to_cents, utcnow
from vinted_arb.valuation.market import PricePoint
from vinted_arb.valuation.opportunity import Opportunity


class EntityRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def pending(self, version: int, limit: int = 5000) -> list[sqlite3.Row]:
        """Anuncios sin normalizar o normalizados con una versión anterior del normalizador."""
        return list(
            self._conn.execute(
                """
                SELECT l.id, l.title, l.status_label
                FROM listings l
                LEFT JOIN listing_entities e ON e.listing_id = l.id
                WHERE e.listing_id IS NULL OR e.normalizer_version < ?
                ORDER BY l.id
                LIMIT ?
                """,
                (version, limit),
            ).fetchall()
        )

    def save(
        self,
        listing_id: int,
        entity: ExtractedEntity,
        *,
        version: int,
        market_eligible: bool,
        now: datetime | None = None,
    ) -> None:
        product = entity.product
        self._conn.execute(
            """
            INSERT OR REPLACE INTO listing_entities (
                listing_id, normalizer_version, sku, family, condition, match_confidence,
                red_flags, accessories, battery_health, market_eligible, normalized_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                listing_id,
                version,
                entity.sku,
                product.family.value if product else None,
                entity.condition.value,
                entity.match_confidence,
                json.dumps(sorted(f.value for f in entity.red_flags)),
                json.dumps(sorted(a.value for a in entity.accessories)),
                entity.battery_health,
                int(market_eligible),
                iso(now or utcnow()),
            ),
        )

    def market_points(self, sku: str, *, since: datetime) -> list[PricePoint]:
        """Comparables vistos desde ``since`` (precio actual del anuncio, última observación)."""
        rows = self._conn.execute(
            """
            SELECT l.id, l.price_cents, l.last_seen_at, e.condition
            FROM listing_entities e
            JOIN listings l ON l.id = e.listing_id
            WHERE e.sku = ? AND e.market_eligible = 1 AND l.last_seen_at >= ?
            """,
            (sku, iso(since)),
        ).fetchall()
        return [
            PricePoint(
                price=from_cents(int(r["price_cents"])),
                condition=Condition(r["condition"]),
                observed_at=datetime.fromisoformat(r["last_seen_at"]),
                listing_id=int(r["id"]),
            )
            for r in rows
        ]

    def sku_counts(self, *, since: datetime, limit: int = 50) -> list[tuple[str, int]]:
        rows = self._conn.execute(
            """
            SELECT e.sku, COUNT(*) AS n
            FROM listing_entities e
            JOIN listings l ON l.id = e.listing_id
            WHERE e.market_eligible = 1 AND l.last_seen_at >= ?
            GROUP BY e.sku
            ORDER BY n DESC, e.sku
            LIMIT ?
            """,
            (iso(since), limit),
        ).fetchall()
        return [(str(r["sku"]), int(r["n"])) for r in rows]


class OpportunityRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def save(self, opp: Opportunity, *, now: datetime | None = None) -> None:
        """Guarda la última evaluación del anuncio conservando el registro de alertas."""
        breakdown = opp.breakdown
        reference = opp.reference
        self._conn.execute(
            """
            INSERT INTO opportunities (
                listing_id, evaluated_at, sku, condition, price_cents, expected_sale_cents,
                net_margin_cents, roi, score, match_confidence, fmv_confidence, risk,
                reference_source, decision, reasons, breakdown
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(listing_id) DO UPDATE SET
                evaluated_at = excluded.evaluated_at, sku = excluded.sku,
                condition = excluded.condition, price_cents = excluded.price_cents,
                expected_sale_cents = excluded.expected_sale_cents,
                net_margin_cents = excluded.net_margin_cents, roi = excluded.roi,
                score = excluded.score, match_confidence = excluded.match_confidence,
                fmv_confidence = excluded.fmv_confidence, risk = excluded.risk,
                reference_source = excluded.reference_source, decision = excluded.decision,
                reasons = excluded.reasons, breakdown = excluded.breakdown
            """,
            (
                opp.listing.id,
                iso(now or utcnow()),
                opp.entity.sku,
                opp.condition.value,
                to_cents(opp.listing.price),
                to_cents(reference.sale_price) if reference else None,
                to_cents(breakdown.net_margin) if breakdown else None,
                float(breakdown.roi) if breakdown and breakdown.roi.is_finite() else None,
                opp.score,
                opp.entity.match_confidence,
                reference.confidence if reference else None,
                opp.risk,
                reference.source if reference else None,
                opp.decision.value,
                json.dumps(list(opp.reasons), ensure_ascii=False),
                json.dumps(breakdown.as_dict()) if breakdown else None,
            ),
        )

    def alerted_price(self, listing_id: int) -> Decimal | None:
        row = self._conn.execute(
            "SELECT alerted_price_cents FROM opportunities WHERE listing_id = ?", (listing_id,)
        ).fetchone()
        if row is None or row["alerted_price_cents"] is None:
            return None
        return from_cents(int(row["alerted_price_cents"]))

    def mark_alerted(self, listing_id: int, price: Decimal, *, now: datetime | None = None) -> None:
        self._conn.execute(
            "UPDATE opportunities SET alerted_at = ?, alerted_price_cents = ? WHERE listing_id = ?",
            (iso(now or utcnow()), to_cents(price), listing_id),
        )

    def recent(self, *, decision: str | None = None, limit: int = 20) -> list[sqlite3.Row]:
        query = """
            SELECT o.*, l.title, l.url
            FROM opportunities o JOIN listings l ON l.id = o.listing_id
        """
        params: tuple[object, ...] = ()
        if decision:
            query += " WHERE o.decision = ?"
            params = (decision,)
        query += " ORDER BY o.evaluated_at DESC, o.score DESC LIMIT ?"
        return list(self._conn.execute(query, (*params, limit)).fetchall())

    def counts(self) -> dict[str, int]:
        rows = self._conn.execute(
            "SELECT decision, COUNT(*) AS n FROM opportunities GROUP BY decision"
        ).fetchall()
        return {str(r["decision"]): int(r["n"]) for r in rows}
