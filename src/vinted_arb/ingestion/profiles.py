"""Perfiles de navegador coherentes.

Un antibot moderno cruza varias capas: huella TLS (JA3/JA4), huella HTTP/2, User-Agent y
Client Hints (``sec-ch-ua*``). Si no cuadran entre sí, eso es una señal de bot más fuerte
que cualquier User-Agent "raro". Por eso:

* Con ``curl_cffi`` la identidad (UA + Client Hints) la pone la propia suplantación
  (``impersonate``), que garantiza coherencia exacta con la huella TLS/HTTP2. Aquí solo
  añadimos las cabeceras de *contexto* (Accept, Referer, Sec-Fetch-*…).
* Con ``httpx`` (sin huella de navegador) enviamos la identidad completa del perfil, con
  Client Hints calculados con el mismo algoritmo GREASE que usa Chromium.
* El perfil se fija por sesión y solo rota cuando la sesión se descarta por bloqueo:
  rotar el UA en cada petición con las mismas cookies es una incoherencia evidente.
"""

from __future__ import annotations

import random
from collections.abc import Sequence
from dataclasses import dataclass, field

# Algoritmo GREASE de Chromium (components/embedder_support/user_agent_utils.cc):
# la marca "falsa" y su orden dependen del número de versión mayor.
_GREASE_CHARS = (" ", "(", ":", "-", ".", "/", ")", ";", "=", "?", "_")
_GREASE_VERSIONS = ("8", "99", "24")
_GREASE_ORDERS = ((0, 1, 2), (0, 2, 1), (1, 0, 2), (1, 2, 0), (2, 0, 1), (2, 1, 0))


def chrome_sec_ch_ua(major: int, brand: str = "Google Chrome") -> str:
    """Valor de ``sec-ch-ua`` que enviaría Chrome ``major``."""
    seed = major
    grease_brand = (
        f"Not{_GREASE_CHARS[seed % len(_GREASE_CHARS)]}A"
        f"{_GREASE_CHARS[(seed + 1) % len(_GREASE_CHARS)]}Brand"
    )
    grease_version = _GREASE_VERSIONS[seed % len(_GREASE_VERSIONS)]
    order = _GREASE_ORDERS[seed % len(_GREASE_ORDERS)]
    entries = [""] * 3
    entries[order[0]] = f'"{grease_brand}";v="{grease_version}"'
    entries[order[1]] = f'"Chromium";v="{major}"'
    entries[order[2]] = f'"{brand}";v="{major}"'
    return ", ".join(entries)


@dataclass(frozen=True, slots=True)
class BrowserProfile:
    name: str
    impersonate: str
    """Objetivo de suplantación de ``curl_cffi`` (huella TLS/HTTP2)."""
    user_agent: str
    client_hints: dict[str, str] = field(default_factory=dict)
    """Cabeceras ``sec-ch-ua*``. Vacío en navegadores que no las envían (Firefox, Safari)."""
    weight: float = 1.0
    """Peso relativo al elegir perfil (aprox. cuota de mercado)."""

    def identity_headers(self) -> dict[str, str]:
        return {"User-Agent": self.user_agent, **self.client_hints}

    def navigation_headers(
        self, *, accept_language: str, with_identity: bool
    ) -> dict[str, str | None]:
        """Cabeceras de una navegación de documento (p. ej. cargar la home)."""
        headers: dict[str, str | None] = {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
            "image/webp,*/*;q=0.8",
            "Accept-Language": accept_language,
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
            "Upgrade-Insecure-Requests": "1",
        }
        if with_identity:
            headers.update(self.identity_headers())
        return headers

    def api_headers(
        self,
        *,
        accept_language: str,
        referer: str,
        with_identity: bool,
    ) -> dict[str, str | None]:
        """Cabeceras de una petición XHR/fetch del front a su propia API.

        Los valores ``None`` eliminan cabeceras de navegación que ``curl_cffi`` añadiría por
        defecto al suplantar (un fetch real nunca envía ``Sec-Fetch-User``).
        """
        headers: dict[str, str | None] = {
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": accept_language,
            "Referer": referer,
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
            "Sec-Fetch-User": None,
            "Upgrade-Insecure-Requests": None,
        }
        if with_identity:
            headers.update(self.identity_headers())
        return headers


def _chrome(major: int, *, os_name: str, ua_platform: str, weight: float) -> BrowserProfile:
    return BrowserProfile(
        name=f"chrome{major}-{os_name.lower()}",
        impersonate=f"chrome{major}",
        user_agent=(
            f"Mozilla/5.0 ({ua_platform}) AppleWebKit/537.36 (KHTML, like Gecko) "
            f"Chrome/{major}.0.0.0 Safari/537.36"
        ),
        client_hints={
            "sec-ch-ua": chrome_sec_ch_ua(major),
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": f'"{os_name}"',
        },
        weight=weight,
    )


DEFAULT_PROFILES: tuple[BrowserProfile, ...] = (
    _chrome(146, os_name="Windows", ua_platform="Windows NT 10.0; Win64; x64", weight=0.40),
    _chrome(145, os_name="macOS", ua_platform="Macintosh; Intel Mac OS X 10_15_7", weight=0.20),
    BrowserProfile(
        name="safari26-macos",
        impersonate="safari260",
        user_agent=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
            "(KHTML, like Gecko) Version/26.0 Safari/605.1.15"
        ),
        weight=0.25,
    ),
    BrowserProfile(
        name="firefox147-windows",
        impersonate="firefox147",
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:147.0) Gecko/20100101 Firefox/147.0"
        ),
        weight=0.15,
    ),
)


class ProfilePool:
    """Selección ponderada de perfiles evitando repetir el que acaba de ser bloqueado."""

    def __init__(
        self,
        profiles: Sequence[BrowserProfile] = DEFAULT_PROFILES,
        *,
        rng: random.Random | None = None,
    ) -> None:
        if not profiles:
            raise ValueError("Se necesita al menos un perfil")
        self._profiles = tuple(profiles)
        self._rng = rng or random.Random()

    @property
    def profiles(self) -> tuple[BrowserProfile, ...]:
        return self._profiles

    def pick(self, *, exclude: BrowserProfile | None = None) -> BrowserProfile:
        candidates = [p for p in self._profiles if p != exclude] or list(self._profiles)
        weights = [p.weight for p in candidates]
        return self._rng.choices(candidates, weights=weights, k=1)[0]
