"""CLI `vinted-arb`: init-db, poll, runs, market, opps, evaluate, fees, parse-title, alert-test."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import signal
import sqlite3
import sys
from collections.abc import Sequence
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from vinted_arb.alerts.formatting import eur, summary_lines
from vinted_arb.config import ConfigError, Settings, load_searches
from vinted_arb.ingestion.engine import PollOutcome
from vinted_arb.models import Listing
from vinted_arb.normalization import Condition, extract
from vinted_arb.pipeline import OpportunityPipeline
from vinted_arb.storage import PollRunLog, connect
from vinted_arb.storage.repository import utcnow
from vinted_arb.valuation import CHANNELS, compute_margin, max_purchase_price
from vinted_arb.valuation.reference import PriceBookError
from vinted_arb.wiring import build_deal_costs, build_engine, build_notifier, build_pipeline

logger = logging.getLogger("vinted_arb")


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def _decimal(value: str) -> Decimal:
    try:
        return Decimal(value.replace(",", "."))
    except InvalidOperation as exc:
        raise argparse.ArgumentTypeError(f"'{value}' no es un número válido") from exc


# ---------------------------------------------------------------------- comandos
def cmd_init_db(args: argparse.Namespace, settings: Settings) -> int:
    conn = connect(settings.db_path)
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    print(f"Base de datos lista en {settings.db_path} (esquema v{version})")
    return 0


def cmd_poll(args: argparse.Namespace, settings: Settings) -> int:
    searches = load_searches(args.searches or settings.searches_file)
    if args.search:
        unknown = set(args.search) - {s.name for s in searches}
        if unknown:
            raise ConfigError(f"Búsquedas desconocidas: {', '.join(sorted(unknown))}")
        searches = [s for s in searches if s.name in args.search]
    conn = connect(settings.db_path)
    notifier = build_notifier(settings)
    pipeline = build_pipeline(settings, conn, notifier=notifier or None)
    engine = build_engine(
        settings,
        conn,
        searches,
        on_new=pipeline.on_new,
        on_polled=pipeline.on_polled,
        on_health=pipeline.on_health,
    )
    if not notifier:
        logger.info("Sin Telegram/Discord configurado: las oportunidades solo se registran")

    async def run() -> list[PollOutcome]:
        try:
            if args.once:
                return await engine.run_once()
            stop = asyncio.Event()
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGINT, signal.SIGTERM):
                with contextlib.suppress(NotImplementedError, RuntimeError):  # Windows
                    loop.add_signal_handler(sig, stop.set)
            logger.info("Ingesta continua de %d búsquedas (Ctrl+C para parar)", len(searches))
            await engine.run_forever(stop)
            return []
        finally:
            await engine.aclose()
            await notifier.aclose()

    try:
        outcomes = asyncio.run(run())
    except KeyboardInterrupt:
        return 130
    if outcomes:
        print(f"\n{'búsqueda':<28} {'estado':<13} {'pág':>3} {'vistos':>6} {'nuevos':>6}")
        for o in outcomes:
            print(
                f"{o.search:<28} {o.status:<13} {o.pages:>3} {o.result.seen:>6} "
                f"{len(o.result.new):>6}"
            )
        if any(o.status == "baseline" for o in outcomes):
            print("\n(línea base: la primera pasada registra lo existente sin marcarlo como nuevo)")
    # Código ≠ 0 si alguna búsqueda no obtuvo datos (útil para cron/supervisores).
    failed = [o for o in outcomes if o.status in ("blocked", "error", "circuit_open")]
    return 1 if failed else 0


def cmd_runs(args: argparse.Namespace, settings: Settings) -> int:
    runs = PollRunLog(connect(settings.db_path)).recent(args.limit)
    if not runs:
        print("Sin ejecuciones registradas todavía.")
        return 0
    print(f"{'inicio':<26} {'búsqueda':<24} {'estado':<13} {'vistos':>6} {'nuevos':>6}  error")
    for r in runs:
        print(
            f"{r['started_at']:<26} {r['search_name']:<24} {r['status']:<13} "
            f"{r['items_seen']:>6} {r['items_new']:>6}  {r['error'] or ''}"
        )
    return 0


def _open_pipeline(settings: Settings) -> tuple[sqlite3.Connection, OpportunityPipeline]:
    conn = connect(settings.db_path)
    pipeline = build_pipeline(settings, conn)
    pipeline.normalize_pending(limit=1_000_000)
    return conn, pipeline


def cmd_market(args: argparse.Namespace, settings: Settings) -> int:
    _, pipeline = _open_pipeline(settings)
    condition = Condition(args.condition)
    since = utcnow() - timedelta(days=settings.fmv_window_days)
    skus = (
        [args.sku]
        if args.sku
        else [s for s, _ in pipeline.entities.sku_counts(since=since, limit=args.limit)]
    )
    if not skus:
        print(
            "Aún no hay comparables. Deja correr `vinted-arb poll` unos días o rellena "
            f"{settings.price_book_file} para arrancar."
        )
        return 0
    print(
        f"Estado: {condition.value} · percentil venta rápida P{settings.fmv_percentile * 100:.0f}"
        f" · κ={settings.fmv_ask_to_sale} · ventana {settings.fmv_window_days} días\n"
    )
    print(
        f"{'sku':<38} {'n':>4} {'P25':>9} {'P50':>9} {'P75':>9} "
        f"{'venta esp.':>11} {'conf':>5}  origen"
    )
    for sku in skus:
        ref = pipeline.reference_for(sku, condition)
        m = ref.market if ref else None
        print(
            f"{sku:<38} {m.n if m else 0:>4} "
            f"{(m.p25 if m else '—')!s:>9} {(m.p50 if m else '—')!s:>9} "
            f"{(m.p75 if m else '—')!s:>9} "
            f"{(ref.sale_price if ref else '—')!s:>11} {(ref.confidence if ref else 0):>5.2f}  "
            f"{ref.source if ref else 'sin datos'}"
        )
    return 0


def cmd_opps(args: argparse.Namespace, settings: Settings) -> int:
    _, pipeline = _open_pipeline(settings)
    rows = pipeline.opportunities.recent(decision=args.decision, limit=args.limit)
    counts = pipeline.opportunities.counts()
    print(
        "Evaluaciones: " + (", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "ninguna")
    )
    for r in rows:
        net = f"{r['net_margin_cents'] / 100:.2f}" if r["net_margin_cents"] is not None else "—"
        roi = f"{r['roi']:.0%}" if r["roi"] is not None else "—"
        sale = (
            f"{r['expected_sale_cents'] / 100:.2f}" if r["expected_sale_cents"] is not None else "—"
        )
        alerted = "📨" if r["alerted_at"] else "  "
        print(
            f"{alerted} {r['evaluated_at'][:16]} {r['decision']:<6} "
            f"{r['price_cents'] / 100:>8.2f} → "
            f"{sale:>8} neto {net:>7} ROI {roi:>5}  {r['title'][:40]:<40} {r['url']}"
        )
        if args.verbose:
            print(f"      {r['sku']} · {'; '.join(json.loads(r['reasons']))}")
    return 0


def cmd_evaluate(args: argparse.Namespace, settings: Settings) -> int:
    _, pipeline = _open_pipeline(settings)
    listing = Listing(
        id=0,
        title=args.title,
        price=args.price,
        currency="EUR",
        url="(evaluación manual)",
        status_label=args.status,
        service_fee=args.service_fee,
    )
    entity = extract(args.title, args.description or "", status_label=args.status)
    opp = pipeline.evaluate_listing(listing, entity)
    print(f"Decisión: {opp.decision.value.upper()}  (score {opp.score})")
    print(
        f"SKU: {entity.sku or '¿?'} · estado {opp.condition.value} "
        f"· match {entity.match_confidence:.2f}"
    )
    for reason in opp.reasons:
        print(f"  · {reason}")
    if opp.breakdown is not None:
        print()
        for line in summary_lines(opp):
            print(line)
    elif opp.reference is not None:
        print(f"Referencia: {eur(opp.reference.sale_price)} ({opp.reference.source})")
    return 0


def cmd_alert_test(args: argparse.Namespace, settings: Settings) -> int:
    notifier = build_notifier(settings)
    if not notifier:
        print(
            "No hay canales configurados: VA_TELEGRAM_BOT_TOKEN + VA_TELEGRAM_CHAT_ID "
            "o VA_DISCORD_WEBHOOK_URL.",
            file=sys.stderr,
        )
        return 2

    async def send() -> bool:
        try:
            return await notifier.send_text("✅ vinted-arb: prueba de alertas correcta")
        finally:
            await notifier.aclose()

    ok = asyncio.run(send())
    print(
        "Mensaje de prueba enviado."
        if ok
        else "No se pudo enviar (revisa el token, el chat o el webhook)."
    )
    return 0 if ok else 1


def cmd_fees(args: argparse.Namespace, settings: Settings) -> int:
    channel = CHANNELS[args.channel or settings.resale_channel]
    costs = build_deal_costs(settings)
    overrides = {
        "inbound_shipping": args.inbound,
        "outbound_shipping": args.outbound,
        "packaging": args.packaging,
        "promotion": args.promotion,
        "risk_reserve_rate": args.risk,
    }
    costs = replace(costs, **{k: v for k, v in overrides.items() if v is not None})
    if args.rebu:
        costs = replace(costs, rebu_vat_rate=Decimal("0.21"))
    min_net = args.min_net if args.min_net is not None else settings.min_net_profit_eur
    min_roi = args.min_roi if args.min_roi is not None else settings.min_roi

    if args.buy is not None:
        breakdown = compute_margin(
            purchase_price=args.buy, sale_price=args.sell, channel=channel, costs=costs
        )
        for key, value in breakdown.as_dict().items():
            print(f"{key:<18} {value}")
        meets = breakdown.net_margin >= min_net and breakdown.roi >= min_roi
        print(f"\n¿Cumple umbrales (≥{min_net} € y ROI ≥{min_roi})? {'SÍ' if meets else 'NO'}")

    limit = max_purchase_price(
        sale_price=args.sell,
        channel=channel,
        min_net_margin=min_net,
        min_roi=min_roi,
        costs=costs,
    )
    if limit is None:
        print(f"\nCon venta a {args.sell} € en {channel.name} no se alcanzan los umbrales.")
    else:
        discount = (1 - limit / args.sell) * 100
        print(
            f"\nPrecio máximo de compra en Vinted: {limit} € "
            f"({discount:.0f} % bajo el precio de venta) para ≥{min_net} € y ROI ≥{min_roi}"
        )
    return 0


def cmd_parse_title(args: argparse.Namespace, settings: Settings) -> int:
    entity = extract(args.title, args.description or "", status_label=args.status)
    product = entity.product
    print(
        json.dumps(
            {
                "normalized_title": entity.normalized_title,
                "sku": entity.sku,
                "confidence": entity.match_confidence,
                "missing_variants": list(product.missing) if product else [],
                "alternatives": [p.sku for p in entity.alternatives],
                "storage_gb": entity.storage_gb,
                "battery_health": entity.battery_health,
                "condition": entity.condition.value,
                "accessories": sorted(a.value for a in entity.accessories),
                "red_flags": sorted(f.value for f in entity.red_flags),
                "is_candidate": entity.is_candidate,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


# ---------------------------------------------------------------------- parser
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vinted-arb", description="Radar de arbitraje de electrónica en Vinted"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init-db", help="Crea o migra la base de datos SQLite")
    p.set_defaults(func=cmd_init_db)

    p = sub.add_parser("poll", help="Consulta las búsquedas guardadas (Módulo 1)")
    p.add_argument("--once", action="store_true", help="Una sola pasada y salir")
    p.add_argument("--search", action="append", help="Limitar a esta búsqueda (repetible)")
    p.add_argument("--searches", help="Fichero YAML de búsquedas (por defecto VA_SEARCHES_FILE)")
    p.set_defaults(func=cmd_poll)

    p = sub.add_parser("runs", help="Últimas ejecuciones de ingesta")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_runs)

    p = sub.add_parser("market", help="Precio de mercado en Vinted por SKU (Módulo 3)")
    p.add_argument("--sku", help="SKU concreto, p. ej. console:ps5-slim:digital")
    p.add_argument(
        "--condition",
        choices=[c.value for c in Condition if c not in (Condition.FOR_PARTS, Condition.UNKNOWN)],
        default=Condition.VERY_GOOD.value,
    )
    p.add_argument("--limit", type=int, default=30)
    p.set_defaults(func=cmd_market)

    p = sub.add_parser("opps", help="Últimas oportunidades evaluadas")
    p.add_argument("--decision", choices=["alert", "watch", "reject"])
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("-v", "--verbose", action="store_true", help="Mostrar SKU y motivos")
    p.set_defaults(func=cmd_opps)

    p = sub.add_parser("evaluate", help="Valora un anuncio a mano contra el mercado actual")
    p.add_argument("title")
    p.add_argument("--price", type=_decimal, required=True, help="Precio en Vinted (€)")
    p.add_argument("--status", help="Etiqueta de estado de Vinted, p. ej. 'Muy bueno'")
    p.add_argument("--description")
    p.add_argument("--service-fee", type=_decimal, help="Tarifa Vinted exacta si la conoces")
    p.set_defaults(func=cmd_evaluate)

    p = sub.add_parser("alert-test", help="Envía un mensaje de prueba a Telegram/Discord")
    p.set_defaults(func=cmd_alert_test)

    p = sub.add_parser("fees", help="Desglose de margen y precio máximo de compra")
    p.add_argument("--sell", type=_decimal, required=True, help="Precio de venta esperado (€)")
    p.add_argument("--buy", type=_decimal, help="Precio de compra en Vinted (€)")
    p.add_argument("--channel", choices=sorted(CHANNELS), help="Por defecto VA_RESALE_CHANNEL")
    p.add_argument("--min-net", type=_decimal, help="Margen neto mínimo (€)")
    p.add_argument("--min-roi", type=_decimal, help="ROI mínimo (0.20 = 20 %%)")
    p.add_argument("--inbound", type=_decimal, help="Envío de ida (€)")
    p.add_argument("--outbound", type=_decimal, help="Envío de vuelta (€)")
    p.add_argument("--packaging", type=_decimal)
    p.add_argument("--promotion", type=_decimal, help="Bump/promoción (€)")
    p.add_argument("--risk", type=_decimal, help="Reserva de riesgo (fracción)")
    p.add_argument("--rebu", action="store_true", help="Aplicar IVA REBU (modo profesional)")
    p.set_defaults(func=cmd_fees)

    p = sub.add_parser("parse-title", help="Depura el normalizador sobre un título")
    p.add_argument("title")
    p.add_argument("--description")
    p.add_argument("--status", help="Etiqueta de estado de Vinted, p. ej. 'Muy bueno'")
    p.set_defaults(func=cmd_parse_title)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        settings = Settings()
    except Exception as exc:  # pydantic ValidationError con el detalle de la variable
        print(f"Configuración inválida (.env / VA_*):\n{exc}", file=sys.stderr)
        return 2
    _configure_logging(settings.log_level)
    try:
        result: int = args.func(args, settings)
    except (ConfigError, PriceBookError) as exc:
        print(f"Error de configuración: {exc}", file=sys.stderr)
        return 2
    return result


if __name__ == "__main__":
    raise SystemExit(main())
