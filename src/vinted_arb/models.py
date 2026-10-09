"""Modelos de dominio compartidos entre módulos."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class SearchQuery(BaseModel):
    """Búsqueda guardada que el motor de ingesta consulta periódicamente."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    search_text: str = Field(min_length=1)
    catalog_ids: tuple[int, ...] = ()
    brand_ids: tuple[int, ...] = ()
    status_ids: tuple[int, ...] = ()
    price_from: Decimal | None = Field(default=None, ge=0)
    price_to: Decimal | None = Field(default=None, ge=0)
    order: str = "newest_first"
    interval_s: float = Field(default=300.0, ge=30.0)
    max_pages: int = Field(default=1, ge=1, le=10)
    per_page: int = Field(default=96, ge=1, le=96)
    enabled: bool = True

    @model_validator(mode="after")
    def _price_range(self) -> SearchQuery:
        if (
            self.price_from is not None
            and self.price_to is not None
            and self.price_to < self.price_from
        ):
            raise ValueError("price_to debe ser >= price_from")
        return self

    def to_params(self, page: int) -> dict[str, str]:
        """Parámetros de ``/api/v2/catalog/items`` para la página indicada."""
        params: dict[str, str] = {
            "search_text": self.search_text,
            "order": self.order,
            "page": str(page),
            "per_page": str(self.per_page),
        }
        if self.catalog_ids:
            params["catalog_ids"] = ",".join(map(str, self.catalog_ids))
        if self.brand_ids:
            params["brand_ids"] = ",".join(map(str, self.brand_ids))
        if self.status_ids:
            params["status_ids"] = ",".join(map(str, self.status_ids))
        if self.price_from is not None:
            params["price_from"] = _plain(self.price_from)
        if self.price_to is not None:
            params["price_to"] = _plain(self.price_to)
        if self.price_from is not None or self.price_to is not None:
            params["currency"] = "EUR"
        return params


def _plain(value: Decimal) -> str:
    """Formatea un Decimal sin notación científica ni ceros superfluos (150.00 → '150')."""
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


@dataclass(frozen=True, slots=True)
class Listing:
    """Anuncio de Vinted tal y como lo devuelve el catálogo, ya tipado."""

    id: int
    title: str
    price: Decimal
    currency: str
    url: str
    brand: str | None = None
    status_label: str | None = None
    """Etiqueta de estado localizada que muestra Vinted (p. ej. "Muy bueno")."""
    size_label: str | None = None
    service_fee: Decimal | None = None
    """Tarifa del comprador calculada por Vinted para este anuncio, si la API la expone."""
    total_price: Decimal | None = None
    """Precio + tarifa del comprador, si la API lo expone."""
    photo_urls: tuple[str, ...] = ()
    seller_id: int | None = None
    """ID seudónimo del vendedor. Por minimización de datos no se guarda su alias."""
    favourite_count: int | None = None
    view_count: int | None = None
    listed_at: datetime | None = None
    """Aproximación a la fecha de publicación (timestamp de la foto principal)."""


@dataclass(frozen=True, slots=True)
class CatalogPage:
    """Una página de resultados del catálogo."""

    listings: tuple[Listing, ...]
    current_page: int | None
    total_pages: int | None
    skipped: int = 0
    """Anuncios descartados por estar malformados."""

    @property
    def is_last(self) -> bool:
        if not self.listings:
            return True
        if self.current_page is None or self.total_pages is None:
            return False
        return self.current_page >= self.total_pages
