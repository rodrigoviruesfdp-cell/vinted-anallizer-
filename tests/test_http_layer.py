"""Detector de bloqueos, perfiles de navegador y parser del catálogo (Módulo 1)."""

from __future__ import annotations

import json
import random
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from vinted_arb.ingestion.block_detector import VerdictKind, classify_response, parse_retry_after
from vinted_arb.ingestion.errors import SchemaDriftError
from vinted_arb.ingestion.parser import parse_catalog_page
from vinted_arb.ingestion.profiles import (
    DEFAULT_PROFILES,
    BrowserProfile,
    ProfilePool,
    chrome_sec_ch_ua,
)
from vinted_arb.ingestion.transport import CurlCffiTransport, HttpxTransport, build_transport

JSON = {"content-type": "application/json"}
HTML = {"content-type": "text/html; charset=utf-8"}
DATADOME_BLOCK = json.dumps(
    {"url": "https://geo.captcha-delivery.com/captcha/?initialCid=abc&hash=def"}
).encode()
CF_INTERSTITIAL = b"<!DOCTYPE html><html><head><title>Just a moment...</title></head></html>"
# La home legítima incluye el tag JS de DataDome y el script jsd de Cloudflare.
LEGIT_HOME = (
    b"<html><head><script>window.ddjskey='x'</script>"
    b"<script src='https://js.datadome.co/tags.js'></script>"
    b"<script src='/cdn-cgi/challenge-platform/scripts/jsd/main.js'></script>"
    b"</head><body>Vinted</body></html>"
)


class TestBlockDetector:
    @pytest.mark.parametrize(
        ("status", "headers", "body", "expect_json", "kind"),
        [
            (200, JSON, b'{"items": []}', True, VerdictKind.OK),
            (403, JSON, DATADOME_BLOCK, True, VerdictKind.CHALLENGE),
            (403, {**JSON, "x-datadome": "protected"}, b"{}", True, VerdictKind.CHALLENGE),
            (403, HTML, b"<html>Forbidden</html>", True, VerdictKind.FORBIDDEN),
            (403, HTML, CF_INTERSTITIAL, True, VerdictKind.CHALLENGE),
            (503, HTML, CF_INTERSTITIAL, True, VerdictKind.CHALLENGE),
            (503, HTML, b"<html>Service Unavailable</html>", True, VerdictKind.SERVER_ERROR),
            (200, {**HTML, "cf-mitigated": "challenge"}, b"", True, VerdictKind.CHALLENGE),
            (429, JSON, b"{}", True, VerdictKind.RATE_LIMITED),
            (401, JSON, b"{}", True, VerdictKind.AUTH_REQUIRED),
            (404, JSON, b"{}", True, VerdictKind.NOT_FOUND),
            (410, JSON, b"{}", True, VerdictKind.NOT_FOUND),
            (200, HTML, b"<html>login</html>", True, VerdictKind.UNEXPECTED),
            (200, HTML, b"<html>captcha-delivery.com</html>", True, VerdictKind.CHALLENGE),
            (200, HTML, LEGIT_HOME, True, VerdictKind.CHALLENGE),  # HTML donde se esperaba JSON
            (200, HTML, LEGIT_HOME, False, VerdictKind.OK),  # la home legítima NO es un bloqueo
            (200, HTML, CF_INTERSTITIAL, False, VerdictKind.CHALLENGE),
            (302, HTML, b"", True, VerdictKind.UNEXPECTED),
        ],
    )
    def test_classification(
        self,
        status: int,
        headers: dict[str, str],
        body: bytes,
        expect_json: bool,
        kind: VerdictKind,
    ) -> None:
        assert classify_response(status, headers, body, expect_json=expect_json).kind is kind

    def test_block_semantics(self) -> None:
        verdict = classify_response(403, JSON, DATADOME_BLOCK)
        assert verdict.is_block and not verdict.retryable
        rate_limited = classify_response(429, {**JSON, "retry-after": "45"}, b"{}")
        assert rate_limited.retryable and not rate_limited.is_block
        assert rate_limited.retry_after_s == 45

    def test_retry_after_http_date(self) -> None:
        now = datetime(2026, 10, 9, 12, 0, 0, tzinfo=UTC).timestamp()
        assert parse_retry_after("Fri, 09 Oct 2026 12:01:30 GMT", now=now) == 90
        assert parse_retry_after("Fri, 09 Oct 2026 11:00:00 GMT", now=now) == 0
        assert parse_retry_after("mañana", now=now) is None
        assert parse_retry_after(None) is None


