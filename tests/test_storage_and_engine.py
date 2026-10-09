"""Persistencia SQLite y motor de ingesta (Módulo 1)."""

from __future__ import annotations

import asyncio
import random
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from conftest import FakeClock
from vinted_arb.ingestion.block_detector import Verdict, VerdictKind
from vinted_arb.ingestion.circuit_breaker import BreakerState, CircuitBreaker
from vinted_arb.ingestion.engine import BREAKER_STATE_KEY, IngestionEngine
from vinted_arb.ingestion.errors import BlockedError, SchemaDriftError
from vinted_arb.models import CatalogPage, Listing, SearchQuery
from vinted_arb.storage import (
    ListingRepository,
    PollRunLog,
    StateStore,
    UpsertResult,
    connect,
)
from vinted_arb.storage.db import SCHEMA_VERSION
from vinted_arb.storage.repository import from_cents, to_cents

T0 = datetime(2026, 10, 9, 10, 0, tzinfo=UTC)


def listing(item_id: int, price: str = "100", title: str | None = None) -> Listing:
    return Listing(
        id=item_id,
        title=title or f"PS5 slim {item_id}",
        price=Decimal(price),
        currency="EUR",
        url=f"https://www.vinted.es/items/{item_id}",
        photo_urls=(f"https://img/{item_id}.jpg",),
        seller_id=item_id * 10,
    )


def page(ids: list[int], *, current: int = 1, total: int = 1) -> CatalogPage:
    return CatalogPage(tuple(listing(i) for i in ids), current_page=current, total_pages=total)


@pytest.fixture
def conn():  # type: ignore[no-untyped-def]
    connection = connect(":memory:")
    yield connection
    connection.close()


# ---------------------------------------------------------------------- storage
class TestStorage:
    def test_migrations_are_idempotent(self, tmp_path: Path) -> None:
        db = tmp_path / "sub" / "test.sqlite3"
        connect(db).close()
        again = connect(db)
        assert again.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert again.execute("PRAGMA journal_mode").fetchone()[0] == "wal"

    def test_cents_roundtrip(self) -> None:
        assert to_cents(Decimal("19.995")) == 2000
        assert from_cents(1999) == Decimal("19.99")

    def test_upsert_detects_new_and_price_changes(self, conn) -> None:  # type: ignore[no-untyped-def]
        repo = ListingRepository(conn)
        first = repo.upsert_many(
            [listing(1, "300"), listing(2, "200")], domain="d", search_name="s", now=T0
        )
        assert [x.id for x in first.new] == [1, 2] and first.seen == 2

        later = T0 + timedelta(minutes=5)
        second = repo.upsert_many(
            [listing(1, "270"), listing(2, "200"), listing(3, "150")],
            domain="d",
            search_name="s",
            now=later,
        )
        assert [x.id for x in second.new] == [3]
        assert len(second.price_changes) == 1
        change = second.price_changes[0]
        assert change.previous_price == Decimal("300.00")
        assert change.delta == Decimal("-30")
        assert change.is_drop

        assert repo.price_history(1) == [
            (T0.isoformat(timespec="seconds"), Decimal("300.00")),
            (later.isoformat(timespec="seconds"), Decimal("270.00")),
        ]
        row = repo.get(1)
        assert row is not None
        assert row["first_seen_at"] == T0.isoformat(timespec="seconds")
        assert row["last_seen_at"] == later.isoformat(timespec="seconds")
        assert row["seller_id"] == 10
        assert repo.count() == 3
        assert repo.known_ids([1, 3, 99]) == {1, 3}

    def test_naive_datetimes_rejected(self, conn) -> None:  # type: ignore[no-untyped-def]
        with pytest.raises(ValueError):
            ListingRepository(conn).upsert_many(
                [listing(1)], domain="d", search_name="s", now=datetime(2026, 1, 1)
            )

    def test_state_store_roundtrip(self, conn) -> None:  # type: ignore[no-untyped-def]
        state = StateStore(conn)
        assert state.get("x", "def") == "def"
        state.set("x", {"a": 1})
        state.set("x", {"a": 2})
        assert state.get("x") == {"a": 2}

    def test_poll_run_log(self, conn) -> None:  # type: ignore[no-untyped-def]
        runs = PollRunLog(conn)
        run_id = runs.start("ps5", now=T0)
        runs.finish(run_id, status="ok", pages=2, result=UpsertResult(new=(listing(1),), seen=96))
        (row,) = runs.recent()
        assert (row["status"], row["pages"], row["items_seen"], row["items_new"]) == (
            "ok",
            2,
            96,
            1,
        )


