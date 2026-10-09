"""Configuración, búsquedas guardadas y CLI."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from vinted_arb.cli import main
from vinted_arb.config import ConfigError, Settings, load_searches
from vinted_arb.models import SearchQuery

REPO_ROOT = Path(__file__).resolve().parents[1]


def write(tmp_path: Path, content: str) -> Path:
    path = tmp_path / "searches.yaml"
    path.write_text(content, encoding="utf-8")
    return path


class TestSearches:
    def test_repository_searches_file_is_valid(self) -> None:
        searches = load_searches(REPO_ROOT / "config" / "searches.yaml")
        assert len(searches) >= 5
        assert all(s.price_from is not None for s in searches)  # defensa anti falsos positivos

    def test_valid_file(self, tmp_path: Path) -> None:
        path = write(
            tmp_path,
            """
searches:
  - name: ps5
    search_text: ps5
    catalog_ids: [3026, 3027]
    status_ids: [2, 3]
    price_from: 150
    interval_s: 120
""",
        )
        (query,) = load_searches(path)
        assert query.catalog_ids == (3026, 3027)
        assert query.to_params(2)["catalog_ids"] == "3026,3027"
        assert query.to_params(2)["status_ids"] == "2,3"
        assert query.to_params(2)["page"] == "2"

    @pytest.mark.parametrize(
        ("content", "message"),
        [
            ("searches: []", "lista no vacía"),
            ("otra_clave: 1", "lista no vacía"),
            ("searches: [{name: a, search_text: x}, {name: a, search_text: y}]", "duplicados"),
            ("searches: [{name: a, search_text: x, price_from: 300, price_to: 100}]", "inválida"),
            ("searches: [{name: a, search_text: x, interval_s: 5}]", "inválida"),
            ("searches: [{name: A B, search_text: x}]", "inválida"),
            ("searches: [{name: a, search_text: x, typo_field: 1}]", "inválida"),
            ("searches: [unclosed", "YAML inválido"),
        ],
    )
    def test_invalid_files(self, tmp_path: Path, content: str, message: str) -> None:
        with pytest.raises(ConfigError, match=message):
            load_searches(write(tmp_path, content))

    def test_missing_file(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match="No existe"):
            load_searches(tmp_path / "nope.yaml")

    def test_price_params_are_plain_numbers(self) -> None:
        query = SearchQuery(name="q", search_text="x", price_from=Decimal("150.00"))
        assert query.to_params(1)["price_from"] == "150"


class TestSettings:
    def test_env_overrides_and_empty_secrets(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("VA_RATE_LIMIT_PER_MINUTE", "3")
        monkeypatch.setenv("VA_TELEGRAM_BOT_TOKEN", "")
        monkeypatch.setenv("VA_LOG_LEVEL", "debug")
        settings = Settings(_env_file=None)  # type: ignore[call-arg]
        assert settings.rate_limit_per_minute == 3
        assert settings.telegram_bot_token is None
        assert settings.log_level == "DEBUG"

    def test_domain_must_be_a_host(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("VA_VINTED_DOMAIN", "https://www.vinted.es")
        with pytest.raises(ValueError):
            Settings(_env_file=None)  # type: ignore[call-arg]

    def test_env_example_matches_settings(self) -> None:
        documented = {
            line.split("=", 1)[0].lstrip("# ").strip()
            for line in (REPO_ROOT / ".env.example").read_text().splitlines()
            if line.lstrip("# ").startswith("VA_")
        }
        fields = {f"VA_{name.upper()}" for name in Settings.model_fields}
        assert documented == fields


class TestCli:
    def test_fees_breakdown_and_limit(self, capsys: pytest.CaptureFixture[str]) -> None:
        code = main(
            ["fees", "--buy", "220", "--sell", "300", "--min-net", "30", "--min-roi", "0.2"]
        )
        out = capsys.readouterr().out
        assert code == 0
        assert "net_margin         55.70" in out
        assert "SÍ" in out
        # Manda el ROI: 293,30 − 1,08·B ≥ 0,2·(1,05·B + 6,70)  ⇒  B ≤ 226,33
        assert "Precio máximo de compra en Vinted: 226.33 €" in out

    def test_fees_unreachable(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert main(["fees", "--sell", "20", "--min-net", "30"]) == 0
        assert "no se alcanzan" in capsys.readouterr().out

    def test_parse_title(self, capsys: pytest.CaptureFixture[str]) -> None:
        code = main(["parse-title", "PS5 Slim digital con caja", "--status", "Muy bueno"])
        data = json.loads(capsys.readouterr().out)
        assert code == 0
        assert data["sku"] == "console:ps5-slim:digital"
        assert data["condition"] == "very_good"
        assert data["accessories"] == ["box"]
        assert data["is_candidate"] is True

    def test_init_db_and_runs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv("VA_DB_PATH", str(tmp_path / "db.sqlite3"))
        assert main(["init-db"]) == 0
        assert main(["runs"]) == 0
        assert "Sin ejecuciones" in capsys.readouterr().out

    def test_unknown_search_is_a_config_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv("VA_DB_PATH", str(tmp_path / "db.sqlite3"))
        searches = str(REPO_ROOT / "config" / "searches.yaml")
        assert main(["poll", "--once", "--searches", searches, "--search", "no-existe"]) == 2
        assert "desconocidas" in capsys.readouterr().err


class TestValuationCli:
    @pytest.fixture
    def env(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        monkeypatch.setenv("VA_DB_PATH", str(tmp_path / "db.sqlite3"))
        book = tmp_path / "book.csv"
        book.write_text(
            "sku,condition,price_eur,updated_at,notes\n"
            # Fecha futura: el test no caduca por envejecimiento del precio manual.
            "console:ps5-slim:digital,,320,2099-01-01,referencia manual\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("VA_PRICE_BOOK_FILE", str(book))
        return tmp_path

    def test_evaluate_against_price_book(
        self, env: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = main(["evaluate", "PS5 Slim Digital 1TB", "--price", "200", "--status", "Muy bueno"])
        out = capsys.readouterr().out
        assert code == 0
        assert "Decisión: ALERT" in out
        assert "price_book" in out
        assert "Margen neto: 97,30 €" in out  # 320 − 200 − 10,70 − 4,50 − 1,50 − 6,00

    def test_evaluate_rejects_accessory(
        self, env: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main(["evaluate", "Mando PS5 DualSense", "--price", "30"]) == 0
        assert "Decisión: REJECT" in capsys.readouterr().out

    def test_market_and_opps_without_data(
        self, env: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main(["market"]) == 0
        assert "Aún no hay comparables" in capsys.readouterr().out
        assert main(["market", "--sku", "console:ps5-slim:digital"]) == 0
        assert "price_book" in capsys.readouterr().out
        assert main(["opps"]) == 0
        assert "Evaluaciones: ninguna" in capsys.readouterr().out

    def test_alert_test_requires_a_channel(
        self, env: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main(["alert-test"]) == 2
        assert "No hay canales configurados" in capsys.readouterr().err

    def test_fees_defaults_to_vinted_channel(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert main(["fees", "--buy", "220", "--sell", "300", "--promotion", "2.45"]) == 0
        out = capsys.readouterr().out
        assert "channel            vinted" in out
        assert "net_margin         53.25" in out

    def test_invalid_price_book_is_a_config_error(
        self, env: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        bad = env / "bad.csv"
        bad.write_text("sku,condition,price_eur\nx:y,nuevo_raro,10\n", encoding="utf-8")
        monkeypatch.setenv("VA_PRICE_BOOK_FILE", str(bad))
        assert main(["evaluate", "PS5 Slim Digital 1TB", "--price", "200"]) == 2
        assert "estado 'nuevo_raro' inválido" in capsys.readouterr().err