class TestProfiles:
    @pytest.mark.parametrize(
        ("major", "expected"),
        [
            (120, '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"'),
            (124, '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"'),
            (131, '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"'),
        ],
    )
    def test_chrome_grease_matches_real_browsers(self, major: int, expected: str) -> None:
        assert chrome_sec_ch_ua(major) == expected

    def test_chrome_profiles_are_internally_consistent(self) -> None:
        for profile in DEFAULT_PROFILES:
            if not profile.impersonate.startswith("chrome"):
                assert profile.client_hints == {}  # Firefox/Safari no envían Client Hints
                continue
            major = profile.impersonate.removeprefix("chrome")
            assert f"Chrome/{major}." in profile.user_agent
            assert f'"Google Chrome";v="{major}"' in profile.client_hints["sec-ch-ua"]

    def test_api_headers_look_like_same_origin_fetch(self) -> None:
        profile = DEFAULT_PROFILES[0]
        headers = profile.api_headers(
            accept_language="es-ES,es;q=0.9",
            referer="https://www.vinted.es/catalog?search_text=ps5",
            with_identity=False,
        )
        assert headers["Accept"].startswith("application/json")  # type: ignore[union-attr]
        assert headers["Sec-Fetch-Mode"] == "cors"
        assert headers["Sec-Fetch-Site"] == "same-origin"
        assert headers["Sec-Fetch-User"] is None  # se elimina la cabecera de navegación
        assert "User-Agent" not in headers  # la pone curl_cffi de forma coherente

    def test_identity_headers_for_httpx(self) -> None:
        profile = DEFAULT_PROFILES[0]
        headers = profile.navigation_headers(accept_language="es-ES", with_identity=True)
        assert headers["User-Agent"] == profile.user_agent
        assert headers["sec-ch-ua-platform"] == '"Windows"'
        assert headers["Sec-Fetch-Mode"] == "navigate"

    def test_pool_avoids_blocked_profile(self) -> None:
        pool = ProfilePool(rng=random.Random(0))
        blocked = pool.profiles[0]
        assert all(pool.pick(exclude=blocked) != blocked for _ in range(100))

    def test_pool_with_single_profile_still_returns_it(self) -> None:
        only = DEFAULT_PROFILES[0]
        assert ProfilePool([only]).pick(exclude=only) == only


class TestParser:
    def test_parses_schema_variants(self, catalog_payload: dict[str, Any]) -> None:
        page = parse_catalog_page(catalog_payload, base_url="https://www.vinted.es")
        assert [listing.id for listing in page.listings] == [5001, 5002, 5003]
        assert page.skipped == 2  # id no numérico + sin precio; el duplicado se ignora aparte
        assert (page.current_page, page.total_pages, page.is_last) == (1, 3, False)

        ps5, iphone, mando = page.listings
        assert ps5.price == Decimal("320.0")
        assert ps5.currency == "EUR"
        assert ps5.service_fee == Decimal("16.70")
        assert ps5.total_price == Decimal("336.70")
        assert ps5.status_label == "Muy bueno"
        assert ps5.brand == "Sony"
        assert ps5.seller_id == 77
        assert ps5.photo_urls == ("https://images.example/full/5001.jpeg",)
        assert ps5.listed_at == datetime.fromtimestamp(1760000000, tz=UTC)
        assert (ps5.favourite_count, ps5.view_count) == (3, 41)

        assert iphone.price == Decimal("410.0")  # precio como cadena (esquema antiguo)
        assert iphone.url == "https://www.vinted.es/items/5002-iphone-13"  # URL relativa resuelta
        assert len(iphone.photo_urls) == 2
        assert iphone.service_fee is None

        assert mando.brand == "Sony"  # marca como objeto
        assert mando.url == "https://www.vinted.es/items/5003"  # URL construida con el ID

    def test_seller_alias_is_not_kept(self, catalog_payload: dict[str, Any]) -> None:
        page = parse_catalog_page(catalog_payload, base_url="https://www.vinted.es")
        assert not any("vendedor_a" in repr(listing) for listing in page.listings)

    def test_empty_page_is_last(self) -> None:
        page = parse_catalog_page({"items": []}, base_url="https://www.vinted.es")
        assert page.listings == () and page.is_last

    @pytest.mark.parametrize(
        "payload",
        [
            {"catalog_items": []},  # clave renombrada
            [],  # no es un objeto
            {"items": [{"foo": 1}, {"bar": 2}]},  # todos malformados
        ],
    )
    def test_structural_changes_raise_schema_drift(self, payload: Any) -> None:
        with pytest.raises(SchemaDriftError):
            parse_catalog_page(payload, base_url="https://www.vinted.es")


class TestTransportFactory:
    async def test_builds_curl_cffi_and_httpx(self) -> None:
        profile = DEFAULT_PROFILES[0]
        curl = build_transport("curl_cffi", profile, timeout_s=5)
        assert isinstance(curl, CurlCffiTransport) and curl.sends_identity_headers
        assert curl.cookie_names() == set()
        await curl.aclose()

        plain = build_transport("httpx", profile, timeout_s=5)
        assert isinstance(plain, HttpxTransport) and not plain.sends_identity_headers
        await plain.aclose()

    async def test_unsupported_impersonation_falls_back(self) -> None:
        legacy = BrowserProfile(name="x", impersonate="chrome1", user_agent="UA")
        transport = build_transport("curl_cffi", legacy, timeout_s=5)
        await transport.aclose()

    def test_unknown_transport(self) -> None:
        with pytest.raises(ValueError):
            build_transport("requests", DEFAULT_PROFILES[0], timeout_s=5)
