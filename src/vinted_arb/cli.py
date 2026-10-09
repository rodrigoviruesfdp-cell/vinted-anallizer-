"""CLI: ``vinted-arb {init-db, poll, runs, fees, parse-title}``."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import signal
import sys
from collections.abc import Sequence
from decimal import Decimal, InvalidOperation

from vinted_arb.config import ConfigError, Settings, load_searches
from vinted_arb.ingestion.engine import PollOutcome
from vinted_arb.models import SearchQuery
from vinted_arb.normalization import extract
from vinted_arb.storage import PollRunLog, UpsertResult, connect
from vinted_arb.valuation import (
    CHANNELS,
    DealCosts,
    compute_margin,
    max_purchase_price,
)
from vinted_arb.wiring import build_engine

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


# ---------------------------------------------------------------------- handlers
async def _print_new_listings(query: SearchQuery, result: UpsertResult) -> None:
    """Consumidor provisional hasta el Módulo 4: normaliza y muestra lo nuevo."""
    for listing in result.new:
        entity = extract(listing.title, status_label=listing.status_label)
        flags = ",".join(sorted(f.value for f in entity.red_flags)) or "-"
        logger.info(
            "NUEVO [%s] %s € · %s → %s (conf %.2f, %s, flags=%s) %s",
            query.name,
            listing.price,
            listing.title,
            entity.sku or "¿?",
            entity.match_confidence,
            entity.condition.value,
            flags,
            listing.url,
        )
    for change in result.price_changes:
        if change.is_drop:
            logger.info(
                "BAJADA [%s] %s € → %s € · %s %s",
                query.name,
                change.previous_price,
                change.listing.price,
                change.listing.title,
                change.listing.url,
            )


async def _log_health(message: str) -> None:
    logger.error("SALUD: %s", message)


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
    engine = build_engine(
        settings, conn, searches, on_new=_print_new_listings, on_health=_log_health
    )

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


def cmd_fees(args: argparse.Namespace, settings: Settings) -> int:
    channel = CHANNELS[args.channel]
    costs = DealCosts(
        inbound_shipping=args.inbound,
        outbound_shipping=args.outbound,
        packaging=args.packaging,
        risk_reserve_rate=args.risk,
        rebu_vat_rate=Decimal("0.21") if args.rebu else None,
    )
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

    p = sub.add_parser("fees", help="Desglose de margen y precio máximo de compra")
    p.add_argument("--sell", type=_decimal, required=True, help="Precio de venta esperado (€)")
    p.add_argument("--buy", type=_decimal, help="Precio de compra en Vinted (€)")
    p.add_argument("--channel", choices=sorted(CHANNELS), default="wallapop")
    p.add_argument("--min-net", type=_decimal, help="Margen neto mínimo (€)")
    p.add_argument("--min-roi", type=_decimal, help="ROI mínimo (0.20 = 20 %%)")
    p.add_argument("--inbound", type=_decimal, default=Decimal("4.50"), help="Envío de ida (€)")
    p.add_argument("--outbound", type=_decimal, default=Decimal("6.00"), help="Envío de vuelta (€)")
    p.add_argument("--packaging", type=_decimal, default=Decimal("1.50"))
    p.add_argument("--risk", type=_decimal, default=Decimal("0.03"), help="Reserva de riesgo")
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
    except ConfigError as exc:
        print(f"Error de configuración: {exc}", file=sys.stderr)
        return 2
    return result


if __name__ == "__main__":
    raise SystemExit(main())
