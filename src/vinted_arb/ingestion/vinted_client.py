"""Cliente de la API de catálogo de Vinted con sesión anónima y resiliencia completa.

Flujo de cada petición::

    breaker.allow_request() → rate_limiter.acquire() → transport.get() → classify_response()
      │ (cerrado)                                                           │
      └─ CircuitOpenError, sin tocar la red    ├─ OK              → JSON
                                               ├─ 401             → renovar sesión 1 vez
                                               ├─ 429 / 5xx / red → backoff y reintento
                                               └─ 403 / captcha   → breaker.trip + descartar
                                                                    sesión + BlockedError

La sesión se obtiene visitando la home (cookies ``access_token_web`` /
``_vinted_*_session``). Se renueva por edad conservando el perfil de navegador y solo se
cambia de perfil cuando la sesión se descarta por bloqueo.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote_plus

from vinted_arb.ingestion.backoff import BackoffPolicy
from vinted_arb.ingestion.block_detector import Verdict, VerdictKind, classify_response
from vinted_arb.ingestion.circuit_breaker import BreakerState, CircuitBreaker
from vinted_arb.ingestion.errors import (
    BlockedError,
    CircuitOpenError,
    HttpStatusError,
    IngestionError,
    NotFoundError,
    RetryExhaustedError,
    SchemaDriftError,
    SessionBootstrapError,
    TransportError,
)
from vinted_arb.ingestion.parser import parse_catalog_page
from vinted_arb.ingestion.profiles import BrowserProfile, ProfilePool
from vinted_arb.ingestion.rate_limiter import AsyncRateLimiter
from vinted_arb.ingestion.transport import Headers, HttpResponse, HttpTransport
from vinted_arb.models import CatalogPage, SearchQuery

logger = logging.getLogger(__name__)

TransportFactory = Callable[[BrowserProfile], HttpTransport]

CATALOG_PATH = "/api/v2/catalog/items"


def has_session_cookie(cookie_names: set[str]) -> bool:
    """¿Hay alguna cookie de sesión conocida de Vinted?"""
    return any(
        name == "access_token_web" or (name.startswith("_vinted_") and name.endswith("_session"))
        for name in cookie_names
    )


@dataclass(slots=True)
class _Session:
    transport: HttpTransport
    profile: BrowserProfile
    created_at: float
    bootstrapped: bool = False


class VintedClient:
    def __init__(
        self,
        *,
        domain: str,
        transport_factory: TransportFactory,
        profiles: ProfilePool,
        rate_limiter: AsyncRateLimiter,
        breaker: CircuitBreaker,
        backoff: BackoffPolicy,
        accept_language: str = "es-ES,es;q=0.9",
        session_max_age_s: float = 2400.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        rng: random.Random | None = None,
    ) -> None:
        self._domain = domain
        self._transport_factory = transport_factory
        self._profiles = profiles
        self._rate_limiter = rate_limiter
        self._breaker = breaker
        self._backoff = backoff
        self._accept_language = accept_language
        self._session_max_age_s = session_max_age_s
        self._clock = clock
        self._sleep = sleep
        self._rng = rng or random.Random()

        self._session: _Session | None = None
        self._sticky_profile: BrowserProfile | None = None
        self._blocked_profile: BrowserProfile | None = None

    # ------------------------------------------------------------------ API pública
    @property
    def base_url(self) -> str:
        return f"https://{self._domain}"

    @property
    def breaker(self) -> CircuitBreaker:
        return self._breaker

    @property
    def current_profile(self) -> BrowserProfile | None:
        return self._session.profile if self._session else None

    async def search(self, query: SearchQuery, *, page: int = 1) -> CatalogPage:
        """Una página del catálogo para la búsqueda indicada."""
        referer = f"{self.base_url}/catalog?search_text={quote_plus(query.search_text)}"
        payload = await self.get_json(CATALOG_PATH, params=query.to_params(page), referer=referer)
        return parse_catalog_page(payload, base_url=self.base_url)

    async def get_json(self, path: str, *, params: Mapping[str, str], referer: str) -> Any:
        """GET a la API interna con reintentos, breaker y gestión de sesión."""
        url = f"{self.base_url}{path}"
        auth_refreshed = False
        last_error: IngestionError | None = None

        for attempt in range(1, self._backoff.max_attempts + 1):
            try:
                session = await self._ensure_session()
                headers = session.profile.api_headers(
                    accept_language=self._accept_language,
                    referer=referer,
                    with_identity=not session.transport.sends_identity_headers,
                )
                response = await self._send(session, url, params, headers)
            except (TransportError, SessionBootstrapError) as exc:
                logger.warning(
                    "Intento %d/%d fallido: %s", attempt, self._backoff.max_attempts, exc
                )
                self._breaker.record_failure(str(exc))
                if isinstance(exc, SessionBootstrapError):
                    await self._discard_session(rotate_profile=False)
                last_error = exc
                await self._pause_before_retry(attempt, None)
                continue

            verdict = classify_response(response.status, response.headers, response.body)
            if verdict.kind is VerdictKind.OK:
                return self._decode_json(response)
            if verdict.kind is VerdictKind.NOT_FOUND:
                self._breaker.record_success()
                raise NotFoundError(url)
            if verdict.is_block:
                await self._handle_block(verdict)
                raise BlockedError(verdict)
            if verdict.kind is VerdictKind.AUTH_REQUIRED and not auth_refreshed:
                logger.info("401 en %s: renovando sesión", path)
                auth_refreshed = True
                await self._discard_session(rotate_profile=False)
                continue

            logger.warning("Intento %d/%d: %s", attempt, self._backoff.max_attempts, verdict.reason)
            self._breaker.record_failure(verdict.reason)
            last_error = HttpStatusError(verdict)
            if not verdict.retryable:
                raise last_error
            await self._pause_before_retry(attempt, verdict.retry_after_s)

        raise RetryExhaustedError(
            f"{path}: {self._backoff.max_attempts} intentos agotados"
        ) from last_error

    async def aclose(self) -> None:
        if self._session is not None:
            await self._session.transport.aclose()
            self._session = None

    # ------------------------------------------------------------------ sesión
    async def _ensure_session(self) -> _Session:
        now = self._clock()
        session = self._session
        if session is not None and now - session.created_at > self._session_max_age_s:
            logger.info(
                "Sesión con %.0fs de antigüedad: renovando (mismo perfil)", now - session.created_at
            )
            await self._discard_session(rotate_profile=False)
            session = None
        if session is None:
            profile = self._sticky_profile or self._profiles.pick(exclude=self._blocked_profile)
            session = _Session(
                transport=self._transport_factory(profile), profile=profile, created_at=now
            )
            self._session = session
            logger.info("Nueva sesión con perfil '%s'", profile.name)
        if not session.bootstrapped:
            await self._bootstrap(session)
        return session

    async def _bootstrap(self, session: _Session) -> None:
        headers = session.profile.navigation_headers(
            accept_language=self._accept_language,
            with_identity=not session.transport.sends_identity_headers,
        )
        response = await self._send(session, f"{self.base_url}/", None, headers)
        verdict = classify_response(
            response.status, response.headers, response.body, expect_json=False
        )
        if verdict.is_block:
            await self._handle_block(verdict)
            raise BlockedError(verdict)
        if verdict.kind is not VerdictKind.OK:
            raise SessionBootstrapError(f"La home respondió: {verdict.reason}")
        if not has_session_cookie(session.transport.cookie_names()):
            # No es fatal: Vinted ha renombrado cookies otras veces. Si la API exige sesión,
            # responderá 401 y se gestionará allí.
            logger.warning(
                "La home no estableció cookies de sesión conocidas (recibidas: %s)",
                sorted(session.transport.cookie_names()),
            )
        self._breaker.record_success()
        session.bootstrapped = True

    async def _discard_session(self, *, rotate_profile: bool) -> None:
        session = self._session
        self._session = None
        if session is None:
            return
        if rotate_profile:
            self._blocked_profile = session.profile
            self._sticky_profile = None
        else:
            self._sticky_profile = session.profile
        try:
            await session.transport.aclose()
        except Exception:  # cerrar nunca debe enmascarar el error original
            logger.debug("Error cerrando transporte", exc_info=True)

    # ------------------------------------------------------------------ red
    async def _send(
        self,
        session: _Session,
        url: str,
        params: Mapping[str, str] | None,
        headers: Headers,
    ) -> HttpResponse:
        if not self._breaker.allow_request():
            raise CircuitOpenError(self._breaker.retry_in_s(), self._breaker.last_reason)
        await self._rate_limiter.acquire()
        return await session.transport.get(url, params=params, headers=headers)

    async def _handle_block(self, verdict: Verdict) -> None:
        logger.error(
            "Bloqueo detectado (%s): abriendo breaker y descartando sesión", verdict.reason
        )
        self._breaker.trip(verdict.reason)
        await self._discard_session(rotate_profile=True)

    async def _pause_before_retry(self, attempt: int, retry_after_s: float | None) -> None:
        if attempt >= self._backoff.max_attempts:
            return
        if self._breaker.state is BreakerState.OPEN:
            return  # el siguiente intento lanzará CircuitOpenError sin tocar la red
        delay = self._backoff.delay_with_hint(attempt, retry_after_s, self._rng)
        logger.info("Reintentando en %.1fs", delay)
        await self._sleep(delay)

    def _decode_json(self, response: HttpResponse) -> Any:
        try:
            payload = response.json()
        except ValueError as exc:
            self._breaker.record_failure("JSON inválido")
            raise SchemaDriftError(f"Respuesta 200 con JSON inválido desde {response.url}") from exc
        self._breaker.record_success()
        return payload
