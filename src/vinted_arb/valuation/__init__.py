"""Módulo 3 (núcleo): comisiones, margen neto y precio máximo de compra."""

from vinted_arb.valuation.fees import (
    CHANNELS,
    EBAY_ES_PRIVATE,
    LOCAL_CASH,
    VINTED,
    WALLAPOP,
    DealCosts,
    MarginBreakdown,
    ResaleChannel,
    VintedBuyerFee,
    compute_margin,
    max_purchase_price,
    money,
    rebu_vat,
)

__all__ = [
    "CHANNELS",
    "EBAY_ES_PRIVATE",
    "LOCAL_CASH",
    "VINTED",
    "WALLAPOP",
    "DealCosts",
    "MarginBreakdown",
    "ResaleChannel",
    "VintedBuyerFee",
    "compute_margin",
    "max_purchase_price",
    "money",
    "rebu_vat",
]
