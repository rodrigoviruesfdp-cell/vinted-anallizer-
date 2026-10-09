"""Valoración contra el mercado de Vinted: estadística, price book y evaluación (Módulo 3)."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from vinted_arb.models import Listing
from vinted_arb.normalization import Condition, extract
from vinted_arb.valuation.fees import VINTED, DealCosts
from vinted_arb.valuation.market import (
    MarketEstimate,
    PricePoint,
    effective_sample_size,
    estimate_market,
    is_market_eligible,
    mad_inliers,
    recency_weight,
    weighted_quantile,
)
from vinted_arb.valuation.opportunity import (
    Decision,
    EvaluationParams,
    evaluate,
    implausible_discount_risk,
)
from vinted_arb.valuation.reference import (
    BookQuote,
    PriceBook,
    PriceBookError,
    Reference,
    resolve_reference,
)

D = Decimal
NOW = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)


def points(prices: list[int], condition: Condition = Condition.VERY_GOOD, age_days: float = 0):
    return [
        PricePoint(D(p), condition, NOW - timedelta(days=age_days), listing_id=i)
        for i, p in enumerate(prices)
    ]


class TestStatistics:
    def test_weighted_quantile_equal_weights(self) -> None:
        values = [10.0, 20.0, 30.0, 40.0]
        weights = [1.0] * 4
        assert weighted_quantile(values, weights, 0.5) == pytest.approx(25.0)
        assert weighted_quantile(values, weights, 0.0) == 10.0
        assert weighted_quantile(values, weights, 1.0) == 40.0
        assert weighted_quantile(values, weights, 0.125) == pytest.approx(10.0)

    def test_weights_pull_the_quantile(self) -> None:
        assert weighted_quantile([100.0, 200.0], [9.0, 1.0], 0.5) < 150

    def test_unsorted_input(self) -> None:
        assert weighted_quantile([30.0, 10.0, 20.0], [1, 1, 1], 0.5) == pytest.approx(20.0)

    @pytest.mark.parametrize(
        ("values", "weights", "q"),
        [([], [], 0.5), ([1.0], [1.0, 2.0], 0.5), ([1.0], [1.0], 1.5), ([1.0], [0.0], 0.5)],
    )
    def test_invalid_quantile_inputs(self, values, weights, q) -> None:  # type: ignore[no-untyped-def]
        with pytest.raises(ValueError):
            weighted_quantile(values, weights, q)

    def test_mad_removes_outliers(self) -> None:
        values = [300.0, 310.0, 295.0, 305.0, 320.0, 290.0, 40.0, 999.0]
        assert mad_inliers(values) == [True] * 6 + [False, False]

    def test_mad_zero_falls_back_to_relative_band(self) -> None:
        assert mad_inliers([100.0, 100.0, 100.0, 150.0]) == [True, True, True, False]

    def test_recency_weight_half_life(self) -> None:
        assert recency_weight(NOW - timedelta(days=14), NOW, 14) == pytest.approx(0.5)
        assert recency_weight(NOW + timedelta(days=1), NOW, 14) == 1.0  # nunca > 1

    def test_effective_sample_size(self) -> None:
        assert effective_sample_size([1, 1, 1, 1]) == pytest.approx(4)
        assert effective_sample_size([1, 0.01, 0.01]) < 1.1


class TestEstimateMarket:
    def test_exact_condition_with_enough_samples(self) -> None:
        prices = [280, 290, 300, 300, 310, 320, 330, 340, 350, 360, 370, 380, 45]
        est = estimate_market(
            points(prices), condition=Condition.VERY_GOOD, now=NOW, min_samples=12
        )
        assert est is not None
        assert not est.pooled
        assert est.outliers_removed == 1  # los 45 € (accesorio o trampa) fuera
        assert est.n == 12
        assert est.confidence == 1.0
        assert est.p25 <= est.quick_sale <= est.p50 <= est.p75
        assert est.quick_sale == D("301.00")  # P30 de Hazen entre 300 y 310

    def test_pooling_rescales_other_conditions(self) -> None:
        good = points([270] * 10, Condition.GOOD)  # 270 / 0,90 = 300 equivalente "muy bueno"
        sealed = points([360] * 4, Condition.NEW_SEALED)  # 360 / 1,20 = 300
        est = estimate_market(good + sealed, condition=Condition.VERY_GOOD, now=NOW, min_samples=12)
        assert est is not None and est.pooled
        assert est.quick_sale == D("300.00")
        assert est.confidence == pytest.approx(0.85)  # penalización por agregar estados

        fair = estimate_market(good + sealed, condition=Condition.FAIR, now=NOW, min_samples=12)
        assert fair is not None and fair.quick_sale == D("234.00")  # 300 × 0,78

    def test_low_sample_confidence(self) -> None:
        est = estimate_market(points([300, 310, 320]), condition=Condition.VERY_GOOD, now=NOW)
        assert est is not None
        assert est.confidence == pytest.approx((3 / 12) ** 0.5, abs=1e-3)

    def test_old_observations_weigh_less(self) -> None:
        fresh = points([300] * 6)
        stale = [
            PricePoint(D(400), Condition.VERY_GOOD, NOW - timedelta(days=28), listing_id=100 + i)
            for i in range(6)
        ]
        est = estimate_market(fresh + stale, condition=Condition.VERY_GOOD, now=NOW, min_samples=4)
        assert est is not None and est.p50 < D("350")

    def test_not_enough_data(self) -> None:
        assert estimate_market(points([300, 310]), condition=Condition.VERY_GOOD, now=NOW) is None
        assert estimate_market([], condition=Condition.GOOD, now=NOW) is None

    def test_for_parts_and_unknown_are_not_valued(self) -> None:
        data = points([300] * 20)
        assert estimate_market(data, condition=Condition.FOR_PARTS, now=NOW) is None
        assert estimate_market(data, condition=Condition.UNKNOWN, now=NOW) is None


class TestMarketEligibility:
    @pytest.mark.parametrize(
        ("title", "status", "eligible"),
        [
            ("PS5 Slim Digital 1TB", "Muy bueno", True),
            ("Consola PS5", "Muy bueno", False),  # variantes sin resolver
            ("Mando PS5 DualSense", "Muy bueno", False),  # accesorio
            ("iPhone 13 128GB pantalla rota", "Bueno", False),  # distorsiona el precio
            ("PS5 Slim Digital 1TB", None, False),  # estado desconocido
            ("iPhone 13 128GB para piezas", "Bueno", False),
        ],
    )
    def test_eligibility(self, title: str, status: str | None, eligible: bool) -> None:
        assert is_market_eligible(extract(title, status_label=status)) is eligible


class TestPriceBook:
    def write(self, tmp_path: Path, rows: str) -> Path:
        path = tmp_path / "price_book.csv"
        path.write_text("sku,condition,price_eur,updated_at,notes\n" + rows, encoding="utf-8")
        return path

    def test_exact_and_multiplier_fallback(self, tmp_path: Path) -> None:
        book = PriceBook.load(
            self.write(
                tmp_path,
                "console:ps5-slim:digital,,330,2026-10-01,base muy bueno\n"
                "console:ps5-slim:digital,new_sealed,420,2026-10-01,\n"
                "#console:ignorado,,1,,comentario\n",
            )
        )
        assert len(book) == 2
        today = date(2026, 10, 9)
        sealed = book.quote("console:ps5-slim:digital", Condition.NEW_SEALED, today=today)
        assert sealed == BookQuote(D("420.00"), 0.7, exact_condition=True)
        good = book.quote("console:ps5-slim:digital", Condition.GOOD, today=today)
        assert good is not None and good.price == D("297.00") and not good.exact_condition
        assert book.quote("phone:otro", Condition.GOOD, today=today) is None

    def test_confidence_decays_with_age(self, tmp_path: Path) -> None:
        book = PriceBook.load(self.write(tmp_path, "a:b,very_good,100,2026-05-01,\n"))
        old = book.quote("a:b", Condition.VERY_GOOD, today=date(2026, 10, 9))
        assert old is not None and old.confidence == 0.3

    def test_missing_file_is_empty(self, tmp_path: Path) -> None:
        assert len(PriceBook.load(tmp_path / "nope.csv")) == 0

    def test_repository_template_loads(self) -> None:
        template = Path(__file__).resolve().parents[1] / "config" / "price_book.csv"
        assert len(PriceBook.load(template)) == 0

    @pytest.mark.parametrize(
        "row",
        [
            "a:b,como_nuevo,100,,\n",
            "a:b,,cien,,\n",
            "a:b,,-5,,\n",
            "a:b,,100,09/10/2026,\n",
        ],
    )
    def test_invalid_rows(self, tmp_path: Path, row: str) -> None:
        with pytest.raises(PriceBookError):
            PriceBook.load(self.write(tmp_path, row))


class TestResolveReference:
    def market(self, confidence: float = 1.0) -> MarketEstimate:
        est = estimate_market(points([300] * 12), condition=Condition.VERY_GOOD, now=NOW)
        assert est is not None
        return replace(est, confidence=confidence)

    def test_market_only_applies_ask_to_sale(self) -> None:
        ref = resolve_reference(self.market(), None, ask_to_sale=D("0.92"))
        assert ref is not None
        assert ref.sale_price == D("276.00") and ref.source == "vinted_market"

    def test_blend_with_book(self) -> None:
        book = BookQuote(D("320"), 0.7, exact_condition=True)
        ref = resolve_reference(self.market(0.5), book, ask_to_sale=D("1"))
        assert ref is not None and ref.source == "blend"
        assert ref.sale_price == D("310.00")
        assert ref.confidence == pytest.approx(0.85)

    def test_book_only_and_nothing(self) -> None:
        book = BookQuote(D("320"), 0.7, exact_condition=True)
        ref = resolve_reference(None, book, ask_to_sale=D("0.92"))
        assert ref is not None and ref.sale_price == D("320") and ref.source == "price_book"
        assert resolve_reference(None, None, ask_to_sale=D("0.92")) is None


def listing(title: str, price: str, *, status: str | None = "Muy bueno", fee: str | None = None):
    return Listing(
        id=1,
        title=title,
        price=D(price),
        currency="EUR",
        url="https://www.vinted.es/items/1",
        status_label=status,
        service_fee=D(fee) if fee else None,
    )


REF_300 = Reference(D("300"), 0.9, "vinted_market")
PARAMS = EvaluationParams(channel=VINTED, costs=DealCosts())


class TestEvaluate:
    def test_alert_when_all_thresholds_met(self) -> None:
        item = listing("PS5 Slim Digital 1TB", "200")
        opp = evaluate(item, extract(item.title, status_label=item.status_label), REF_300, PARAMS)
        assert opp.decision is Decision.ALERT
        assert opp.breakdown is not None
        assert opp.breakdown.channel == "vinted"
        # 300 − 200 − (0,70 + 10) − 4,50 − 1,50 − 6,00 (reserva 3 %)
        assert opp.breakdown.net_margin == D("77.30")
        assert opp.discount == D("0.333")
        assert opp.risk == 0.0
        assert opp.score == pytest.approx(77.30 * 0.95 * 0.9, abs=0.01)

    def test_watch_when_margin_positive_but_short(self) -> None:
        item = listing("PS5 Slim Digital 1TB", "255")
        opp = evaluate(item, extract(item.title, status_label=item.status_label), REF_300, PARAMS)
        assert opp.decision is Decision.WATCH
        assert any("ROI" in r for r in opp.reasons)

    def test_reject_when_losing_money(self) -> None:
        item = listing("PS5 Slim Digital 1TB", "295")
        opp = evaluate(item, extract(item.title, status_label=item.status_label), REF_300, PARAMS)
        assert opp.decision is Decision.REJECT

    def test_exact_service_fee_is_used(self) -> None:
        item = listing("PS5 Slim Digital 1TB", "200", fee="9.99")
        opp = evaluate(item, extract(item.title), REF_300, PARAMS)
        assert opp.breakdown is not None and opp.breakdown.vinted_fee == D("9.99")

    @pytest.mark.parametrize(
        ("title", "reference", "reason"),
        [
            ("Camiseta Zara", REF_300, "producto no reconocido"),
            ("Mando PS5 DualSense", REF_300, "accessory_only"),
            ("Consola PS5", REF_300, "identificación insuficiente"),
            ("PS5 Slim Digital 1TB", None, "sin precio de referencia"),
        ],
    )
    def test_rejections(self, title: str, reference: Reference | None, reason: str) -> None:
        item = listing(title, "100")
        opp = evaluate(item, extract(title, status_label="Muy bueno"), reference, PARAMS)
        assert opp.decision is Decision.REJECT
        assert reason in opp.reasons[0]

    def test_too_good_to_be_true_is_not_alerted(self) -> None:
        item = listing("PS5 Slim Digital 1TB", "75")  # −75 %: estafa o pieza
        opp = evaluate(item, extract(item.title, status_label="Muy bueno"), REF_300, PARAMS)
        assert opp.decision is Decision.WATCH
        assert opp.risk_factors["implausible_discount"] == 0.6
        assert any("riesgo" in r for r in opp.reasons)

    def test_unknown_condition_valued_as_good_with_risk(self) -> None:
        item = listing("PS5 Slim Digital 1TB", "200", status=None)
        opp = evaluate(item, extract(item.title), REF_300, PARAMS)
        assert opp.condition is Condition.GOOD
        assert opp.risk_factors == {"unknown_condition": 0.1}

    def test_low_fmv_confidence_blocks_alert(self) -> None:
        item = listing("PS5 Slim Digital 1TB", "200")
        weak = Reference(D("300"), 0.4, "vinted_market_pooled")
        opp = evaluate(item, extract(item.title, status_label="Muy bueno"), weak, PARAMS)
        assert opp.decision is Decision.WATCH

    @pytest.mark.parametrize(
        ("discount", "risk"),
        [("0.30", 0.0), ("0.40", 0.0), ("0.55", 0.3), ("0.70", 0.6), ("0.9", 0.6)],
    )
    def test_implausible_discount_curve(self, discount: str, risk: float) -> None:
        assert implausible_discount_risk(D(discount)) == pytest.approx(risk)
