"""Motor de ingesta: planifica las búsquedas, pagina, deduplica y persiste.

* **Planificación**: cada búsqueda tiene su intervalo con ±jitter; arranque escalonado;
  ejecución en serie (una sola "persona" navegando), todas bajo el mismo rate limiter.
* **Paginación incremental**: solo se pide la página siguiente si el anuncio más antiguo
  de la actual también es nuevo (el orden es ``newest_first``; si el último ya lo
  conocíamos, todo lo que viene detrás también).
* **Línea base**: la primera consulta de una búsqueda registra todo como visto sin
  emitirlo como "nuevo", para no inundar de alertas al arrancar.
* **Breaker persistente**: su estado se guarda en ``kv`` tras cada consulta y se restaura
  al arrancar; reiniciar el proceso no se salta un enfriamiento.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Literal

from vinted_arb.ingestion.circuit_breaker import BreakerState
from vinted_arb.ingestion.errors import (
    BlockedError,
    CircuitOpenError,
    IngestionError,
    SchemaDriftError,
)
from vinted_arb.ingestion.vinted_client import VintedClient
from vinted_arb.models import SearchQuery
from vinted_arb.storage.repository import ListingRepository, PollRunLog, StateStore, UpsertResult

logger = logging.getLogger(__name__)

BREAKER_STATE_KEY = "breaker:vinted"
BASELINE_KEY_PREFIX = "baseline_done:"

PollStatus = Literal["ok", "baseline", "blocked", "circuit_open", "error"]
NewListingsHandler = Callable[[SearchQuery, UpsertResult], Awaitable[None]]
HealthHandler = Callable[[str], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class PollOutcome:
    search: str
    status: PollStatus
    result: UpsertResult = field(default_factory=UpsertResult)
    pages: int = 0
    error: str | None = None


class IngestionEngine:
    def __init__(
        self,
        *,
        client: VintedClient,
        listings: ListingRepository,
        state: StateStore,
        runs: PollRunLog,
        searches: Sequence[SearchQuery],
        domain: str,
        on_new: NewListingsHandler | None = None,
        on_health: HealthHandler | None = None,
        interval_jitter: float = 0.2,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        rng: random.Random | None = None,
    ) -> None:
        if not 0 <= interval_jitter < 1:
            raise ValueError("interval_jitter debe estar en [0, 1)")
        names = [s.name for s in searches]
        if len(names) != len(set(names)):
            raise ValueError("Los nombres de búsqueda deben ser únicos")
        self._client = client
        self._listings = listings
        self._state = state
        self._runs = runs
        self._searches = [s for s in searches if s.enabled]
        self._domain = domain
        self._on_new = on_new
        self._on_health = on_health
        self._interval_jitter = interval_jitter
        self._clock = clock
        self._sleep = sleep
        self._rng = rng or random.Random()
        self._restore_breaker()

    @property
    def searches(self) -> list[SearchQuery]:
        return list(self._searches)

    async def aclose(self) -> None:
        """Guarda el estado del breaker y cierra la sesión HTTP."""
        self._persist_breaker()
        await self._client.aclose()

    # ------------------------------------------------------------------ una búsqueda
    async def poll_search(self, query: SearchQuery) -> PollOutcome:
        breaker = self._client.breaker
        if breaker.state is BreakerState.OPEN and breaker.retry_in_s() > 0:
            return PollOutcome(query.name, "circuit_open", error=breaker.last_reason)

        baseline_key = BASELINE_KEY_PREFIX + query.name
        is_baseline = not self._state.get(baseline_key, False)
        run_id = self._runs.start(query.name)
        total = UpsertResult()
        pages = 0
        status: PollStatus = "ok"
        error: str | None = None

        try:
            for page_number in range(1, query.max_pages + 1):
                page = await self._client.search(query, page=page_number)
                pages += 1
                result = self._listings.upsert_many(
                    page.listings, domain=self._domain, search_name=query.name
                )
                total = total.merge(result)
                new_ids = {listing.id for listing in result.new}
                oldest_is_new = bool(page.listings) and page.listings[-1].id in new_ids
                if page.is_last or not oldest_is_new:
                    break
        except BlockedError as exc:
            status, error = "blocked", str(exc)
            await self._notify_health(
                f"Bloqueo antibot en '{query.name}': {exc.verdict.reason}. "
                f"Pausa de {self._client.breaker.retry_in_s():.0f}s."
            )
        except CircuitOpenError as exc:
            status, error = "circuit_open", str(exc)
        except SchemaDriftError as exc:
            status, error = "error", str(exc)
            await self._notify_health(f"Posible cambio en la API de Vinted ('{query.name}'): {exc}")
        except IngestionError as exc:
            status, error = "error", str(exc)
            logger.warning("Consulta '%s' fallida: %s", query.name, exc)
        except Exception as exc:  # un fallo inesperado no debe tumbar el bucle continuo
            status, error = "error", f"{type(exc).__name__}: {exc}"
            logger.exception("Error inesperado en la consulta '%s'", query.name)

        if pages > 0 and is_baseline:
            self._state.set(baseline_key, True)
            if status == "ok":
                status = "baseline"

        self._runs.finish(run_id, status=status, pages=pages, result=total, error=error)
        self._persist_breaker()
        logger.info(
            "Búsqueda '%s': %s · páginas=%d vistos=%d nuevos=%d cambios_precio=%d",
            query.name,
            status,
            pages,
            total.seen,
            len(total.new),
            len(total.price_changes),
        )

        if not is_baseline and (total.new or total.price_changes) and self._on_new is not None:
            try:
                await self._on_new(query, total)
            except Exception:  # un consumidor defectuoso no debe parar la ingesta
                logger.exception("El manejador de anuncios nuevos falló para '%s'", query.name)

        return PollOutcome(query.name, status, total, pages, error)

    # ------------------------------------------------------------------ ciclos
    async def run_once(self, only: Sequence[str] | None = None) -> list[PollOutcome]:
        """Una pasada por todas las búsquedas activas (o las indicadas en ``only``)."""
        selected = [s for s in self._searches if only is None or s.name in only]
        outcomes: list[PollOutcome] = []
        for query in selected:
            outcomes.append(await self.poll_search(query))
        return outcomes

    async def run_forever(self, stop: asyncio.Event) -> None:
        """Bucle continuo hasta que se active ``stop``."""
        if not self._searches:
            logger.warning("No hay búsquedas activas")
            return
        now = self._clock()
        stagger = min(s.interval_s for s in self._searches) / len(self._searches)
        next_due = {s.name: now + i * stagger for i, s in enumerate(self._searches)}
        by_name = {s.name: s for s in self._searches}
        notified_pause = False

        while not stop.is_set():
            retry_in = self._client.breaker.retry_in_s()
            if retry_in > 0:
                if not notified_pause:
                    logger.warning("Breaker abierto: ingesta en pausa %.0fs", retry_in)
                    notified_pause = True
                await self._wait(retry_in, stop)
                continue
            notified_pause = False

            name, due = min(next_due.items(), key=lambda item: item[1])
            delay = due - self._clock()
            if delay > 0:
                await self._wait(delay, stop)
                continue

            query = by_name[name]
            await self.poll_search(query)
            next_due[name] = self._clock() + self._jittered(query.interval_s)

        self._persist_breaker()

    # ------------------------------------------------------------------ internos
    def _jittered(self, interval_s: float) -> float:
        j = self._interval_jitter
        return interval_s * self._rng.uniform(1 - j, 1 + j)

    async def _wait(self, delay: float, stop: asyncio.Event) -> None:
        if stop.is_set() or delay <= 0:
            return
        sleeper = asyncio.ensure_future(self._sleep(delay))
        stopper = asyncio.ensure_future(stop.wait())
        _, pending = await asyncio.wait({sleeper, stopper}, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)

    def _restore_breaker(self) -> None:
        snapshot = self._state.get(BREAKER_STATE_KEY)
        if isinstance(snapshot, dict):
            self._client.breaker.restore(snapshot)
            if self._client.breaker.state is BreakerState.OPEN:
                logger.warning(
                    "Breaker restaurado en OPEN (%s); quedan %.0fs de enfriamiento",
                    self._client.breaker.last_reason,
                    self._client.breaker.retry_in_s(),
                )

    def _persist_breaker(self) -> None:
        self._state.set(BREAKER_STATE_KEY, self._client.breaker.snapshot())

    async def _notify_health(self, message: str) -> None:
        logger.error(message)
        if self._on_health is None:
            return
        try:
            await self._on_health(message)
        except Exception:
            logger.exception("El manejador de salud falló")
