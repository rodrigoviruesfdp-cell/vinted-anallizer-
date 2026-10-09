"""Conexión SQLite (modo WAL) y migraciones versionadas con ``PRAGMA user_version``.

Convenciones: dinero en céntimos enteros, fechas en ISO 8601 UTC.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

_MIGRATIONS: tuple[str, ...] = (
    # v1: ingesta (Módulo 1)
    """
    CREATE TABLE listings (
        id                  INTEGER PRIMARY KEY,
        domain              TEXT    NOT NULL,
        title               TEXT    NOT NULL,
        brand               TEXT,
        status_label        TEXT,
        size_label          TEXT,
        price_cents         INTEGER NOT NULL,
        currency            TEXT    NOT NULL,
        service_fee_cents   INTEGER,
        total_price_cents   INTEGER,
        url                 TEXT    NOT NULL,
        photo_url           TEXT,
        seller_id           INTEGER,
        favourite_count     INTEGER,
        view_count          INTEGER,
        listed_at           TEXT,
        first_seen_at       TEXT    NOT NULL,
        last_seen_at        TEXT    NOT NULL,
        first_search        TEXT    NOT NULL
    );
    CREATE INDEX idx_listings_last_seen ON listings(last_seen_at);
    CREATE INDEX idx_listings_first_search ON listings(first_search, first_seen_at);

    CREATE TABLE price_history (
        listing_id   INTEGER NOT NULL REFERENCES listings(id) ON DELETE CASCADE,
        observed_at  TEXT    NOT NULL,
        price_cents  INTEGER NOT NULL,
        PRIMARY KEY (listing_id, observed_at)
    );

    CREATE TABLE kv (
        key         TEXT PRIMARY KEY,
        value       TEXT NOT NULL,
        updated_at  TEXT NOT NULL
    );

    CREATE TABLE poll_runs (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        search_name  TEXT    NOT NULL,
        started_at   TEXT    NOT NULL,
        finished_at  TEXT,
        status       TEXT    NOT NULL DEFAULT 'running',
        pages        INTEGER NOT NULL DEFAULT 0,
        items_seen   INTEGER NOT NULL DEFAULT 0,
        items_new    INTEGER NOT NULL DEFAULT 0,
        price_changes INTEGER NOT NULL DEFAULT 0,
        error        TEXT
    );
    CREATE INDEX idx_poll_runs_search ON poll_runs(search_name, started_at);
    """,
    # v2: normalización (Módulo 2) y valoración/alertas (Módulos 3-4)
    """
    CREATE TABLE listing_entities (
        listing_id          INTEGER PRIMARY KEY REFERENCES listings(id) ON DELETE CASCADE,
        normalizer_version  INTEGER NOT NULL,
        sku                 TEXT,
        family              TEXT,
        condition           TEXT    NOT NULL,
        match_confidence    REAL    NOT NULL,
        red_flags           TEXT    NOT NULL,
        accessories         TEXT    NOT NULL,
        battery_health      INTEGER,
        market_eligible     INTEGER NOT NULL,
        normalized_at       TEXT    NOT NULL
    );
    CREATE INDEX idx_entities_market ON listing_entities(sku, market_eligible);

    CREATE TABLE opportunities (
        listing_id            INTEGER PRIMARY KEY REFERENCES listings(id) ON DELETE CASCADE,
        evaluated_at          TEXT    NOT NULL,
        sku                   TEXT,
        condition             TEXT    NOT NULL,
        price_cents           INTEGER NOT NULL,
        expected_sale_cents   INTEGER,
        net_margin_cents      INTEGER,
        roi                   REAL,
        score                 REAL    NOT NULL,
        match_confidence      REAL    NOT NULL,
        fmv_confidence        REAL,
        risk                  REAL    NOT NULL,
        reference_source      TEXT,
        decision              TEXT    NOT NULL,
        reasons               TEXT    NOT NULL,
        breakdown             TEXT,
        alerted_at            TEXT,
        alerted_price_cents   INTEGER
    );
    CREATE INDEX idx_opportunities_decision ON opportunities(decision, evaluated_at);
    """,
)

SCHEMA_VERSION = len(_MIGRATIONS)


def connect(path: str | Path) -> sqlite3.Connection:
    """Abre (y migra) la base de datos. ``":memory:"`` para tests."""
    target = str(path)
    if target != ":memory:":
        Path(target).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target, isolation_level=None)  # autocommit; transacciones explícitas
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    if target != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
    migrate(conn)
    return conn


def migrate(conn: sqlite3.Connection) -> int:
    """Aplica las migraciones pendientes. Devuelve la versión final."""
    current = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if current > SCHEMA_VERSION:
        raise RuntimeError(
            f"La base de datos tiene versión {current} y este código solo conoce {SCHEMA_VERSION}"
        )
    for version in range(current, SCHEMA_VERSION):
        conn.execute("BEGIN")
        try:
            for statement in _split_statements(_MIGRATIONS[version]):
                conn.execute(statement)
            conn.execute(f"PRAGMA user_version = {version + 1}")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")
    return SCHEMA_VERSION


def _split_statements(script: str) -> list[str]:
    return [stmt.strip() for stmt in script.split(";") if stmt.strip()]
