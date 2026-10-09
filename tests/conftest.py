"""Dobles de prueba compartidos: reloj falso, transporte programable y helpers."""

from __future__ import annotations

import json
import random
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from vinted_arb.ingestion.backoff import BackoffPolicy
from vinted_arb.ingestion.circuit_breaker import CircuitBreaker
from vinted_arb.ingestion.profiles import BrowserProfile, ProfilePool
from vinted_arb.ingestion.rate_limiter import AsyncRateLimiter
from vinted_arb.ingestion.transport import HttpResponse
from vinted_arb.ingestion.vinted_client import VintedClient

FIXTURES = Path(__file__).parent / "fixtures"


class FakeClock:
    """Reloj controlable; ``sleep`` avanza el tiempo en lugar de esperar."""

    def __init__(self, start: float = 1_700_000_000.0) -> None:
        self.now = start
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += max(0.0, seconds)


def json_response(
    payload: Any, status: int = 200, headers: Mapping[str, str] | None = None
) -> HttpResponse:
    return HttpResponse(
        status=status,
        headers={"content-type": "application/json; charset=utf-8", **(headers or {})},
        body=json.dumps(payload).encode(),
        url="https://www.vinted.es/api",
    )


def html_response(
    body: str = "<html><head></head><body>Vinted</body></html>",
    status: int = 200,
    headers: Mapping[str, str] | None = None,
) -> HttpResponse:
    return HttpResponse(
        status=status,
        headers={"content-type": "text/html; charset=utf-8", **(headers or {})},
        body=body.encode(),
        url="https://www.vinted.es/",
    )


@dataclass
class Call:
    url: str
    params: dict[str, str] | None
    headers: dict[str, str | None]
    transport_id: int


@dataclass
class Router:
    """Guion de respuestas: la home y la API tienen colas independientes."""

    home: deque[HttpResponse | Exception] = field(default_factory=deque)
    api: deque[HttpResponse | Exception] = field(default_factory=deque)
    home_cookies: set[str] = field(default_factory=lambda: {"access_token_web", "anon_id"})
    calls: list[Call] = field(default_factory=list)
    transports: list[FakeTransport] = field(default_factory=list)

    def factory(self, profile: BrowserProfile) -> FakeTransport:
        transport = FakeTransport(self, profile, len(self.transports))
        self.transports.append(transport)
        return transport

    def next_response(self, url: str) -> HttpResponse | Exception:
        if url.endswith("/"):
            return self.home.popleft() if self.home else html_response()
        if not self.api:
            raise AssertionError(f"Petición API no programada: {url}")
        return self.api.popleft()

    @property
    def home_calls(self) -> list[Call]:
        return [c for c in self.calls if c.url.endswith("/")]

    @property
    def api_calls(self) -> list[Call]:
        return [c for c in self.calls if not c.url.endswith("/")]


class FakeTransport:
    sends_identity_headers = True

    def __init__(self, router: Router, profile: BrowserProfile, transport_id: int) -> None:
        self.router = router
        self.profile = profile
        self.id = transport_id
        self.cookies: set[str] = set()
        self.closed = False

    async def get(
        self,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str | None] | None = None,
    ) -> HttpResponse:
        assert not self.closed, "Uso de un transporte ya cerrado"
        self.router.calls.append(
            Call(url, dict(params) if params else None, dict(headers or {}), self.id)
        )
        response = self.router.next_response(url)
        if isinstance(response, Exception):
            raise response
        if url.endswith("/") and response.status == 200:
            self.cookies |= self.router.home_cookies
        return response

    def cookie_names(self) -> set[str]:
        return set(self.cookies)

    async def aclose(self) -> None:
        self.closed = True


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def router() -> Router:
    return Router()


@pytest.fixture
def catalog_payload() -> dict[str, Any]:
    data: dict[str, Any] = json.loads((FIXTURES / "catalog_items.json").read_text())
    return data


def make_client(
    router: Router,
    clock: FakeClock,
    *,
    max_attempts: int = 4,
    failure_threshold: int = 3,
    session_max_age_s: float = 2400.0,
) -> VintedClient:
    return VintedClient(
        domain="www.vinted.es",
        transport_factory=router.factory,
        profiles=ProfilePool(rng=random.Random(7)),
        rate_limiter=AsyncRateLimiter(
            600, burst=100, jitter_ratio=0, clock=clock, sleep=clock.sleep
        ),
        breaker=CircuitBreaker(
            "test", failure_threshold=failure_threshold, base_cooldown_s=900, clock=clock
        ),
        backoff=BackoffPolicy(
            base_delay_s=1, max_delay_s=8, max_attempts=max_attempts, jitter="none"
        ),
        session_max_age_s=session_max_age_s,
        clock=clock,
        sleep=clock.sleep,
        rng=random.Random(0),
    )
