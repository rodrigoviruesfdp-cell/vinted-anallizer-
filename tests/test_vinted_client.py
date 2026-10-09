"""Cliente de Vinted: sesión, reintentos, breaker y rotación de perfil (Módulo 1)."""

from __future__ import annotations

import json
from typing import Any

import pytest

from conftest import FakeClock, Router, html_response, json_response, make_client
from vinted_arb.ingestion.circuit_breaker import BreakerState
from vinted_arb.ingestion.errors import (
    BlockedError,
    CircuitOpenError,
    HttpStatusError,
    NotFoundError,
    RetryExhaustedError,
    SchemaDriftError,
    TransportError,
)
from vinted_arb.ingestion.transport import HttpResponse
from vinted_arb.models import SearchQuery

QUERY = SearchQuery(name="ps5", search_text="ps5 slim", price_from=150, price_to=550)
DATADOME_403 = json_response(
    {"url": "https://geo.captcha-delivery.com/captcha/?initialCid=abc"}, status=403
)


async def test_bootstraps_session_then_fetches_catalog(
    router: Router, clock: FakeClock, catalog_payload: dict[str, Any]
) -> None:
    router.api.append(json_response(catalog_payload))
    client = make_client(router, clock)

    page = await client.search(QUERY)

    assert [listing.id for listing in page.listings] == [5001, 5002, 5003]
    home, api = router.calls
    assert home.url == "https://www.vinted.es/"
    assert home.headers["Sec-Fetch-Mode"] == "navigate"
    assert api.url == "https://www.vinted.es/api/v2/catalog/items"
    assert api.params == {
        "search_text": "ps5 slim",
        "order": "newest_first",
        "page": "1",
        "per_page": "96",
        "price_from": "150",
        "price_to": "550",
        "currency": "EUR",
    }
    assert api.headers["Referer"] == "https://www.vinted.es/catalog?search_text=ps5+slim"
    assert api.headers["Sec-Fetch-Mode"] == "cors"
    assert "User-Agent" not in api.headers  # el transporte suplanta la identidad
    assert client.breaker.state is BreakerState.CLOSED


async def test_session_is_reused_between_requests(
    router: Router, clock: FakeClock, catalog_payload: dict[str, Any]
) -> None:
    router.api.extend([json_response(catalog_payload), json_response(catalog_payload)])
    client = make_client(router, clock)
    await client.search(QUERY)
    await client.search(QUERY, page=2)
    assert len(router.home_calls) == 1
    assert router.api_calls[1].params["page"] == "2"  # type: ignore[index]


async def test_429_is_retried_honouring_retry_after(
    router: Router, clock: FakeClock, catalog_payload: dict[str, Any]
) -> None:
    router.api.extend(
        [
            json_response({}, status=429, headers={"retry-after": "30"}),
            json_response(catalog_payload),
        ]
    )
    client = make_client(router, clock)
    page = await client.search(QUERY)
    assert len(page.listings) == 3
    assert 30 in clock.sleeps
    assert client.breaker.state is BreakerState.CLOSED


async def test_datadome_block_trips_breaker_without_retrying(
    router: Router, clock: FakeClock
) -> None:
    router.api.append(DATADOME_403)
    client = make_client(router, clock)

    with pytest.raises(BlockedError) as excinfo:
        await client.search(QUERY)

    assert "DataDome" in str(excinfo.value)
    assert len(router.api_calls) == 1  # ni un solo reintento tras el desafío
    assert client.breaker.state is BreakerState.OPEN
    assert router.transports[0].closed  # sesión marcada descartada

    calls_before = len(router.calls)
    with pytest.raises(CircuitOpenError):
        await client.search(QUERY)
    assert len(router.calls) == calls_before  # con el circuito abierto no sale nada a la red


async def test_after_cooldown_new_session_uses_a_different_profile(
    router: Router, clock: FakeClock, catalog_payload: dict[str, Any]
) -> None:
    router.api.extend([DATADOME_403, json_response(catalog_payload)])
    client = make_client(router, clock)
    with pytest.raises(BlockedError):
        await client.search(QUERY)
    blocked_profile = router.transports[0].profile

    clock.advance(900)
    page = await client.search(QUERY)

    assert len(page.listings) == 3
    assert router.transports[1].profile != blocked_profile
    assert client.breaker.state is BreakerState.CLOSED


async def test_401_renews_session_once_keeping_profile(
    router: Router, clock: FakeClock, catalog_payload: dict[str, Any]
) -> None:
    router.api.extend([json_response({}, status=401), json_response(catalog_payload)])
    client = make_client(router, clock)
    page = await client.search(QUERY)
    assert len(page.listings) == 3
    assert len(router.home_calls) == 2
    assert router.transports[0].profile == router.transports[1].profile


