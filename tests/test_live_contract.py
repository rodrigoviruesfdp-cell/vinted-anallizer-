"""Test de contrato contra Vinted real. Excluido por defecto; ejecútalo con ``pytest -m live``.

Detecta cambios en la API interna (esquema, cookies de sesión, bloqueos) antes de que
afecten a la ingesta continua. Hace 2 peticiones: la home y una página de catálogo.
"""

from __future__ import annotations

import pytest

from vinted_arb.config import Settings
from vinted_arb.ingestion.vinted_client import has_session_cookie
from vinted_arb.models import SearchQuery
from vinted_arb.wiring import build_client

pytestmark = pytest.mark.live


async def test_catalog_contract() -> None:
    client = build_client(Settings())
    try:
        page = await client.search(
            SearchQuery(name="contract", search_text="ps5", per_page=20, price_from=100)
        )
        session = client._session  # inspección deliberada en el test de contrato
        assert session is not None
        assert has_session_cookie(session.transport.cookie_names()), (
            "La home ya no fija cookies de sesión conocidas: revisar has_session_cookie()"
        )
    finally:
        await client.aclose()

    assert page.listings, "El catálogo no devolvió anuncios"
    assert page.skipped == 0, "Hay anuncios que el parser ya no entiende"
    sample = page.listings[0]
    assert sample.price > 0 and sample.url.startswith("https://")
    assert sample.photo_urls, "Los anuncios ya no traen fotos en el catálogo"
