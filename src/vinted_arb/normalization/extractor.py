"""Extractor de entidades: anuncio en texto libre → entidad estructurada para valorar."""

from __future__ import annotations

from dataclasses import dataclass

from vinted_arb.normalization.catalog import (
    MATCHERS,
    MatchContext,
    ProductMatch,
    looks_like_accessory,
)
from vinted_arb.normalization.condition import Condition, resolve_condition
from vinted_arb.normalization.signals import (
    HARD_FLAGS,
    Accessory,
    RedFlag,
    detect_red_flags,
    extract_accessories,
)
from vinted_arb.normalization.text import (
    extract_battery_health,
    extract_storage_gb,
    normalize_text,
)

# Súbelo al cambiar reglas del normalizador: las entidades guardadas se recalculan solas.
NORMALIZER_VERSION = 1


@dataclass(frozen=True, slots=True)
class ExtractedEntity:
    normalized_title: str
    product: ProductMatch | None
    alternatives: tuple[ProductMatch, ...]
    """Otros productos detectados (lotes con varios dispositivos)."""
    storage_gb: int | None
    battery_health: int | None
    condition: Condition
    accessories: frozenset[Accessory]
    red_flags: frozenset[RedFlag]

    @property
    def sku(self) -> str | None:
        return self.product.sku if self.product else None

    @property
    def match_confidence(self) -> float:
        return self.product.confidence if self.product else 0.0

    @property
    def hard_flags(self) -> frozenset[RedFlag]:
        return self.red_flags & HARD_FLAGS

    @property
    def is_candidate(self) -> bool:
        """¿Merece pasar a valoración? Producto reconocido y sin señales de descarte."""
        return self.product is not None and not self.hard_flags


def match_products(ctx: MatchContext) -> list[ProductMatch]:
    """Todos los productos reconocidos, de mayor a menor confianza (uno por modelo)."""
    matches: dict[str, ProductMatch] = {}
    for matcher in MATCHERS:
        found = matcher(ctx)
        if found is None:
            continue
        current = matches.get(found.model)
        if current is None or found.confidence > current.confidence:
            matches[found.model] = found
    return sorted(
        matches.values(), key=lambda m: (m.confidence, m.span_start is not None), reverse=True
    )


def extract(
    title: str,
    description: str = "",
    *,
    status_label: str | None = None,
) -> ExtractedEntity:
    """Normaliza un anuncio y extrae producto, estado, accesorios y señales de alarma."""
    title_norm = normalize_text(title)
    description_norm = normalize_text(description) if description else ""
    full = f"{title_norm} {description_norm}".strip() if description_norm else title_norm
    ctx = MatchContext(title=title_norm, full=full)

    products = match_products(ctx)
    product = products[0] if products else None
    alternatives = tuple(products[1:])

    battery = extract_battery_health(full)
    flags = set(detect_red_flags(title_norm, full, battery_health=battery))
    if product is not None and looks_like_accessory(title_norm, product):
        flags.add(RedFlag.ACCESSORY_ONLY)
    title_products = {p.model for p in products if p.span_start is not None}
    if len(title_products) > 1:
        flags.add(RedFlag.AMBIGUOUS_BUNDLE)

    return ExtractedEntity(
        normalized_title=title_norm,
        product=product,
        alternatives=alternatives,
        storage_gb=extract_storage_gb(title_norm) or extract_storage_gb(full),
        battery_health=battery,
        condition=resolve_condition(status_label, full),
        accessories=extract_accessories(full),
        red_flags=frozenset(flags),
    )
