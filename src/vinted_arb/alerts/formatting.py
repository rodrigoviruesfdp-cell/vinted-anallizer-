"""Formato de las alertas: desglose de margen legible en el móvil."""

from __future__ import annotations

import html
from decimal import Decimal
from typing import Any

from vinted_arb.valuation.opportunity import Opportunity

TELEGRAM_CAPTION_LIMIT = 1024
DISCORD_COLOR_GOOD = 0x2E9E5B
DISCORD_COLOR_WATCH = 0xD9A21B


def eur(value: Decimal) -> str:
    """1234.5 → '1.234,50 €' (formato español)."""
    text = f"{value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"{text} €"


def _pct(value: Decimal | None) -> str:
    return "—" if value is None or not value.is_finite() else f"{value:.0%}"


def summary_lines(opp: Opportunity) -> list[str]:
    """Líneas de texto plano comunes a todos los canales."""
    b = opp.breakdown
    ref = opp.reference
    lines: list[str] = []
    if b is None or ref is None:
        return [f"{opp.listing.title} · {eur(opp.listing.price)}", *opp.reasons]

    fee_note = "exacta de Vinted" if opp.listing.service_fee is not None else "estimada"
    lines.append(f"Compra: {eur(b.purchase_price)} + tarifa {eur(b.vinted_fee)} ({fee_note})")
    market = ref.market
    market_note = (
        f"{ref.source}, conf. {ref.confidence:.2f}, n={market.n}"
        if market
        else f"{ref.source}, conf. {ref.confidence:.2f}"
    )
    lines.append(f"Venta esperada: {eur(b.sale_price)} ({market_note})")
    if market:
        lines.append(
            f"Mercado: P25 {eur(market.p25)} · P50 {eur(market.p50)} · P75 {eur(market.p75)}"
        )
    lines.append(
        f"Margen neto: {eur(b.net_margin)} · ROI {_pct(b.roi)} · descuento {_pct(opp.discount)}"
    )
    costs = [f"envío {eur(b.inbound_shipping)}", f"embalaje {eur(b.packaging)}"]
    if b.promotion:
        costs.append(f"bump {eur(b.promotion)}")
    if b.resale_fees:
        costs.append(f"comisión {eur(b.resale_fees)}")
    if b.outbound_shipping:
        costs.append(f"envío salida {eur(b.outbound_shipping)}")
    costs.append(f"reserva riesgo {eur(b.risk_reserve)}")
    if b.vat_on_margin:
        costs.append(f"IVA REBU {eur(b.vat_on_margin)}")
    lines.append("Costes: " + " · ".join(costs))
    lines.append(
        f"{opp.entity.sku} · {opp.condition.value} · match {opp.entity.match_confidence:.2f}"
    )
    if opp.risk_factors:
        detail = ", ".join(f"{k} {v:.2f}" for k, v in sorted(opp.risk_factors.items()))
        lines.append(f"Riesgo {opp.risk:.2f}: {detail}")
    if opp.entity.battery_health is not None:
        lines.append(f"Batería declarada: {opp.entity.battery_health} %")
    return lines


def telegram_caption(opp: Opportunity) -> str:
    """Texto HTML para ``sendPhoto``/``sendMessage`` (máx. 1024 caracteres en fotos).

    El recorte se hace sobre texto plano, quitando líneas del final, para no partir nunca
    una etiqueta o entidad HTML (Telegram rechazaría el mensaje entero).
    """
    roi = _pct(opp.roi)
    net = eur(opp.net_margin) if opp.net_margin is not None else "—"
    title = opp.listing.title if len(opp.listing.title) <= 150 else opp.listing.title[:149] + "…"
    head = f"🟢 <b>{html.escape(net)} netos · ROI {roi}</b>\n<b>{html.escape(title)}</b>\n"
    lines = summary_lines(opp)
    while lines:
        caption = head + "\n" + "\n".join(html.escape(line) for line in lines)
        if len(caption) <= TELEGRAM_CAPTION_LIMIT:
            return caption
        lines = lines[:-1]
    return head.rstrip()


def discord_payload(opp: Opportunity) -> dict[str, Any]:
    """Cuerpo JSON de un webhook de Discord con un embed."""
    embed: dict[str, Any] = {
        "title": opp.listing.title[:256],
        "url": opp.listing.url,
        "description": "\n".join(summary_lines(opp))[:4000],
        "color": DISCORD_COLOR_GOOD if opp.decision.value == "alert" else DISCORD_COLOR_WATCH,
    }
    if opp.listing.photo_urls:
        embed["thumbnail"] = {"url": opp.listing.photo_urls[0]}
    return {"content": f"Oportunidad: {opp.listing.url}", "embeds": [embed]}
