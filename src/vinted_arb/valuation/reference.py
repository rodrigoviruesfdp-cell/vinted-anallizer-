"""Precio de venta de referencia: mercado propio de Vinted + *price book* manual.

* **Mercado Vinted** (fuente principal): percentil de venta rápida de los anuncios
  comparables × ``ask_to_sale`` (κ), que descuenta las ofertas a la baja que aceptarás
  para vender rápido. κ es una hipótesis a calibrar con ventas reales (M1.1/M5).
* **Price book** (``config/price_book.csv``): precios de venta que fijas tú para los SKUs
  que conoces bien. Arranca el sistema el primer día, cuando aún no hay histórico.
* Si conviven, se mezclan con la confianza del mercado como peso.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from vinted_arb.normalization.condition import Condition
from vinted_arb.valuation.fees import money
from vinted_arb.valuation.market import CONDITION_MULTIPLIERS, MarketEstimate

PRICE_BOOK_HEADER = ("sku", "condition", "price_eur", "updated_at", "notes")


class PriceBookError(ValueError):
    """El fichero del price book es inválido."""


@dataclass(frozen=True, slots=True)
class PriceBookEntry:
    sku: str
    condition: Condition | None
    """``None`` = precio en estado "Muy bueno", aplicable a otros estados con multiplicadores."""
    price: Decimal
    updated_at: date | None


@dataclass(frozen=True, slots=True)
class BookQuote:
    price: Decimal
    confidence: float
    exact_condition: bool


class PriceBook:
    def __init__(self, entries: list[PriceBookEntry] | None = None) -> None:
        self._entries: dict[tuple[str, Condition | None], PriceBookEntry] = {}
        for entry in entries or []:
            self._entries[(entry.sku, entry.condition)] = entry

    def __len__(self) -> int:
        return len(self._entries)

    @classmethod
    def load(cls, path: str | Path) -> PriceBook:
        """Carga el CSV. Si no existe, devuelve un price book vacío."""
        file = Path(path)
        if not file.exists():
            return cls()
        entries: list[PriceBookEntry] = []
        with file.open(encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            missing = set(PRICE_BOOK_HEADER[:3]) - set(reader.fieldnames or [])
            if missing:
                raise PriceBookError(f"{file}: faltan columnas {sorted(missing)}")
            for line, row in enumerate(reader, start=2):
                sku = (row.get("sku") or "").strip()
                if not sku or sku.startswith("#"):
                    continue
                entries.append(_parse_row(row, sku, file, line))
        return cls(entries)

    def quote(
        self, sku: str, condition: Condition, *, today: date | None = None
    ) -> BookQuote | None:
        """Precio de venta esperado para ``sku`` en ``condition``, si hay entrada."""
        today = today or datetime.now(UTC).date()
        exact = self._entries.get((sku, condition))
        if exact is not None:
            return BookQuote(exact.price, _book_confidence(exact, today), exact_condition=True)
        base = self._entries.get((sku, None))
        if base is None or condition not in CONDITION_MULTIPLIERS:
            return None
        price = money(base.price * CONDITION_MULTIPLIERS[condition])
        return BookQuote(price, _book_confidence(base, today) * 0.9, exact_condition=False)


def _parse_row(row: dict[str, str], sku: str, file: Path, line: int) -> PriceBookEntry:
    raw_condition = (row.get("condition") or "").strip()
    try:
        condition = Condition(raw_condition) if raw_condition else None
    except ValueError as exc:
        valid = ", ".join(c.value for c in CONDITION_MULTIPLIERS)
        raise PriceBookError(f"{file}:{line}: estado '{raw_condition}' inválido ({valid})") from exc
    try:
        price = money((row.get("price_eur") or "").strip().replace(",", "."))
    except InvalidOperation as exc:
        raise PriceBookError(f"{file}:{line}: price_eur inválido") from exc
    if price <= 0:
        raise PriceBookError(f"{file}:{line}: price_eur debe ser positivo")
    raw_date = (row.get("updated_at") or "").strip()
    try:
        updated = date.fromisoformat(raw_date) if raw_date else None
    except ValueError as exc:
        raise PriceBookError(f"{file}:{line}: updated_at debe ser AAAA-MM-DD") from exc
    return PriceBookEntry(sku=sku, condition=condition, price=price, updated_at=updated)


def _book_confidence(entry: PriceBookEntry, today: date) -> float:
    """Los precios manuales envejecen: la electrónica se deprecia mes a mes."""
    if entry.updated_at is None:
        return 0.5
    age = (today - entry.updated_at).days
    if age <= 30:
        return 0.7
    if age <= 90:
        return 0.5
    return 0.3


@dataclass(frozen=True, slots=True)
class Reference:
    sale_price: Decimal
    """Precio al que esperas vender (neto de negociación, antes de costes)."""
    confidence: float
    source: str
    market: MarketEstimate | None = None
    book: BookQuote | None = None


def resolve_reference(
    market: MarketEstimate | None,
    book: BookQuote | None,
    *,
    ask_to_sale: Decimal,
) -> Reference | None:
    """Combina mercado y price book en un único precio de venta esperado."""
    market_sale = money(market.quick_sale * ask_to_sale) if market else None
    if market is not None and market_sale is not None:
        if book is None or market.confidence >= 1.0:
            source = "vinted_market_pooled" if market.pooled else "vinted_market"
            return Reference(market_sale, market.confidence, source, market=market)
        weight = Decimal(str(market.confidence))
        blended = money(weight * market_sale + (1 - weight) * book.price)
        confidence = market.confidence + (1 - market.confidence) * book.confidence
        return Reference(blended, round(confidence, 3), "blend", market=market, book=book)
    if book is not None:
        return Reference(book.price, book.confidence, "price_book", book=book)
    return None
