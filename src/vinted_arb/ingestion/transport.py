"""Transportes HTTP tras una interfaz común (``HttpTransport``).

* :class:`CurlCffiTransport`: recomendado. Suplanta la huella TLS/HTTP2 de un navegador real.
* :class:`HttpxTransport`: alternativa sin dependencias nativas. Su huella TLS es la de
  Python, así que es más fácil de detectar; útil para depurar o para APIs oficiales.

Cada transporte mantiene su propio *cookie jar*: una instancia = una sesión de navegador.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from vinted_arb.ingestion.errors import TransportError
from vinted_arb.ingestion.profiles import BrowserProfile

logger = logging.getLogger(__name__)

Headers = Mapping[str, str | None]


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status: int
    headers: Mapping[str, str]
    """Cabeceras con nombres en minúsculas."""
    body: bytes
    url: str

    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")

    def json(self) -> Any:
        return json.loads(self.body)


class HttpTransport(Protocol):
    @property
    def sends_identity_headers(self) -> bool:
        """True si el transporte ya pone UA/Client Hints coherentes por sí mismo."""
        ...

    async def get(
        self,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
        headers: Headers | None = None,
    ) -> HttpResponse: ...

    def cookie_names(self) -> set[str]: ...

    async def aclose(self) -> None: ...


class CurlCffiTransport:
    """Transporte basado en ``curl_cffi`` con suplantación de navegador."""

    def __init__(self, *, impersonate: str, timeout_s: float, proxy: str | None = None) -> None:
        try:
            from curl_cffi.requests import AsyncSession, BrowserType
        except ImportError as exc:  # pragma: no cover - depende del entorno
            raise RuntimeError("curl_cffi no está instalado: pip install curl_cffi") from exc

        supported = {b.value for b in BrowserType}
        if impersonate not in supported:
            logger.warning(
                "curl_cffi no soporta '%s' en esta versión; se usa 'chrome' (última disponible)",
                impersonate,
            )
            impersonate = "chrome"
        self._session: Any = AsyncSession(impersonate=impersonate, timeout=timeout_s, proxy=proxy)

    @property
    def sends_identity_headers(self) -> bool:
        return True

    async def get(
        self,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
        headers: Headers | None = None,
    ) -> HttpResponse:
        from curl_cffi.requests.exceptions import RequestException

        try:
            response = await self._session.get(
                url,
                params=dict(params) if params else None,
                headers=dict(headers) if headers else None,
                allow_redirects=True,
            )
        except RequestException as exc:
            raise TransportError(f"{type(exc).__name__}: {exc}") from exc
        return HttpResponse(
            status=int(response.status_code),
            headers={str(k).lower(): str(v) for k, v in response.headers.items()},
            body=bytes(response.content),
            url=str(response.url),
        )

    def cookie_names(self) -> set[str]:
        return {cookie.name for cookie in self._session.cookies.jar}

    async def aclose(self) -> None:
        await self._session.close()


class HttpxTransport:
    """Transporte basado en ``httpx`` (sin huella de navegador)."""

    def __init__(self, *, timeout_s: float, proxy: str | None = None) -> None:
        import httpx

        self._httpx = httpx
        self._client = httpx.AsyncClient(timeout=timeout_s, proxy=proxy, follow_redirects=True)

    @property
    def sends_identity_headers(self) -> bool:
        return False

    async def get(
        self,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
        headers: Headers | None = None,
    ) -> HttpResponse:
        clean_headers = {k: v for k, v in (headers or {}).items() if v is not None}
        try:
            response = await self._client.get(url, params=params, headers=clean_headers)
        except self._httpx.HTTPError as exc:
            raise TransportError(f"{type(exc).__name__}: {exc}") from exc
        return HttpResponse(
            status=response.status_code,
            headers={k.lower(): v for k, v in response.headers.items()},
            body=response.content,
            url=str(response.url),
        )

    def cookie_names(self) -> set[str]:
        return {cookie.name for cookie in self._client.cookies.jar}

    async def aclose(self) -> None:
        await self._client.aclose()


def build_transport(
    kind: str,
    profile: BrowserProfile,
    *,
    timeout_s: float,
    proxy: str | None = None,
) -> HttpTransport:
    """Crea el transporte configurado para un perfil. Cae a httpx si falta curl_cffi."""
    if kind == "curl_cffi":
        try:
            return CurlCffiTransport(
                impersonate=profile.impersonate, timeout_s=timeout_s, proxy=proxy
            )
        except RuntimeError:
            logger.warning("curl_cffi no disponible; usando httpx (huella TLS no de navegador)")
            return HttpxTransport(timeout_s=timeout_s, proxy=proxy)
    if kind == "httpx":
        return HttpxTransport(timeout_s=timeout_s, proxy=proxy)
    raise ValueError(f"Transporte desconocido: {kind!r}")