# ---------------------------------------------------------------------- engine
class StubClient:
    """Cliente de Vinted falso: devuelve páginas o lanza errores según un guion."""

    def __init__(self, breaker: CircuitBreaker, script: list[CatalogPage | Exception]) -> None:
        self.breaker = breaker
        self.script = script
        self.calls: list[tuple[str, int]] = []
        self.closed = False
        self.on_call: Callable[[], None] | None = None

    async def search(self, query: SearchQuery, *, page: int = 1) -> CatalogPage:
        self.calls.append((query.name, page))
        if self.on_call:
            self.on_call()
        result = self.script.pop(0) if self.script else CatalogPage((), 1, 1)
        if isinstance(result, Exception):
            if isinstance(result, BlockedError):
                self.breaker.trip(result.verdict.reason)
            raise result
        return result

    async def aclose(self) -> None:
        self.closed = True


class EngineHarness:
    def __init__(self, conn, clock: FakeClock, searches: list[SearchQuery], script=None) -> None:  # type: ignore[no-untyped-def]
        self.breaker = CircuitBreaker("t", base_cooldown_s=900, clock=clock)
        self.client = StubClient(self.breaker, list(script or []))
        self.state = StateStore(conn)
        self.new_events: list[tuple[str, UpsertResult]] = []
        self.health: list[str] = []

        async def on_new(query: SearchQuery, result: UpsertResult) -> None:
            self.new_events.append((query.name, result))

        async def on_health(message: str) -> None:
            self.health.append(message)

        self.engine = IngestionEngine(
            client=self.client,  # type: ignore[arg-type]
            listings=ListingRepository(conn),
            state=self.state,
            runs=PollRunLog(conn),
            searches=searches,
            domain="www.vinted.es",
            on_new=on_new,
            on_health=on_health,
            clock=clock,
            sleep=clock.sleep,
            rng=random.Random(0),
        )


PS5 = SearchQuery(name="ps5", search_text="ps5", max_pages=3)


async def test_first_poll_is_a_silent_baseline(conn, clock: FakeClock) -> None:  # type: ignore[no-untyped-def]
    h = EngineHarness(conn, clock, [PS5], [page([3, 2, 1]), page([4, 3, 2, 1])])

    first = await h.engine.poll_search(PS5)
    assert first.status == "baseline"
    assert h.new_events == []

    second = await h.engine.poll_search(PS5)
    assert second.status == "ok"
    assert [x.id for x in second.result.new] == [4]
    assert [(name, [x.id for x in r.new]) for name, r in h.new_events] == [("ps5", [4])]


async def test_price_drop_is_emitted(conn, clock: FakeClock) -> None:  # type: ignore[no-untyped-def]
    cheaper = CatalogPage((listing(1, "80"),), 1, 1)
    h = EngineHarness(conn, clock, [PS5], [page([1]), cheaper])
    await h.engine.poll_search(PS5)
    outcome = await h.engine.poll_search(PS5)
    assert outcome.result.new == ()
    assert outcome.result.price_changes[0].is_drop
    assert len(h.new_events) == 1


async def test_pagination_stops_when_oldest_item_is_known(conn, clock: FakeClock) -> None:  # type: ignore[no-untyped-def]
    ListingRepository(conn).upsert_many([listing(5)], domain="d", search_name="ps5")
    h = EngineHarness(
        conn,
        clock,
        [PS5],
        [page([10, 9, 8], current=1, total=9), page([7, 6, 5], current=2, total=9)],
    )
    outcome = await h.engine.poll_search(PS5)
    assert h.client.calls == [("ps5", 1), ("ps5", 2)]
    assert outcome.pages == 2
    assert len(outcome.result.new) == 5


async def test_pagination_respects_max_pages_and_last_page(conn, clock: FakeClock) -> None:  # type: ignore[no-untyped-def]
    query = SearchQuery(name="q", search_text="q", max_pages=2)
    h = EngineHarness(
        conn, clock, [query], [page([3], current=1, total=9), page([2], current=2, total=9)]
    )
    await h.engine.poll_search(query)
    assert h.client.calls == [("q", 1), ("q", 2)]

    last = SearchQuery(name="last", search_text="x", max_pages=5)
    h2 = EngineHarness(conn, clock, [last], [page([30], current=1, total=1)])
    await h2.engine.poll_search(last)
    assert h2.client.calls == [("last", 1)]


async def test_block_is_reported_and_breaker_persisted(conn, clock: FakeClock) -> None:  # type: ignore[no-untyped-def]
    blocked = BlockedError(Verdict(VerdictKind.CHALLENGE, 403, "403 con desafío (DataDome)"))
    h = EngineHarness(conn, clock, [PS5], [blocked])
    outcome = await h.engine.poll_search(PS5)
    assert outcome.status == "blocked"
    assert any("DataDome" in message for message in h.health)
    assert h.state.get(BREAKER_STATE_KEY)["state"] == "open"

    # Con el circuito abierto ni siquiera se llama al cliente ni se registra ejecución.
    calls = len(h.client.calls)
    skipped = await h.engine.poll_search(PS5)
    assert skipped.status == "circuit_open"
    assert len(h.client.calls) == calls


