"""Estimación del precio de mercado a partir de anuncios comparables (ARCHITECTURE.md §3.2).

Con Vinted como canal de reventa, el mercado relevante es el propio Vinted: los anuncios
activos del mismo SKU y estado son exactamente la competencia contra la que vas a vender.

Método:

1. Peso por recencia: ``w = 2^(−edad / vida_media)``.
2. Limpieza robusta: fuera las observaciones a más de ``k · 1,4826 · MAD`` de la mediana
   (lotes, roturas no declaradas, precios trampa).
3. Percentiles ponderados. El precio de "venta rápida" es un percentil bajo (P30 por
   defecto): para vender rápido hay que estar entre los más baratos de tu competencia.
4. Si hay pocas observaciones del estado exacto, se agregan todos los estados del SKU
   normalizándolos con multiplicadores de estado (con penalización de confianza).
5. Confianza: ``min(1, √(n_eff / n_min))`` con ``n_eff = (Σw)² / Σw²``.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from vinted_arb.normalization.condition import Condition
from vinted_arb.normalization.extractor import ExtractedEntity
from vinted_arb.normalization.signals import RedFlag
from vinted_arb.valuation.fees import money

# Hipótesis iniciales relativas a "Muy bueno" (ARCHITECTURE.md §3.2): calibrar con datos propios.
CONDITION_MULTIPLIERS: dict[Condition, Decimal] = {
    Condition.NEW_SEALED: Decimal("1.20"),
    Condition.NEW: Decimal("1.10"),
    Condition.VERY_GOOD: Decimal("1.00"),
    Condition.GOOD: Decimal("0.90"),
    Condition.FAIR: Decimal("0.78"),
}

MIN_OBSERVATIONS = 3
MARKET_MIN_MATCH_CONFIDENCE = 0.80
# Además de las señales duras, estas distorsionan el precio de un comparable.
MARKET_EXCLUDED_FLAGS: frozenset[RedFlag] = frozenset(
    {RedFlag.SCREEN_DAMAGE, RedFlag.OFF_PLATFORM, RedFlag.AMBIGUOUS_BUNDLE, RedFlag.SWAP_ONLY}
)
POOLING_CONFIDENCE_FACTOR = 0.85
MAD_SCALE = 1.4826  # convierte la MAD en una estimación de σ para datos normales


@dataclass(frozen=True, slots=True)
class PricePoint:
    price: Decimal
    condition: Condition
    observed_at: datetime
    listing_id: int | None = None


@dataclass(frozen=True, slots=True)
class MarketEstimate:
    """Distribución de precios pedidos comparables, ya ajustada al estado objetivo."""

    condition: Condition
    quick_sale: Decimal
    """Percentil configurado (P30 por defecto): precio pedido para vender rápido."""
    p25: Decimal
    p50: Decimal
    p75: Decimal
    n: int
    n_eff: float
    outliers_removed: int
    pooled: bool
    """True si se agregaron otros estados normalizados con multiplicadores."""
    confidence: float


def is_market_eligible(entity: ExtractedEntity) -> bool:
    """¿Sirve este anuncio como comparable de mercado para su SKU?"""
    return (
        entity.product is not None
        and not entity.product.missing
        and entity.match_confidence >= MARKET_MIN_MATCH_CONFIDENCE
        and not entity.hard_flags
        and not (entity.red_flags & MARKET_EXCLUDED_FLAGS)
        and entity.condition in CONDITION_MULTIPLIERS
    )


def weighted_quantile(values: Sequence[float], weights: Sequence[float], q: float) -> float:
    """Cuantil ponderado con interpolación lineal sobre los puntos medios de cada peso.

    Con pesos iguales coincide con la definición de Hazen (``(i − 0,5) / n``).
    """
    if not values or len(values) != len(weights):
        raise ValueError("values y weights deben tener la misma longitud (no vacía)")
    if not 0 <= q <= 1:
        raise ValueError("q debe estar en [0, 1]")
    pairs = sorted(zip(values, weights, strict=True))
    total = sum(w for _, w in pairs)
    if total <= 0:
        raise ValueError("La suma de pesos debe ser positiva")
    positions: list[float] = []
    cumulative = 0.0
    for _, weight in pairs:
        positions.append((cumulative + weight / 2) / total)
        cumulative += weight
    if q <= positions[0]:
        return pairs[0][0]
    if q >= positions[-1]:
        return pairs[-1][0]
    for i in range(1, len(pairs)):
        if q <= positions[i]:
            lo_pos, hi_pos = positions[i - 1], positions[i]
            lo_val, hi_val = pairs[i - 1][0], pairs[i][0]
            fraction = (q - lo_pos) / (hi_pos - lo_pos) if hi_pos > lo_pos else 0.0
            return lo_val + fraction * (hi_val - lo_val)
    return pairs[-1][0]  # pragma: no cover - inalcanzable


def _median(values: Sequence[float]) -> float:
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


def mad_inliers(values: Sequence[float], k: float = 3.0) -> list[bool]:
    """Máscara de valores no atípicos según la desviación absoluta mediana (MAD).

    Si la MAD es 0 (muchos precios idénticos) se aceptan los valores a ±25 % de la mediana.
    """
    if not values:
        return []
    median = _median(values)
    mad = _median([abs(v - median) for v in values])
    if mad == 0:
        return [abs(v - median) <= 0.25 * abs(median) for v in values]
    limit = k * MAD_SCALE * mad
    return [abs(v - median) <= limit for v in values]


def recency_weight(observed_at: datetime, now: datetime, half_life_days: float) -> float:
    age_days = max(0.0, (now - observed_at).total_seconds() / 86400)
    return float(2 ** (-age_days / half_life_days))


def effective_sample_size(weights: Sequence[float]) -> float:
    total = sum(weights)
    squares = sum(w * w for w in weights)
    return (total * total / squares) if squares > 0 else 0.0


def _estimate(
    prices: Sequence[float],
    weights: Sequence[float],
    *,
    condition: Condition,
    percentile: float,
    min_samples: int,
    pooled: bool,
    scale: Decimal = Decimal(1),
) -> MarketEstimate | None:
    mask = mad_inliers(prices)
    kept = [(p, w) for (p, w, ok) in zip(prices, weights, mask, strict=True) if ok]
    if len(kept) < MIN_OBSERVATIONS:
        return None
    values = [p for p, _ in kept]
    kept_weights = [w for _, w in kept]
    n_eff = effective_sample_size(kept_weights)
    confidence = min(1.0, math.sqrt(n_eff / min_samples))
    if pooled:
        confidence *= POOLING_CONFIDENCE_FACTOR

    def q(level: float) -> Decimal:
        return money(Decimal(str(weighted_quantile(values, kept_weights, level))) * scale)

    return MarketEstimate(
        condition=condition,
        quick_sale=q(percentile),
        p25=q(0.25),
        p50=q(0.50),
        p75=q(0.75),
        n=len(kept),
        n_eff=round(n_eff, 2),
        outliers_removed=len(prices) - len(kept),
        pooled=pooled,
        confidence=round(confidence, 3),
    )


def estimate_market(
    points: Sequence[PricePoint],
    *,
    condition: Condition,
    now: datetime,
    percentile: float = 0.30,
    half_life_days: float = 14.0,
    min_samples: int = 12,
    multipliers: Mapping[Condition, Decimal] = CONDITION_MULTIPLIERS,
) -> MarketEstimate | None:
    """Estimación de mercado para ``condition``. ``None`` si no hay datos suficientes."""
    if condition not in multipliers:
        return None  # "para piezas" / desconocido: no se valoran contra el mercado
    usable = [p for p in points if p.condition in multipliers and p.price > 0]

    exact = [p for p in usable if p.condition is condition]
    exact_weights = [recency_weight(p.observed_at, now, half_life_days) for p in exact]
    if exact and effective_sample_size(exact_weights) >= min_samples:
        return _estimate(
            [float(p.price) for p in exact],
            exact_weights,
            condition=condition,
            percentile=percentile,
            min_samples=min_samples,
            pooled=False,
        )

    # Agregación entre estados: todo se lleva a equivalente "Muy bueno" y se reescala.
    pooled_prices = [float(p.price / multipliers[p.condition]) for p in usable]
    pooled_weights = [recency_weight(p.observed_at, now, half_life_days) for p in usable]
    pooled_is_better = len(usable) > len(exact)
    if pooled_is_better:
        return _estimate(
            pooled_prices,
            pooled_weights,
            condition=condition,
            percentile=percentile,
            min_samples=min_samples,
            pooled=True,
            scale=multipliers[condition],
        )
    if not exact:
        return None
    return _estimate(
        [float(p.price) for p in exact],
        exact_weights,
        condition=condition,
        percentile=percentile,
        min_samples=min_samples,
        pooled=False,
    )
