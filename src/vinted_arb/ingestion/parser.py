"""Parser del JSON de ``/api/v2/catalog/items``, tolerante a variantes del esquema.

La API interna de Vinted ha cambiado de forma varias veces. Variantes conocidas:

* ``price``: cadena (``"45.0"``) o objeto (``{"amount": "45.0", "currency_code": "EUR"}``).
* ``total_item_price`` / ``service_fee``: mismas dos formas; pueden faltar.
* ``currency``: a nivel de ítem en el esquema antiguo.
* ``url``: absoluta o relativa; si falta se construye con el ID.
* Fecha de publicación: no viene explícita en el catálogo; se aproxima con
  ``photo.high_resolution.timestamp``.

Un anuncio malformado se descarta (y se cuenta en ``skipped``) sin tumbar la página. Si
falta la clave ``items`` se lanza :class:`SchemaDriftError`: eso es un cambio estructural.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from vinted_arb.ingestion.errors import SchemaDriftError
from vinted_arb.models import CatalogPage, Listing

logger = logging.getLogger(__name__)


class ListingParseError(ValueError):
    """Un anuncio concreto no tiene los campos mínimos."""


def _parse_money(value: Any) -> tuple[Decimal, str | None] | None:
    if value is None:
        return None
    amount: Any
    currency: str | None = None
    if isinstance(value, Mapping):
        amount = value.get("amount")
        raw_currency = value.get("currency_code") or value.get("currency")
        currency = str(raw_currency) if raw_currency else None
    else:
        amount = value
    if amount is None or isinstance(amount, bool):
        return None
    try:
        parsed = Decimal(str(amount))
    except InvalidOperation:
        return None
    if not parsed.is_finite() or parsed < 0:
        return None
    return parsed, currency


def _opt_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _opt_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _photo_urls(item: Mapping[str, Any]) -> tuple[str, ...]:
    urls: list[str] = []
    photos: list[Any] = []
    if isinstance(item.get("photo"), Mapping):
        photos.append(item["photo"])
    if isinstance(item.get("photos"), list):
        photos.extend(item["photos"])
    for photo in photos:
        if not isinstance(photo, Mapping):
            continue
        url = photo.get("full_size_url") or photo.get("url")
        if isinstance(url, str) and url and url not in urls:
            urls.append(url)
    return tuple(urls)


def _listed_at(item: Mapping[str, Any]) -> datetime | None:
    photo = item.get("photo")
    if not isinstance(photo, Mapping):
        return None
    high_res = photo.get("high_resolution")
    if not isinstance(high_res, Mapping):
        return None
    ts = _opt_int(high_res.get("timestamp"))
    if ts is None or ts <= 0:
        return None
    try:
        return datetime.fromtimestamp(ts, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


def _listing_url(item: Mapping[str, Any], item_id: int, base_url: str) -> str:
    url = item.get("url")
    if isinstance(url, str) and url:
        if url.startswith("http"):
            return url
        return f"{base_url.rstrip('/')}/{url.lstrip('/')}"
    path = item.get("path")
    if isinstance(path, str) and path:
        return f"{base_url.rstrip('/')}/{path.lstrip('/')}"
    return f"{base_url.rstrip('/')}/items/{item_id}"


def parse_item(item: Mapping[str, Any], *, base_url: str, default_currency: str = "EUR") -> Listing:
    """Convierte un ítem del catálogo en :class:`Listing`."""
    item_id = _opt_int(item.get("id"))
    if item_id is None or item_id <= 0:
        raise ListingParseError("Falta 'id'")
    title = _opt_str(item.get("title"))
    if title is None:
        raise ListingParseError(f"Ítem {item_id} sin 'title'")
    price = _parse_money(item.get("price"))
    if price is None:
        raise ListingParseError(f"Ítem {item_id} sin precio válido")
    amount, currency = price
    currency = currency or _opt_str(item.get("currency")) or default_currency

    service_fee = _parse_money(item.get("service_fee"))
    total_price = _parse_money(item.get("total_item_price"))

    user = item.get("user")
    seller_id = _opt_int(user.get("id")) if isinstance(user, Mapping) else None

    status = item.get("status")
    status_label = _opt_str(status) if isinstance(status, str) else None

    brand = _opt_str(item.get("brand_title"))
    if brand is None and isinstance(item.get("brand"), Mapping):
        brand = _opt_str(item["brand"].get("title"))

    return Listing(
        id=item_id,
        title=title,
        price=amount,
        currency=currency.upper(),
        url=_listing_url(item, item_id, base_url),
        brand=brand,
        status_label=status_label,
        size_label=_opt_str(item.get("size_title")),
        service_fee=service_fee[0] if service_fee else None,
        total_price=total_price[0] if total_price else None,
        photo_urls=_photo_urls(item),
        seller_id=seller_id,
        favourite_count=_opt_int(item.get("favourite_count")),
        view_count=_opt_int(item.get("view_count")),
        listed_at=_listed_at(item),
    )


def parse_catalog_page(payload: Any, *, base_url: str) -> CatalogPage:
    """Convierte la respuesta completa del catálogo en :class:`CatalogPage`."""
    if not isinstance(payload, Mapping):
        raise SchemaDriftError(f"Se esperaba un objeto JSON y llegó {type(payload).__name__}")
    items = payload.get("items")
    if not isinstance(items, list):
        raise SchemaDriftError(
            f"Respuesta sin lista 'items' (claves: {sorted(payload.keys())[:10]})"
        )

    listings: list[Listing] = []
    seen_ids: set[int] = set()
    skipped = 0
    for raw in items:
        if not isinstance(raw, Mapping):
            skipped += 1
            continue
        try:
            listing = parse_item(raw, base_url=base_url)
        except ListingParseError as exc:
            skipped += 1
            logger.debug("Anuncio descartado: %s", exc)
            continue
        if listing.id in seen_ids:
            continue
        seen_ids.add(listing.id)
        listings.append(listing)

    if items and not listings:
        # Todos los anuncios malformados: casi seguro un cambio de esquema.
        raise SchemaDriftError(f"Ninguno de los {len(items)} anuncios se pudo interpretar")
    if skipped:
        logger.warning("%d anuncios malformados descartados en la página", skipped)

    pagination = payload.get("pagination")
    current_page = total_pages = None
    if isinstance(pagination, Mapping):
        current_page = _opt_int(pagination.get("current_page"))
        total_pages = _opt_int(pagination.get("total_pages"))

    return CatalogPage(
        listings=tuple(listings),
        current_page=current_page,
        total_pages=total_pages,
        skipped=skipped,
    )
