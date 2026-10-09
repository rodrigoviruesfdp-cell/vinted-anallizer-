"""Módulo 2 (núcleo): normalización de anuncios y extracción de entidades."""

from vinted_arb.normalization.catalog import Family, ProductMatch
from vinted_arb.normalization.condition import Condition, resolve_condition
from vinted_arb.normalization.extractor import ExtractedEntity, extract
from vinted_arb.normalization.signals import HARD_FLAGS, Accessory, RedFlag
from vinted_arb.normalization.text import normalize_text

__all__ = [
    "HARD_FLAGS",
    "Accessory",
    "Condition",
    "ExtractedEntity",
    "Family",
    "ProductMatch",
    "RedFlag",
    "extract",
    "normalize_text",
    "resolve_condition",
]