async def test_breaker_state_survives_restart(conn, clock: FakeClock) -> None:  # type: ignore[no-untyped-def]
    blocked = BlockedError(Verdict(VerdictKind.CHALLENGE, 403, "captcha"))
    first = EngineHarness(conn, clock, [PS5], [blocked])
    await first.engine.poll_search(PS5)

    clock.advance(60)
    restarted = EngineHarness(conn, clock, [PS5])
    assert restarted.breaker.state is BreakerState.OPEN
    assert restarted.breaker.retry_in_s() == pytest.approx(840)


async def test_schema_drift_notifies_health(conn, clock: FakeClock) -> None:  # type: ignore[no-untyped-def]
    h = EngineHarness(conn, clock, [PS5], [SchemaDriftError("sin 'items'")])
    outcome = await h.engine.poll_search(PS5)
    assert outcome.status == "error"
    assert any("API de Vinted" in m for m in h.health)


async def test_unexpected_exception_is_contained(conn, clock: FakeClock) -> None:  # type: ignore[no-untyped-def]
    h = EngineHarness(conn, clock, [PS5], [ValueError("bug inesperado"), page([1])])
    outcome = await h.engine.poll_search(PS5)
    assert outcome.status == "error"
    assert "ValueError" in (outcome.error or "")
    assert (await h.engine.poll_search(PS5)).status == "baseline"


async def test_failing_consumer_does_not_stop_ingestion(conn, clock: FakeClock) -> None:  # type: ignore[no-untyped-def]
    h = EngineHarness(conn, clock, [PS5], [page([1]), page([2, 1])])

    async def broken(query: SearchQuery, result: UpsertResult) -> None:
        raise RuntimeError("consumidor roto")

    h.engine._on_new = broken
    await h.engine.poll_search(PS5)
    outcome = await h.engine.poll_search(PS5)
    assert outcome.status == "ok"


async def test_run_once_covers_enabled_searches_only(conn, clock: FakeClock) -> None:  # type: ignore[no-untyped-def]
    disabled = SearchQuery(name="off", search_text="x", enabled=False)
    other = SearchQuery(name="switch", search_text="switch")
    h = EngineHarness(conn, clock, [PS5, disabled, other])
    outcomes = await h.engine.run_once()
    assert [o.search for o in outcomes] == ["ps5", "switch"]
    assert await h.engine.run_once(only=["switch"]) != []


async def test_run_forever_schedules_by_interval_until_stopped(conn, clock: FakeClock) -> None:  # type: ignore[no-untyped-def]
    fast = SearchQuery(name="fast", search_text="a", interval_s=60)
    slow = SearchQuery(name="slow", search_text="b", interval_s=300)
    h = EngineHarness(conn, clock, [fast, slow])
    stop = asyncio.Event()
    start = clock()
    h.client.on_call = lambda: stop.set() if clock() - start > 600 else None

    await asyncio.wait_for(h.engine.run_forever(stop), timeout=5)

    names = [name for name, _ in h.client.calls]
    assert names.count("fast") > names.count("slow") >= 2
    assert names.count("fast") >= 8  # ~600 s / 60 s con ±20 % de jitter


async def test_run_forever_waits_out_open_breaker(conn, clock: FakeClock) -> None:  # type: ignore[no-untyped-def]
    h = EngineHarness(conn, clock, [PS5])
    h.breaker.trip("captcha")
    stop = asyncio.Event()
    h.client.on_call = stop.set
    start = clock()
    await asyncio.wait_for(h.engine.run_forever(stop), timeout=5)
    assert clock() - start >= 900  # no consultó nada hasta cumplir el enfriamiento
    assert len(h.client.calls) == 1


def test_duplicate_search_names_rejected(conn, clock: FakeClock) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ValueError):
        EngineHarness(conn, clock, [PS5, PS5])


async def test_on_polled_runs_on_baseline_and_before_on_new(conn, clock: FakeClock) -> None:  # type: ignore[no-untyped-def]
    h = EngineHarness(conn, clock, [PS5], [page([1]), page([2, 1])])
    order: list[str] = []

    async def on_polled(query: SearchQuery, outcome) -> None:  # type: ignore[no-untyped-def]
        order.append(f"polled:{outcome.status}")

    async def on_new(query: SearchQuery, result: UpsertResult) -> None:
        order.append("new")

    h.engine._on_polled = on_polled
    h.engine._on_new = on_new
    await h.engine.poll_search(PS5)
    await h.engine.poll_search(PS5)
    assert order == ["polled:baseline", "polled:ok", "new"]
