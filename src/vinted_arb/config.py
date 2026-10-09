"""Configuración: variables de entorno ``VA_*`` (o ``.env``) y búsquedas en YAML."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import Field, SecretStr, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from vinted_arb.models import SearchQuery


class ConfigError(ValueError):
    """Configuración inválida, con un mensaje pensado para la persona que la escribió."""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="VA_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # Vinted
    vinted_domain: str = Field(default="www.vinted.es", pattern=r"^[a-z0-9.-]+\.[a-z]{2,}$")
    accept_language: str = "es-ES,es;q=0.9"

    # Transporte
    http_transport: Literal["curl_cffi", "httpx"] = "curl_cffi"
    http_timeout_s: float = Field(default=20.0, gt=0)
    http_proxy_url: str | None = None

    # Ritmo y resiliencia
    rate_limit_per_minute: float = Field(default=6.0, gt=0, le=60)
    rate_limit_burst: int = Field(default=2, ge=1, le=10)
    rate_limit_jitter: float = Field(default=0.35, ge=0, le=2)
    retry_max_attempts: int = Field(default=4, ge=1, le=10)
    retry_base_delay_s: float = Field(default=3.0, gt=0)
    retry_max_delay_s: float = Field(default=90.0, gt=0)
    breaker_failure_threshold: int = Field(default=3, ge=1)
    breaker_cooldown_s: float = Field(default=900.0, ge=60)
    breaker_max_cooldown_s: float = Field(default=21600.0, ge=60)
    session_max_age_s: float = Field(default=2400.0, ge=60)
    poll_interval_jitter: float = Field(default=0.2, ge=0, lt=1)

    # Persistencia
    db_path: Path = Path("data/vinted_arb.sqlite3")
    searches_file: Path = Path("config/searches.yaml")

    # Reventa y costes (Módulo 3)
    resale_channel: Literal["vinted", "wallapop", "ebay_es_private", "local_cash"] = "vinted"
    inbound_shipping_eur: Decimal = Field(default=Decimal("4.50"), ge=0)
    outbound_shipping_eur: Decimal = Field(default=Decimal("6.00"), ge=0)
    packaging_eur: Decimal = Field(default=Decimal("1.50"), ge=0)
    promotion_eur: Decimal = Field(default=Decimal("0"), ge=0)
    risk_reserve_rate: Decimal = Field(default=Decimal("0.03"), ge=0, le=1)
    rebu_enabled: bool = False

    # Valoración (Módulo 3)
    fmv_percentile: float = Field(default=0.30, gt=0, lt=1)
    fmv_ask_to_sale: Decimal = Field(default=Decimal("0.92"), gt=0, le=1)
    fmv_window_days: int = Field(default=30, ge=1)
    fmv_half_life_days: float = Field(default=14.0, gt=0)
    fmv_min_samples: int = Field(default=12, ge=1)
    price_book_file: Path = Path("config/price_book.csv")

    # Umbrales de oportunidad (Módulo 3)
    min_net_profit_eur: Decimal = Field(default=Decimal("30"), ge=0)
    min_roi: Decimal = Field(default=Decimal("0.20"), ge=0)
    min_match_confidence: float = Field(default=0.80, ge=0, le=1)
    min_fmv_confidence: float = Field(default=0.60, ge=0, le=1)
    max_risk: float = Field(default=0.50, ge=0, le=1)

    # Alertas (Módulo 4)
    alert_max_per_hour: int = Field(default=30, ge=1)
    telegram_bot_token: SecretStr | None = None
    telegram_chat_id: str | None = None
    discord_webhook_url: SecretStr | None = None

    # Referencias de mercado (Módulo 3)
    ebay_client_id: str | None = None
    ebay_client_secret: SecretStr | None = None
    ebay_marketplace_id: str = "EBAY_ES"

    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    @field_validator(
        "http_proxy_url",
        "telegram_bot_token",
        "telegram_chat_id",
        "discord_webhook_url",
        "ebay_client_id",
        "ebay_client_secret",
        mode="before",
    )
    @classmethod
    def _empty_as_none(cls, value: Any) -> Any:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("log_level", mode="before")
    @classmethod
    def _upper_log_level(cls, value: Any) -> Any:
        return value.upper() if isinstance(value, str) else value


def load_searches(path: str | Path) -> list[SearchQuery]:
    """Carga y valida ``config/searches.yaml``."""
    file = Path(path)
    if not file.exists():
        raise ConfigError(f"No existe el fichero de búsquedas: {file}")
    try:
        data = yaml.safe_load(file.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"YAML inválido en {file}: {exc}") from exc
    raw = data.get("searches") if isinstance(data, dict) else None
    if not isinstance(raw, list) or not raw:
        raise ConfigError(f"{file} debe contener una lista no vacía bajo la clave 'searches'")

    searches: list[SearchQuery] = []
    for index, entry in enumerate(raw):
        try:
            searches.append(SearchQuery.model_validate(entry))
        except ValidationError as exc:
            label = entry.get("name", f"#{index}") if isinstance(entry, dict) else f"#{index}"
            raise ConfigError(f"Búsqueda '{label}' inválida en {file}:\n{exc}") from exc

    names = [s.name for s in searches]
    duplicates = sorted({n for n in names if names.count(n) > 1})
    if duplicates:
        raise ConfigError(f"Nombres de búsqueda duplicados en {file}: {', '.join(duplicates)}")
    return searches
