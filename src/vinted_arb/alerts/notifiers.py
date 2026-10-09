"""Notificadores: Telegram Bot API y webhooks de Discord.

Los errores de entrega se registran y no se propagan: una alerta perdida no debe detener
la ingesta. El token del bot nunca aparece en los logs.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, Protocol

import httpx

from vinted_arb.alerts.formatting import discord_payload, telegram_caption
from vinted_arb.valuation.opportunity import Opportunity

logger = logging.getLogger(__name__)

MAX_RETRY_AFTER_S = 30.0


class Notifier(Protocol):
    async def send_opportunity(self, opp: Opportunity) -> bool: ...

    async def send_text(self, text: str) -> bool: ...

    async def aclose(self) -> None: ...


class TelegramNotifier:
    API = "https://api.telegram.org"

    def __init__(
        self,
        token: str,
        chat_id: str,
        *,
        client: httpx.AsyncClient | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._token = token
        self._chat_id = chat_id
        self._client = client or httpx.AsyncClient(timeout=15)
        self._sleep = sleep

    async def send_opportunity(self, opp: Opportunity) -> bool:
        caption = telegram_caption(opp)
        markup = json.dumps(
            {"inline_keyboard": [[{"text": "Abrir en Vinted", "url": opp.listing.url}]]}
        )
        if opp.listing.photo_urls:
            sent = await self._call(
                "sendPhoto",
                {
                    "chat_id": self._chat_id,
                    "photo": opp.listing.photo_urls[0],
                    "caption": caption,
                    "parse_mode": "HTML",
                    "reply_markup": markup,
                },
            )
            if sent:
                return True
            logger.info("sendPhoto falló; reintentando como mensaje de texto")
        return await self._call(
            "sendMessage",
            {
                "chat_id": self._chat_id,
                "text": caption,
                "parse_mode": "HTML",
                "reply_markup": markup,
                "disable_web_page_preview": "true",
            },
        )

    async def send_text(self, text: str) -> bool:
        return await self._call("sendMessage", {"chat_id": self._chat_id, "text": text[:4000]})

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _call(self, method: str, data: dict[str, str], *, retry: bool = True) -> bool:
        url = f"{self.API}/bot{self._token}/{method}"
        try:
            response = await self._client.post(url, data=data)
            payload: dict[str, Any] = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("Telegram %s: error de red (%s)", method, self._redact(str(exc)))
            return False
        if payload.get("ok"):
            return True
        retry_after = (payload.get("parameters") or {}).get("retry_after")
        if response.status_code == 429 and retry and isinstance(retry_after, int | float):
            await self._sleep(min(float(retry_after), MAX_RETRY_AFTER_S))
            return await self._call(method, data, retry=False)
        logger.warning(
            "Telegram %s rechazado (%s): %s",
            method,
            payload.get("error_code"),
            self._redact(str(payload.get("description"))),
        )
        return False

    def _redact(self, text: str) -> str:
        return text.replace(self._token, "***")


class DiscordNotifier:
    def __init__(
        self,
        webhook_url: str,
        *,
        client: httpx.AsyncClient | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._url = webhook_url
        self._client = client or httpx.AsyncClient(timeout=15)
        self._sleep = sleep

    async def send_opportunity(self, opp: Opportunity) -> bool:
        return await self._post(discord_payload(opp))

    async def send_text(self, text: str) -> bool:
        return await self._post({"content": text[:2000]})

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _post(self, body: dict[str, Any], *, retry: bool = True) -> bool:
        try:
            response = await self._client.post(self._url, json=body)
        except httpx.HTTPError as exc:
            logger.warning("Discord: error de red (%s)", type(exc).__name__)
            return False
        if response.status_code in (200, 204):
            return True
        if response.status_code == 429 and retry:
            try:
                retry_after = float(response.json().get("retry_after", 1.0))
            except ValueError:
                retry_after = 1.0
            await self._sleep(min(retry_after, MAX_RETRY_AFTER_S))
            return await self._post(body, retry=False)
        logger.warning("Discord rechazó la alerta (HTTP %s)", response.status_code)
        return False


class MultiNotifier:
    """Reparte cada alerta entre varios canales; basta con que uno la entregue."""

    def __init__(self, notifiers: Sequence[Notifier]) -> None:
        self._notifiers = list(notifiers)

    def __bool__(self) -> bool:
        return bool(self._notifiers)

    async def send_opportunity(self, opp: Opportunity) -> bool:
        results = await asyncio.gather(
            *(n.send_opportunity(opp) for n in self._notifiers), return_exceptions=True
        )
        return any(r is True for r in results)

    async def send_text(self, text: str) -> bool:
        results = await asyncio.gather(
            *(n.send_text(text) for n in self._notifiers), return_exceptions=True
        )
        return any(r is True for r in results)

    async def aclose(self) -> None:
        await asyncio.gather(*(n.aclose() for n in self._notifiers), return_exceptions=True)
