"""Pipeline de oportunidades (M2→M3→M4) y notificadores Telegram/Discord."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest

from vinted_arb import pipeline as pipeline_module
from vinted_arb.alerts.formatting import (
    TELEGRAM_CAPTION_LIMIT,
    discord_payload,
    eur,
    telegram_caption,
)
from vinted_arb.alerts.notifiers import DiscordNotifier, MultiNotifier, TelegramNotifier
from vinted_arb.models import Listing, SearchQuery
from vinted_arb.normalization import Condition, extract
from vinted_arb.pipeline import MarketParams, OpportunityPipeline
from vinted_arb.storage import ListingRepository, connect
from vinted_arb.storage.repository import PriceChange, UpsertResult
from vinted_arb.valuation.opportunity import Decision, EvaluationParams, evaluate
from vinted_arb.valuation.reference import PriceBook, Reference

D = Decimal
NOW = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)
QUERY = SearchQuery(name="ps5", search_text="ps5")
TITLE = "PS5 Slim Digital 1TB"


def make_listing(
    item_id: int, price: str, title: str = TITLE, status: str = "Muy bueno"
) -> Listing:
    return Listing(
        id=item_id,
        title=title,
        price=D(price),
        currency="EUR",
        url=f"https://www.vinted.es/items/{item_id}",
        status_label=status,
        photo_urls=(f"https://img.example/{item_id}.jpg",),
    )


class RecordingNotifier:
    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.opportunities: list[Any] = []
        self.texts: list[str] = []

    async def send_opportunity(self, opp: Any) -> bool:
        self.opportunities.append(opp)
        return self.ok

    async def send_text(self, text: str) -> bool:
        self.texts.append(text)
        return self.ok

    async def aclose(self) -> None:
        pass


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def conn():  # type: ignore[no-untyped-def]
    connection = connect(":memory:")
    yield connection
    connection.close()


def seed_market(conn, prices: list[int], start_id: int = 1000) -> None:  # type: ignore[no-untyped-def]
    listings = [make_listing(start_id + i, str(p)) for i, p in enumerate(prices)]
    ListingRepository(conn).upsert_many(listings, domain="d", search_name="ps5", now=NOW)


def make_pipeline(conn, notifier=None, **kwargs: Any) -> OpportunityPipeline:  # type: ignore[no-untyped-def]
    return OpportunityPipeline(
        conn,
        evaluation=EvaluationParams(),
        market=MarketParams(min_samples=12),
        notifier=notifier,
        now=lambda: NOW,
        **kwargs,
    )


MARKET_PRICES = [300, 305, 310, 315, 320, 325, 330, 335, 340, 345, 350, 355]


class TestPipeline:
    def test_normalize_pending_feeds_the_market(self, conn) -> None:  # type: ignore[no-untyped-def]
        seed_market(conn, MARKET_PRICES)
        ListingRepository(conn).upsert_many(
            [make_listing(1, "35", title="Mando PS5 DualSense")],
            domain="d",
            search_name="ps5",
            now=NOW,
        )
        pipeline = make_pipeline(conn)
        assert pipeline.normalize_pending() == 13
        assert pipeline.normalize_pending() == 0  # idempotente
        assert pipeline.entities.sku_counts(since=NOW.replace(day=1)) == [
            ("console:ps5-slim:digital", 12)  # el mando no entra en el mercado
        ]
        ref = pipeline.reference_for("console:ps5-slim:digital", Condition.VERY_GOOD)
        assert ref is not None and ref.source == "vinted_market"
        assert ref.market is not None and ref.market.n == 12

    def test_normalizer_version_bump_renormalizes(self, conn, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        seed_market(conn, MARKET_PRICES[:3])
        pipeline = make_pipeline(conn)
        assert pipeline.normalize_pending() == 3
        monkeypatch.setattr(pipeline_module, "NORMALIZER_VERSION", 99)
        assert pipeline.normalize_pending() == 3

    async def test_new_cheap_listing_triggers_one_alert(self, conn) -> None:  # type: ignore[no-untyped-def]
        seed_market(conn, MARKET_PRICES)
        notifier = RecordingNotifier()
        pipeline = make_pipeline(conn, notifier)
        pipeline.normalize_pending()

        cheap = make_listing(1, "200")
        ListingRepository(conn).upsert_many([cheap], domain="d", search_name="ps5", now=NOW)
        (opp,) = await pipeline.on_new(QUERY, UpsertResult(new=(cheap,), seen=1))

        assert opp.decision is Decision.ALERT
        assert opp.reference is not None and opp.reference.source == "vinted_market"
        # P30 de los comparables (≈315,5) × κ 0,92
        assert opp.reference.sale_price == D("290.26")
        assert len(notifier.opportunities) == 1
        assert pipeline.opportunities.alerted_price(1) == D("200.00")
        (row,) = pipeline.opportunities.recent(decision="alert")
        assert row["title"] == TITLE and row["sku"] == "console:ps5-slim:digital"

        # Misma oportunidad otra vez (p. ej. reevaluación): no se repite la alerta.
        await pipeline.on_new(QUERY, UpsertResult(new=(cheap,), seen=1))
        assert len(notifier.opportunities) == 1

        # Bajada de precio significativa: nueva alerta.
        cheaper = make_listing(1, "180")
        ListingRepository(conn).upsert_many([cheaper], domain="d", search_name="ps5", now=NOW)
        drop = PriceChange(cheaper, D("200"))
        await pipeline.on_new(QUERY, UpsertResult(price_changes=(drop,), seen=1))
        assert len(notifier.opportunities) == 2
        assert pipeline.opportunities.alerted_price(1) == D("180.00")

    async def test_candidate_is_excluded_from_its_own_market(self, conn) -> None:  # type: ignore[no-untyped-def]
        seed_market(conn, MARKET_PRICES[:12])
        pipeline = make_pipeline(conn)
        pipeline.normalize_pending()
        listing = make_listing(1000, "300")  # es uno de los comparables
        opp = pipeline.evaluate_listing(listing)
        assert opp.reference is not None and opp.reference.market is not None
        assert opp.reference.market.n == 11

    async def test_watch_and_reject_are_stored_but_not_alerted(self, conn) -> None:  # type: ignore[no-untyped-def]
        seed_market(conn, MARKET_PRICES)
        notifier = RecordingNotifier()
        pipeline = make_pipeline(conn, notifier)
        items = [make_listing(1, "260"), make_listing(2, "35", title="Mando PS5 DualSense")]
        ListingRepository(conn).upsert_many(items, domain="d", search_name="ps5", now=NOW)
        opps = await pipeline.on_new(QUERY, UpsertResult(new=tuple(items), seen=2))
        assert [o.decision for o in opps] == [Decision.WATCH, Decision.REJECT]
        assert notifier.opportunities == []
        assert pipeline.opportunities.counts() == {"watch": 1, "reject": 1}

    async def test_alert_rate_limit(self, conn, caplog) -> None:  # type: ignore[no-untyped-def]
        seed_market(conn, MARKET_PRICES)
        notifier = RecordingNotifier()
        clock = Clock()
        pipeline = make_pipeline(conn, notifier, max_alerts_per_hour=2, monotonic=clock)
        items = [make_listing(i, "200") for i in (1, 2, 3)]
        ListingRepository(conn).upsert_many(items, domain="d", search_name="ps5", now=NOW)
        with caplog.at_level(logging.WARNING):
            await pipeline.on_new(QUERY, UpsertResult(new=tuple(items), seen=3))
        assert len(notifier.opportunities) == 2
        assert "Límite de 2 alertas/hora" in caplog.text
        assert pipeline.opportunities.alerted_price(3) is None

        clock.t = 3601  # pasa la hora: vuelve a haber cupo
        await pipeline.on_new(QUERY, UpsertResult(new=(items[2],), seen=1))
        assert len(notifier.opportunities) == 3

    async def test_failed_delivery_is_not_marked_alerted(self, conn) -> None:  # type: ignore[no-untyped-def]
        seed_market(conn, MARKET_PRICES)
        pipeline = make_pipeline(conn, RecordingNotifier(ok=False))
        cheap = make_listing(1, "200")
        ListingRepository(conn).upsert_many([cheap], domain="d", search_name="ps5", now=NOW)
        await pipeline.on_new(QUERY, UpsertResult(new=(cheap,), seen=1))
        assert pipeline.opportunities.alerted_price(1) is None

    async def test_price_book_bootstraps_without_market(self, conn, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
        book_file = tmp_path / "book.csv"
        book_file.write_text(
            "sku,condition,price_eur,updated_at,notes\nconsole:ps5-slim:digital,,320,2026-10-01,\n"
        )
        pipeline = make_pipeline(conn, price_book=PriceBook.load(book_file))
        opp = pipeline.evaluate_listing(make_listing(1, "200"))
        assert opp.reference is not None and opp.reference.source == "price_book"
        assert opp.decision is Decision.ALERT

    async def test_health_messages_are_forwarded(self, conn) -> None:  # type: ignore[no-untyped-def]
        notifier = RecordingNotifier()
        await make_pipeline(conn, notifier).on_health("Bloqueo antibot")
        assert notifier.texts == ["⚠️ Bloqueo antibot"]


# ---------------------------------------------------------------------- formato
def sample_opportunity(title: str = TITLE, price: str = "200") -> Any:
    listing = make_listing(42, price, title=title)
    entity = extract(title, status_label="Muy bueno")
    reference = Reference(D("300"), 0.9, "vinted_market")
    return evaluate(listing, entity, reference, EvaluationParams())


class TestFormatting:
    def test_eur_spanish_format(self) -> None:
        assert eur(D("1234.5")) == "1.234,50 €"
        assert eur(D("-3")) == "-3,00 €"

    def test_telegram_caption_contains_breakdown_and_escapes_html(self) -> None:
        opp = sample_opportunity(title="PS5 Slim Digital 1TB <oferta> & caja")
        caption = telegram_caption(opp)
        assert "&lt;oferta&gt; &amp; caja" in caption
        assert "77,30 € netos" in caption
        assert "Compra: 200,00 €" in caption
        assert "console:ps5-slim:digital" in caption
        assert len(caption) <= TELEGRAM_CAPTION_LIMIT

    def test_long_content_is_trimmed_without_breaking_html(self) -> None:
        opp = sample_opportunity(title="PS5 Slim Digital 1TB " + "<x>" * 700)
        caption = telegram_caption(opp)
        assert len(caption) <= TELEGRAM_CAPTION_LIMIT
        assert caption.count("<b>") == caption.count("</b>") == 2
        assert "&lt;x" in caption and "<x>" not in caption

    def test_discord_payload(self) -> None:
        payload = discord_payload(sample_opportunity())
        (embed,) = payload["embeds"]
        assert embed["url"] == "https://www.vinted.es/items/42"
        assert embed["thumbnail"]["url"] == "https://img.example/42.jpg"
        assert "Margen neto" in embed["description"]


# ---------------------------------------------------------------------- notificadores
async def no_sleep(_: float) -> None:
    return None


def telegram_with(handler: Any) -> TelegramNotifier:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return TelegramNotifier("123:SECRET", "999", client=client, sleep=no_sleep)


class TestTelegram:
    async def test_send_photo_with_button(self) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json={"ok": True})

        assert await telegram_with(handler).send_opportunity(sample_opportunity())
        (request,) = seen
        assert request.url.path == "/bot123:SECRET/sendPhoto"
        form = dict(httpx.QueryParams(request.content.decode()))
        assert form["chat_id"] == "999"
        assert form["photo"] == "https://img.example/42.jpg"
        assert form["parse_mode"] == "HTML"
        button = json.loads(form["reply_markup"])["inline_keyboard"][0][0]
        assert button == {"text": "Abrir en Vinted", "url": "https://www.vinted.es/items/42"}

    async def test_falls_back_to_text_when_photo_fails(self) -> None:
        methods: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            methods.append(request.url.path.rsplit("/", 1)[-1])
            if methods[-1] == "sendPhoto":
                return httpx.Response(
                    400, json={"ok": False, "error_code": 400, "description": "wrong file"}
                )
            return httpx.Response(200, json={"ok": True})

        assert await telegram_with(handler).send_opportunity(sample_opportunity())
        assert methods == ["sendPhoto", "sendMessage"]

    async def test_retries_once_on_429(self) -> None:
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            if calls == 1:
                return httpx.Response(
                    429, json={"ok": False, "error_code": 429, "parameters": {"retry_after": 3}}
                )
            return httpx.Response(200, json={"ok": True})

        assert await telegram_with(handler).send_text("hola")
        assert calls == 2

    async def test_token_never_logged(self, caplog: pytest.LogCaptureFixture) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError(f"fallo conectando a {request.url}")

        with caplog.at_level(logging.WARNING):
            assert not await telegram_with(handler).send_text("hola")
        assert "SECRET" not in caplog.text
        assert "***" in caplog.text


class TestDiscord:
    async def test_webhook_and_retry(self) -> None:
        bodies: list[dict[str, Any]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            bodies.append(json.loads(request.content))
            if len(bodies) == 1:
                return httpx.Response(429, json={"retry_after": 0.5})
            return httpx.Response(204)

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        notifier = DiscordNotifier("https://discord.example/webhook", client=client, sleep=no_sleep)
        assert await notifier.send_opportunity(sample_opportunity())
        assert len(bodies) == 2 and bodies[1]["embeds"][0]["title"] == TITLE

    async def test_rejected(self) -> None:
        client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(404)))
        notifier = DiscordNotifier("https://discord.example/webhook", client=client)
        assert not await notifier.send_text("hola")


class TestMultiNotifier:
    async def test_any_success_counts_and_errors_are_contained(self) -> None:
        class Broken(RecordingNotifier):
            async def send_text(self, text: str) -> bool:
                raise RuntimeError("caído")

        ok = RecordingNotifier()
        multi = MultiNotifier([Broken(), ok])
        assert multi
        assert await multi.send_text("hola")
        assert ok.texts == ["hola"]
        assert not MultiNotifier([])
