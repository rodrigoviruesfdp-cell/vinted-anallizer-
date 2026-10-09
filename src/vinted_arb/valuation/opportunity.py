"""Evaluación de oportunidades: margen, riesgo, puntuación y decisión (ARCHITECTURE.md §3.3).

    Score = Margen neto · C_match · C_fmv · (1 − R),     R = 1 − Π(1 − r_i)

La liquidez (P_venta) se incorporará cuando M1.1 detecte ventas reales en Vinted; hasta
entonces vale 1 y el control de rotación lo hace el percentil de venta rápida.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum

from vinted_arb.models import Listing
from vinted_arb.normalization.condition import Condition
from vinted_arb.normalization.extractor import ExtractedEntity
from vinted_arb.normalization.signals import RedFlag
from vinted_arb.valuation.fees import (
    VINTED,
    DealCosts,
    MarginBreakdown,
    ResaleChannel,
    compute_margin,
)
from vinted_arb.valuation.reference import Reference

# Riesgo por señal blanda (las duras descartan directamente).
SOFT_FLAG_RISK: dict[RedFlag, float] = {
    RedFlag.OFF_PLATFORM: 0.6,
    RedFlag.SCREEN_DAMAGE: 0.5,
    RedFlag.AMBIGUOUS_BUNDLE: 0.4,
    RedFlag.SWAP_ONLY: 0.3,
    RedFlag.LOW_BATTERY: 0.15,
}
UNKNOWN_CONDITION_RISK = 0.1
# Descuento "demasiado bueno": de 0 de riesgo a ≤40 % hasta 0,6 a ≥70 %.
IMPLAUSIBLE_DISCOUNT_START = Decimal("0.40")
IMPLAUSIBLE_DISCOUNT_FULL = Decimal("0.70")
IMPLAUSIBLE_DISCOUNT_MAX_RISK = 0.6


class Decision(StrEnum):
    ALERT = "alert"
    WATCH = "watch"
    """Margen positivo pero algún umbral sin cumplir: se guarda para el análisis en modo sombra."""
    REJECT = "reject"


@dataclass(frozen=True, slots=True)
class EvaluationParams:
    channel: ResaleChannel = VINTED
    costs: DealCosts = field(default_factory=DealCosts)
    min_net_margin: Decimal = Decimal("30")
    min_roi: Decimal = Decimal("0.20")
    min_match_confidence: float = 0.80
    min_fmv_confidence: float = 0.60
    max_risk: float = 0.50


@dataclass(frozen=True, slots=True)
class Opportunity:
    listing: Listing
    entity: ExtractedEntity
    condition: Condition
    """Estado usado para valorar ("desconocido" se valora como "bueno", por prudencia)."""
    reference: Reference | None
    breakdown: MarginBreakdown | None
    discount: Decimal | None
    """1 − precio / precio de venta esperado."""
    risk: float
    risk_factors: dict[str, float]
    score: float
    decision: Decision
    reasons: tuple[str, ...]

    @property
    def net_margin(self) -> Decimal | None:
        return self.breakdown.net_margin if self.breakdown else None

    @property
    def roi(self) -> Decimal | None:
        return self.breakdown.roi if self.breakdown else None


def valuation_condition(condition: Condition) -> Condition:
    """Estado con el que se valora: "desconocido" cuenta como "bueno", por prudencia."""
    return Condition.GOOD if condition is Condition.UNKNOWN else condition


def implausible_discount_risk(discount: Decimal) -> float:
    if discount <= IMPLAUSIBLE_DISCOUNT_START:
        return 0.0
    span = IMPLAUSIBLE_DISCOUNT_FULL - IMPLAUSIBLE_DISCOUNT_START
    fraction = min(Decimal(1), (discount - IMPLAUSIBLE_DISCOUNT_START) / span)
    return round(float(fraction) * IMPLAUSIBLE_DISCOUNT_MAX_RISK, 3)


def risk_factors(entity: ExtractedEntity, discount: Decimal | None) -> dict[str, float]:
    factors = {
        flag.value: SOFT_FLAG_RISK[flag] for flag in entity.red_flags if flag in SOFT_FLAG_RISK
    }
    if entity.condition is Condition.UNKNOWN:
        factors["unknown_condition"] = UNKNOWN_CONDITION_RISK
    if discount is not None:
        implausible = implausible_discount_risk(discount)
        if implausible > 0:
            factors["implausible_discount"] = implausible
    return factors


def combine_risk(factors: dict[str, float]) -> float:
    survival = 1.0
    for value in factors.values():
        survival *= 1.0 - value
    return round(1.0 - survival, 3)


def _eur(value: Decimal) -> str:
    return f"{value:.2f} €".replace(".", ",")


def evaluate(
    listing: Listing,
    entity: ExtractedEntity,
    reference: Reference | None,
    params: EvaluationParams,
) -> Opportunity:
    """Evalúa un anuncio frente a su precio de referencia."""
    condition = valuation_condition(entity.condition)

    def reject(*reasons: str, factors: dict[str, float] | None = None) -> Opportunity:
        factors = factors or {}
        return Opportunity(
            listing=listing,
            entity=entity,
            condition=condition,
            reference=reference,
            breakdown=None,
            discount=None,
            risk=combine_risk(factors),
            risk_factors=factors,
            score=0.0,
            decision=Decision.REJECT,
            reasons=reasons,
        )

    if entity.product is None:
        return reject("producto no reconocido")
    if entity.hard_flags:
        return reject(
            "señales de descarte: " + ", ".join(sorted(f.value for f in entity.hard_flags))
        )
    if entity.match_confidence < params.min_match_confidence:
        missing = ", ".join(entity.product.missing) or "—"
        return reject(
            f"identificación insuficiente ({entity.match_confidence:.2f}; falta: {missing})"
        )
    if reference is None:
        return reject(f"sin precio de referencia para {entity.sku} ({condition.value})")

    breakdown = compute_margin(
        purchase_price=listing.price,
        sale_price=reference.sale_price,
        channel=params.channel,
        costs=params.costs,
        vinted_fee_override=listing.service_fee,
    )
    discount = (
        Decimal(1) - listing.price / reference.sale_price
        if reference.sale_price > 0
        else Decimal(0)
    ).quantize(Decimal("0.001"))
    factors = risk_factors(entity, discount)
    risk = combine_risk(factors)
    net = breakdown.net_margin
    score = (
        float(net) * entity.match_confidence * reference.confidence * (1 - risk) if net > 0 else 0.0
    )

    shortfalls: list[str] = []
    if net < params.min_net_margin:
        shortfalls.append(f"margen {_eur(net)} < {_eur(params.min_net_margin)}")
    if breakdown.roi < params.min_roi:
        shortfalls.append(f"ROI {breakdown.roi:.0%} < {params.min_roi:.0%}")
    if reference.confidence < params.min_fmv_confidence:
        shortfalls.append(
            f"referencia poco fiable ({reference.confidence:.2f} < {params.min_fmv_confidence})"
        )
    if risk > params.max_risk:
        detail = ", ".join(f"{k}={v}" for k, v in sorted(factors.items()))
        shortfalls.append(f"riesgo {risk:.2f} > {params.max_risk} ({detail})")

    if not shortfalls:
        decision = Decision.ALERT
        reasons: tuple[str, ...] = (
            f"margen {_eur(net)} · ROI {breakdown.roi:.0%} · descuento {discount:.0%}",
        )
    elif net > 0:
        decision = Decision.WATCH
        reasons = tuple(shortfalls)
    else:
        decision = Decision.REJECT
        reasons = tuple(shortfalls)

    return Opportunity(
        listing=listing,
        entity=entity,
        condition=condition,
        reference=reference,
        breakdown=breakdown,
        discount=discount,
        risk=risk,
        risk_factors=factors,
        score=round(score, 2),
        decision=decision,
        reasons=reasons,
    )