async def test_repeated_401_gives_up(router: Router, clock: FakeClock) -> None:
    router.api.extend([json_response({}, status=401)] * 3)
    client = make_client(router, clock, max_attempts=3, failure_threshold=10)
    with pytest.raises(RetryExhaustedError) as excinfo:
        await client.search(QUERY)
    assert isinstance(excinfo.value.__cause__, HttpStatusError)


async def test_persistent_5xx_exhausts_retries_with_backoff(
    router: Router, clock: FakeClock
) -> None:
    router.api.extend([json_response({}, status=500)] * 3)
    client = make_client(router, clock, max_attempts=3, failure_threshold=10)
    with pytest.raises(RetryExhaustedError):
        await client.search(QUERY)
    assert clock.sleeps == [1, 2]  # backoff exponencial sin jitter en el test
    assert len(router.api_calls) == 3


async def test_soft_failures_open_the_breaker(router: Router, clock: FakeClock) -> None:
    router.api.extend([json_response({}, status=503)] * 2)
    client = make_client(router, clock, max_attempts=4, failure_threshold=2)
    with pytest.raises(CircuitOpenError):
        await client.search(QUERY)
    assert len(router.api_calls) == 2
    assert client.breaker.state is BreakerState.OPEN


async def test_transport_errors_are_retried(
    router: Router, clock: FakeClock, catalog_payload: dict[str, Any]
) -> None:
    router.api.extend([TransportError("timeout"), json_response(catalog_payload)])
    client = make_client(router, clock)
    page = await client.search(QUERY)
    assert len(page.listings) == 3


async def test_html_challenge_with_200_is_a_block(router: Router, clock: FakeClock) -> None:
    router.api.append(html_response("<html><script src='https://ct.captcha-delivery.com/c.js'>"))
    client = make_client(router, clock)
    with pytest.raises(BlockedError):
        await client.search(QUERY)


async def test_blocked_home_aborts_bootstrap(router: Router, clock: FakeClock) -> None:
    router.home.append(html_response("<title>Just a moment...</title>", status=403))
    client = make_client(router, clock)
    with pytest.raises(BlockedError):
        await client.search(QUERY)
    assert router.api_calls == []
    assert client.breaker.state is BreakerState.OPEN


async def test_home_without_session_cookie_still_proceeds(
    router: Router, clock: FakeClock, catalog_payload: dict[str, Any]
) -> None:
    router.home_cookies = {"anon_id"}
    router.api.append(json_response(catalog_payload))
    client = make_client(router, clock)
    page = await client.search(QUERY)
    assert len(page.listings) == 3


async def test_session_renewed_by_age_keeps_profile(
    router: Router, clock: FakeClock, catalog_payload: dict[str, Any]
) -> None:
    router.api.extend([json_response(catalog_payload)] * 2)
    client = make_client(router, clock, session_max_age_s=60)
    await client.search(QUERY)
    clock.advance(61)
    await client.search(QUERY)
    assert len(router.home_calls) == 2
    assert router.transports[0].closed
    assert router.transports[0].profile == router.transports[1].profile


async def test_invalid_json_is_schema_drift(router: Router, clock: FakeClock) -> None:
    router.api.append(
        HttpResponse(200, {"content-type": "application/json"}, b"{roto", "https://x")
    )
    client = make_client(router, clock)
    with pytest.raises(SchemaDriftError):
        await client.search(QUERY)


async def test_missing_items_key_is_schema_drift(router: Router, clock: FakeClock) -> None:
    router.api.append(json_response({"catalog_items": []}))
    client = make_client(router, clock)
    with pytest.raises(SchemaDriftError):
        await client.search(QUERY)


async def test_404_raises_not_found_without_penalty(router: Router, clock: FakeClock) -> None:
    router.api.append(json_response({}, status=404))
    client = make_client(router, clock, failure_threshold=1)
    with pytest.raises(NotFoundError):
        await client.search(QUERY)
    assert client.breaker.state is BreakerState.CLOSED


async def test_aclose_closes_transport(
    router: Router, clock: FakeClock, catalog_payload: dict[str, Any]
) -> None:
    router.api.append(json_response(catalog_payload))
    client = make_client(router, clock)
    await client.search(QUERY)
    await client.aclose()
    assert router.transports[0].closed


def test_fixture_is_valid_json() -> None:
    # Protege el fixture sintético contra ediciones que rompan el JSON.
    from conftest import FIXTURES

    json.loads((FIXTURES / "catalog_items.json").read_text())
