"""Comisiones de plataforma y cálculo del margen neto de una operación de arbitraje.

Fórmula (ver ARCHITECTURE.md §3.2)::

    Margen neto = P_venta
                  − P_compra
                  − Tarifa Vinted (comprador)
                  − Comisiones del canal de reventa
                  − Envío de ida (Vinted → nosotros)
                  − Envío de vuelta (nosotros → comprador final, solo si lo paga el vendedor)
                  − Embalaje
                  − Promoción del anuncio (p. ej. "Bump" de Vinted)
                  − Reserva de riesgo (fraude, defectos ocultos, devoluciones)
                  − IVA sobre el margen (REBU, solo si operas como profesional)

Todas las cantidades monetarias son ``Decimal`` redondeadas a céntimos con ROUND_HALF_UP.
Los valores por defecto reflejan las condiciones públicas conocidas en octubre de 2026 y
deben revisarse periódicamente: las plataformas cambian sus tarifas sin previo aviso.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

CENT = Decimal("0.01")
ZERO = Decimal("0.00")
INFINITE_ROI = Decimal("Infinity")

MoneyLike = Decimal | int | str


def money(value: MoneyLike) -> Decimal:
    """Convierte a ``Decimal`` redondeado a céntimos.

    No acepta ``float`` a propósito: los importes deben llegar como texto o ``Decimal``
    (los precios de la API de Vinted ya vienen como cadenas, p. ej. ``"45.0"``).
    """
    if isinstance(value, float):
        raise TypeError("Usa Decimal o str para importes monetarios, no float")
    return Decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)


@dataclass(frozen=True, slots=True)
class VintedBuyerFee:
    """Tarifa obligatoria que paga el comprador en Vinted.

    Desde octubre de 2026 se llama "Vinted fee" (antes "Protección al comprador");
    Vinted declara que se calcula igual: importe fijo + porcentaje del precio del artículo.
    Si la API devuelve ``service_fee`` para un anuncio concreto, usa ese valor exacto
    mediante ``vinted_fee_override`` en :func:`compute_margin`.
    """

    fixed: Decimal = Decimal("0.70")
    rate: Decimal = Decimal("0.05")

    def __call__(self, item_price: Decimal) -> Decimal:
        if item_price < 0:
            raise ValueError("El precio del artículo no puede ser negativo")
        return money(self.fixed + self.rate * item_price)


@dataclass(frozen=True, slots=True)
class ResaleChannel:
    """Canal de reventa y estructura de comisiones que soporta el vendedor."""

    name: str
    rate: Decimal = ZERO
    """Porcentaje sobre la base de cálculo (0.115 = 11,5 %)."""
    fixed_per_order: Decimal = ZERO
    small_order_threshold: Decimal | None = None
    """Por debajo de este importe se cobra ``small_order_fixed`` y no ``fixed_per_order``."""
    small_order_fixed: Decimal | None = None
    fee_includes_shipping: bool = False
    """eBay calcula su porcentaje sobre el total cobrado al comprador (artículo + envío)."""
    seller_pays_shipping: bool = False
    """True si el vendedor absorbe el envío de salida (p. ej. estrategia de "envío gratis")."""

    def fees(self, sale_price: Decimal, shipping_charged: Decimal = ZERO) -> Decimal:
        """Comisiones del canal para una venta a ``sale_price``.

        ``shipping_charged`` es el envío que paga el comprador final; solo afecta a
        canales cuyo porcentaje se calcula sobre el total (``fee_includes_shipping``).
        """
        if sale_price < 0 or shipping_charged < 0:
            raise ValueError("Importes negativos no permitidos")
        base = sale_price + (shipping_charged if self.fee_includes_shipping else ZERO)
        fixed = self.fixed_per_order
        if (
            self.small_order_threshold is not None
            and self.small_order_fixed is not None
            and base < self.small_order_threshold
        ):
            fixed = self.small_order_fixed
        return money(self.rate * base + fixed)


# --- Canales predefinidos -------------------------------------------------------------
# Wallapop: el vendedor no paga comisión; el comprador paga envío + protección.
WALLAPOP = ResaleChannel(name="wallapop")
# Vinted (UE): el vendedor no paga comisión; el comprador paga la "Vinted fee".
VINTED = ResaleChannel(name="vinted")
# eBay.es particular: 11,5 % del total + 0,35 € por pedido (0,05 € si el pedido < 10 €).
EBAY_ES_PRIVATE = ResaleChannel(
    name="ebay_es_private",
    rate=Decimal("0.115"),
    fixed_per_order=Decimal("0.35"),
    small_order_threshold=Decimal("10"),
    small_order_fixed=Decimal("0.05"),
    fee_includes_shipping=True,
)
# Venta en mano: sin comisiones ni envío de salida.
LOCAL_CASH = ResaleChannel(name="local_cash")

CHANNELS: dict[str, ResaleChannel] = {
    channel.name: channel for channel in (WALLAPOP, VINTED, EBAY_ES_PRIVATE, LOCAL_CASH)
}


@dataclass(frozen=True, slots=True)
class DealCosts:
    """Supuestos de costes de una operación, configurables por categoría o búsqueda."""

    inbound_shipping: Decimal = Decimal("4.50")
    """Envío Vinted → nosotros (lo pagamos como compradores)."""
    outbound_shipping: Decimal = Decimal("6.00")
    """Envío nosotros → comprador final. Solo es coste si ``seller_pays_shipping``."""
    packaging: Decimal = Decimal("1.50")
    promotion: Decimal = Decimal("0.00")
    """Promoción pagada por el vendedor (p. ej. "Bump" de Vinted de 3 o 7 días)."""
    risk_reserve_rate: Decimal = Decimal("0.03")
    """Pérdida esperada por fraude/defectos/devoluciones como fracción del precio de compra."""
    rebu_vat_rate: Decimal | None = None
    """IVA del Régimen Especial de Bienes Usados (0.21 en España). ``None`` si eres particular."""

    def __post_init__(self) -> None:
        for name in (
            "inbound_shipping",
            "outbound_shipping",
            "packaging",
            "promotion",
            "risk_reserve_rate",
        ):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} no puede ser negativo")
        if self.rebu_vat_rate is not None and self.rebu_vat_rate < 0:
            raise ValueError("rebu_vat_rate no puede ser negativo")


@dataclass(frozen=True, slots=True)
class MarginBreakdown:
    """Desglose completo de una operación. Todas las cifras en euros."""

    channel: str
    sale_price: Decimal
    purchase_price: Decimal
    vinted_fee: Decimal
    inbound_shipping: Decimal
    resale_fees: Decimal
    outbound_shipping: Decimal
    packaging: Decimal
    risk_reserve: Decimal
    vat_on_margin: Decimal
    promotion: Decimal = ZERO

    @property
    def total_costs(self) -> Decimal:
        return (
            self.purchase_price
            + self.vinted_fee
            + self.inbound_shipping
            + self.resale_fees
            + self.outbound_shipping
            + self.packaging
            + self.promotion
            + self.risk_reserve
            + self.vat_on_margin
        )

    @property
    def cash_invested(self) -> Decimal:
        """Capital que sale de tu bolsillo antes de cobrar la reventa."""
        return (
            self.purchase_price
            + self.vinted_fee
            + self.inbound_shipping
            + self.packaging
            + self.promotion
            + self.outbound_shipping
        )

    @property
    def net_margin(self) -> Decimal:
        return self.sale_price - self.total_costs

    @property
    def roi(self) -> Decimal:
        """Margen neto / capital invertido (0.25 = 25 %)."""
        if self.cash_invested == 0:
            return INFINITE_ROI
        return (self.net_margin / self.cash_invested).quantize(Decimal("0.0001"), ROUND_HALF_UP)

    def as_dict(self) -> dict[str, str]:
        return {
            "channel": self.channel,
            "sale_price": str(self.sale_price),
            "purchase_price": str(self.purchase_price),
            "vinted_fee": str(self.vinted_fee),
            "inbound_shipping": str(self.inbound_shipping),
            "resale_fees": str(self.resale_fees),
            "outbound_shipping": str(self.outbound_shipping),
            "packaging": str(self.packaging),
            "promotion": str(self.promotion),
            "risk_reserve": str(self.risk_reserve),
            "vat_on_margin": str(self.vat_on_margin),
            "total_costs": str(self.total_costs),
            "cash_invested": str(self.cash_invested),
            "net_margin": str(self.net_margin),
            "roi": str(self.roi),
        }


def rebu_vat(sale_price: Decimal, purchase_price: Decimal, rate: Decimal) -> Decimal:
    """Cuota de IVA en REBU: se aplica sobre el margen, que ya incluye el impuesto.

    cuota = margen × t / (1 + t). Si el margen es negativo no hay cuota.
    """
    margin = sale_price - purchase_price
    if margin <= 0:
        return ZERO
    return money(margin * rate / (1 + rate))


def compute_margin(
    *,
    purchase_price: MoneyLike,
    sale_price: MoneyLike,
    channel: ResaleChannel = WALLAPOP,
    costs: DealCosts | None = None,
    buyer_fee: VintedBuyerFee | None = None,
    vinted_fee_override: MoneyLike | None = None,
) -> MarginBreakdown:
    """Calcula el desglose de margen de comprar en Vinted y revender en ``channel``."""
    costs = costs or DealCosts()
    buyer_fee = buyer_fee or VintedBuyerFee()
    purchase = money(purchase_price)
    sale = money(sale_price)
    if purchase < 0 or sale < 0:
        raise ValueError("Los precios no pueden ser negativos")

    vinted_fee = (
        money(vinted_fee_override) if vinted_fee_override is not None else buyer_fee(purchase)
    )
    outbound_cost = money(costs.outbound_shipping) if channel.seller_pays_shipping else ZERO
    shipping_charged = ZERO if channel.seller_pays_shipping else money(costs.outbound_shipping)
    vat = rebu_vat(sale, purchase, costs.rebu_vat_rate) if costs.rebu_vat_rate is not None else ZERO

    return MarginBreakdown(
        channel=channel.name,
        sale_price=sale,
        purchase_price=purchase,
        vinted_fee=vinted_fee,
        inbound_shipping=money(costs.inbound_shipping),
        resale_fees=channel.fees(sale, shipping_charged),
        outbound_shipping=outbound_cost,
        packaging=money(costs.packaging),
        promotion=money(costs.promotion),
        risk_reserve=money(purchase * costs.risk_reserve_rate),
        vat_on_margin=vat,
    )


def max_purchase_price(
    *,
    sale_price: MoneyLike,
    channel: ResaleChannel = WALLAPOP,
    min_net_margin: MoneyLike = ZERO,
    min_roi: Decimal = ZERO,
    costs: DealCosts | None = None,
    buyer_fee: VintedBuyerFee | None = None,
) -> Decimal | None:
    """Precio máximo de compra en Vinted que cumple ambos umbrales a la vez.

    Devuelve ``None`` si ni comprando a 0 € se alcanzan. Usa búsqueda binaria en céntimos:
    con umbrales no negativos, el margen y el ROI decrecen al subir el precio de compra,
    así que el conjunto de precios válidos es un intervalo [0, P_max].
    """
    min_net = money(min_net_margin)
    if min_net < 0 or min_roi < 0:
        raise ValueError("Los umbrales deben ser no negativos")
    sale = money(sale_price)

    def meets(cents: int) -> bool:
        breakdown = compute_margin(
            purchase_price=Decimal(cents) / 100,
            sale_price=sale,
            channel=channel,
            costs=costs,
            buyer_fee=buyer_fee,
        )
        return breakdown.net_margin >= min_net and breakdown.roi >= min_roi

    lo, hi = 0, int(sale * 100)
    if not meets(lo):
        return None
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if meets(mid):
            lo = mid
        else:
            hi = mid - 1
    return money(Decimal(lo) / 100)
