"""Repositorios sobre SQLite: anuncios, estado clave-valor y registro de ejecuciones."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from vinted_arb.models import Listing


def to_cents(amount: Decimal) -> int:
    return int((amount * 100).to_integral_value(rounding=ROUND_HALF_UP))


def from_cents(cents: int) -> Decimal:
    return (Decimal(cents) / 100).quantize(Decimal("0.01"))


def iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        raise ValueError("Se requieren datetimes con zona horaria (UTC)")
    return dt.astimezone(UTC).isoformat(timespec="seconds")


def utcnow() -> datetime:
    return datetime.now(UTC)


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[None]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


@dataclass(frozen=True, slots=True)
class PriceChange:
    listing: Listing
    previous_price: Decimal

    @property
    def delta(self) -> Decimal:
        return self.listing.price - self.previous_price

    @property
    def is_drop(self) -> bool:
        return self.delta < 0


@dataclass(frozen=True, slots=True)
class UpsertResult:
    new: tuple[Listing, ...] = ()
    price_changes: tuple[PriceChange, ...] = ()
    seen: int = 0

    def merge(self, other: UpsertResult) -> UpsertResult:
        return UpsertResult(
            new=self.new + other.new,
            price_changes=self.price_changes + other.price_changes,
            seen=self.seen + other.seen,
        )


class ListingRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def upsert_many(
        self,
        listings: Iterable[Listing],
        *,
        domain: str,
        search_name: str,
        now: datetime | None = None,
    ) -> UpsertResult:
        """Inserta anuncios nuevos, refresca los conocidos y detecta cambios de precio."""
        observed_at = iso(now or utcnow())
        new: list[Listing] = []
        changes: list[PriceChange] = []
        seen = 0
        with transaction(self._conn):
            for listing in listings:
                seen += 1
                price_cents = to_cents(listing.price)
                row = self._conn.execute(
                    "SELECT price_cents FROM listings WHERE id = ?", (listing.id,)
                ).fetchone()
                if row is None:
                    self._insert(listing, domain, search_name, observed_at, price_cents)
                    new.append(listing)
                    continue
                self._refresh(listing, observed_at, price_cents)
                previous = int(row["price_cents"])
                if previous != price_cents:
                    self._record_price(listing.id, observed_at, price_cents)
                    changes.append(PriceChange(listing, from_cents(previous)))
        return UpsertResult(new=tuple(new), price_changes=tuple(changes), seen=seen)

    def known_ids(self, ids: Iterable[int]) -> set[int]:
        id_list = list(ids)
        if not id_list:
            return set()
        placeholders = ",".join("?" * len(id_list))
        rows = self._conn.execute(
            f"SELECT id FROM listings WHERE id IN ({placeholders})", id_list
        ).fetchall()
        return {int(r["id"]) for r in rows}

    def get(self, listing_id: int) -> sqlite3.Row | None:
        row: sqlite3.Row | None = self._conn.execute(
            "SELECT * FROM listings WHERE id = ?", (listing_id,)
        ).fetchone()
        return row

    def price_history(self, listing_id: int) -> list[tuple[str, Decimal]]:
        rows = self._conn.execute(
            "SELECT observed_at, price_cents FROM price_history WHERE listing_id = ? "
            "ORDER BY observed_at",
            (listing_id,),
        ).fetchall()
        return [(r["observed_at"], from_cents(int(r["price_cents"]))) for r in rows]

    def count(self) -> int:
        return int(self._conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0])

    # ------------------------------------------------------------------ internos
    def _insert(
        self, listing: Listing, domain: str, search_name: str, observed_at: str, price_cents: int
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO listings (
                id, domain, title, brand, status_label, size_label, price_cents, currency,
                service_fee_cents, total_price_cents, url, photo_url, seller_id,
                favourite_count, view_count, listed_at, first_seen_at, last_seen_at, first_search
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                listing.id,
                domain,
                listing.title,
                listing.brand,
                listing.status_label,
                listing.size_label,
                price_cents,
                listing.currency,
                to_cents(listing.service_fee) if listing.service_fee is not None else None,
                to_cents(listing.total_price) if listing.total_price is not None else None,
                listing.url,
                listing.photo_urls[0] if listing.photo_urls else None,
                listing.seller_id,
                listing.favourite_count,
                listing.view_count,
                iso(listing.listed_at) if listing.listed_at else None,
                observed_at,
                observed_at,
                search_name,
            ),
        )
        self._record_price(listing.id, observed_at, price_cents)

    def _refresh(self, listing: Listing, observed_at: str, price_cents: int) -> None:
        self._conn.execute(
            """
            UPDATE listings SET
                title = ?, brand = ?, status_label = ?, price_cents = ?,
                service_fee_cents = ?, total_price_cents = ?,
                photo_url = COALESCE(?, photo_url),
                favourite_count = COALESCE(?, favourite_count),
                view_count = COALESCE(?, view_count),
                last_seen_at = ?
            WHERE id = ?
            """,
            (
                listing.title,
                listing.brand,
                listing.status_label,
                price_cents,
                to_cents(listing.service_fee) if listing.service_fee is not None else None,
                to_cents(listing.total_price) if listing.total_price is not None else None,
                listing.photo_urls[0] if listing.photo_urls else None,
                listing.favourite_count,
                listing.view_count,
                observed_at,
                listing.id,
            ),
        )

    def _record_price(self, listing_id: int, observed_at: str, price_cents: int) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO price_history (listing_id, observed_at, price_cents) "
            "VALUES (?, ?, ?)",
            (listing_id, observed_at, price_cents),
        )


class StateStore:
    """Almacén clave-valor JSON (estado del breaker, líneas base completadas…)."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def get(self, key: str, default: Any = None) -> Any:
        row = self._conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        if row is None:
            return default
        return json.loads(row["value"])

    def set(self, key: str, value: Any, *, now: datetime | None = None) -> None:
        self._conn.execute(
            "INSERT INTO kv (key, value, updated_at) VALUES (?, ?, ?) ON CONFLICT(key) "
            "DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (key, json.dumps(value, sort_keys=True), iso(now or utcnow())),
        )


class PollRunLog:
    """Registro de cada consulta de una búsqueda (observabilidad y KPIs del modo sombra)."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def start(self, search_name: str, *, now: datetime | None = None) -> int:
        cursor = self._conn.execute(
            "INSERT INTO poll_runs (search_name, started_at) VALUES (?, ?)",
            (search_name, iso(now or utcnow())),
        )
        return int(cursor.lastrowid or 0)

    def finish(
        self,
        run_id: int,
        *,
        status: str,
        pages: int = 0,
        result: UpsertResult | None = None,
        error: str | None = None,
        now: datetime | None = None,
    ) -> None:
        result = result or UpsertResult()
        self._conn.execute(
            """
            UPDATE poll_runs SET finished_at = ?, status = ?, pages = ?, items_seen = ?,
                items_new = ?, price_changes = ?, error = ?
            WHERE id = ?
            """,
            (
                iso(now or utcnow()),
                status,
                pages,
                result.seen,
                len(result.new),
                len(result.price_changes),
                error,
                run_id,
            ),
        )

    def recent(self, limit: int = 20) -> list[sqlite3.Row]:
        return list(
            self._conn.execute(
                "SELECT * FROM poll_runs ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        )
