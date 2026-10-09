"""Clasificación de respuestas HTTP: éxito, límite de ritmo, desafío antibot, error…

Señales conocidas:

* **DataDome**: 403 con JSON ``{"url": "https://geo.captcha-delivery.com/captcha/…"}`` en
  peticiones XHR, o HTML con ``captcha-delivery.com`` / ``ct.captcha-delivery.com``;
  cabecera ``x-datadome``; cookie ``datadome`` en el ``set-cookie`` del bloqueo.
* **Cloudflare**: cabecera ``cf-mitigated: challenge``; HTML "Just a moment…",
  ``/cdn-cgi/challenge-platform``, ``cf-chl``.
* Captchas genéricos: ``g-recaptcha``, ``h-captcha``, ``turnstile``.

Un 200 con HTML cuando se esperaba JSON también se trata como desafío si contiene esas
marcas (algunos antibots responden 200 con la página de verificación).
"""

from __future__ import annotations

import email.utils
import time
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

# Marcas "fuertes": solo aparecen en páginas de desafío/bloqueo.
_STRONG_MARKERS: tuple[tuple[str, str], ...] = (
    ("captcha-delivery.com", "DataDome captcha"),
    ("just a moment...", "Cloudflare interstitial"),
    ("attention required! | cloudflare", "Cloudflare block"),
    ("cf_chl_opt", "Cloudflare challenge"),
)
# Marcas "débiles": también aparecen en páginas legítimas protegidas (p. ej. el tag JS de
# DataDome o el script jsd de Cloudflare se inyectan en la home). Solo cuentan cuando la
# respuesta ya es sospechosa (403, o HTML donde se esperaba JSON).
_WEAK_MARKERS: tuple[tuple[str, str], ...] = (
    ("datadome", "DataDome"),
    ("/cdn-cgi/challenge-platform", "Cloudflare challenge"),
    ("cf-chl", "Cloudflare challenge"),
    ("g-recaptcha", "reCAPTCHA"),
    ("h-captcha", "hCaptcha"),
    ("turnstile", "Cloudflare Turnstile"),
)

# Solo se inspeccionan los primeros KB del cuerpo: las páginas de desafío son pequeñas y
# así evitamos falsos positivos con contenido legítimo largo.
_BODY_SCAN_BYTES = 8192


class VerdictKind(StrEnum):
    OK = "ok"
    NOT_FOUND = "not_found"
    AUTH_REQUIRED = "auth_required"
    RATE_LIMITED = "rate_limited"
    CHALLENGE = "challenge"
    FORBIDDEN = "forbidden"
    SERVER_ERROR = "server_error"
    UNEXPECTED = "unexpected"


@dataclass(frozen=True, slots=True)
class Verdict:
    kind: VerdictKind
    status: int
    reason: str
    retry_after_s: float | None = None

    @property
    def is_block(self) -> bool:
        """Bloqueo antibot: no se reintenta, se abre el breaker y se descarta la sesión."""
        return self.kind in (VerdictKind.CHALLENGE, VerdictKind.FORBIDDEN)

    @property
    def retryable(self) -> bool:
        return self.kind in (
            VerdictKind.RATE_LIMITED,
            VerdictKind.SERVER_ERROR,
            VerdictKind.AUTH_REQUIRED,
        )


def parse_retry_after(value: str | None, *, now: float | None = None) -> float | None:
    """Interpreta ``Retry-After`` en segundos o como fecha HTTP. Devuelve segundos >= 0."""
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        parsed = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    reference = time.time() if now is None else now
    return max(0.0, parsed.timestamp() - reference)


def _challenge_marker(body: bytes, *, strong_only: bool) -> str | None:
    head = body[:_BODY_SCAN_BYTES].decode("utf-8", errors="ignore").lower()
    markers = _STRONG_MARKERS if strong_only else _STRONG_MARKERS + _WEAK_MARKERS
    for marker, label in markers:
        if marker in head:
            return label
    return None


def classify_response(
    status: int,
    headers: Mapping[str, str],
    body: bytes,
    *,
    expect_json: bool = True,
) -> Verdict:
    """Clasifica una respuesta. ``headers`` debe tener las claves en minúsculas."""
    content_type = headers.get("content-type", "").lower()
    is_json = "json" in content_type

    if headers.get("cf-mitigated", "").lower() == "challenge":
        return Verdict(VerdictKind.CHALLENGE, status, "Cloudflare cf-mitigated: challenge")

    if status == 429:
        return Verdict(
            VerdictKind.RATE_LIMITED,
            status,
            "429 Too Many Requests",
            retry_after_s=parse_retry_after(headers.get("retry-after")),
        )

    if status == 403:
        marker = _challenge_marker(body, strong_only=False)
        if marker or "x-datadome" in headers:
            return Verdict(
                VerdictKind.CHALLENGE, status, f"403 con desafío ({marker or 'DataDome'})"
            )
        return Verdict(VerdictKind.FORBIDDEN, status, "403 Forbidden")

    if status == 401:
        return Verdict(VerdictKind.AUTH_REQUIRED, status, "401: sesión caducada o ausente")

    if status in (404, 410):
        return Verdict(VerdictKind.NOT_FOUND, status, f"{status}: recurso inexistente")

    if 500 <= status < 600:
        marker = _challenge_marker(body, strong_only=True)
        if marker:
            return Verdict(VerdictKind.CHALLENGE, status, f"{status} con desafío ({marker})")
        return Verdict(
            VerdictKind.SERVER_ERROR,
            status,
            f"{status}: error de servidor",
            retry_after_s=parse_retry_after(headers.get("retry-after")),
        )

    if 200 <= status < 300:
        if is_json:
            return Verdict(VerdictKind.OK, status, "ok")
        marker = _challenge_marker(body, strong_only=not expect_json)
        if marker:
            return Verdict(
                VerdictKind.CHALLENGE, status, f"{status} con página de desafío ({marker})"
            )
        if expect_json:
            return Verdict(
                VerdictKind.UNEXPECTED, status, f"Se esperaba JSON y llegó '{content_type}'"
            )
        return Verdict(VerdictKind.OK, status, "ok")

    return Verdict(VerdictKind.UNEXPECTED, status, f"Estado HTTP inesperado {status}")
