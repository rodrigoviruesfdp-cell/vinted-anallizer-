"""Comisiones y margen neto (Módulo 3, núcleo)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from vinted_arb.valuation import (
    EBAY_ES_PRIVATE,
    VINTED,
    WALLAPOP,
    DealCosts,
    ResaleChannel,
    VintedBuyerFee,
    compute_margin,
    max_purchase_price,
    money,
    rebu_vat,
)

D = Decimal


class TestMoney:
    def test_rounds_half_up_to_cents(self) -> None:
        assert money("1.005") == D("1.01")
        assert money("1.004") == D("1.00")
        assert money(3) == D("3.00")

    def test_rejects_float(self) -> None:
        with pytest.raises(TypeError):
            money(0.1)  # type: ignore[arg-type]


class TestVintedBuyerFee:
    @pytest.mark.parametrize(
        ("price", "expected"),
        [
            ("0", "0.70"),
            ("100", "5.70"),
            ("320", "16.70"),  # coincide con el service_fee del fixture
            ("10.10", "1.21"),  # 0,70 + 0,505 → redondeo a favor de céntimo
        ],
    )
    def test_fixed_plus_percentage(self, price: str, expected: str) -> None:
        assert VintedBuyerFee()(D(price)) == D(expected)

    def test_negative_price_rejected(self) -> None:
        with pytest.raises(ValueError):
            VintedBuyerFee()(D("-1"))

    def test_custom_schedule(self) -> None:
        assert VintedBuyerFee(fixed=D("1"), rate=D("0.08"))(D("50")) == D("5.00")


class TestResaleChannels:
    def test_wallapop_and_vinted_charge_seller_nothing(self) -> None:
        assert WALLAPOP.fees(D("300"), D("6")) == D("0.00")
        assert VINTED.fees(D("300")) == D("0.00")

    def test_ebay_percentage_includes_shipping_charged(self) -> None:
        # 11,5 % de (300 + 6) + 0,35
        assert EBAY_ES_PRIVATE.fees(D("300"), D("6")) == D("35.54")

    def test_ebay_small_order_fixed_fee(self) -> None:
        # pedido < 10 €: 11,5 % de 8 + 0,05
        assert EBAY_ES_PRIVATE.fees(D("8")) == D("0.97")

    def test_negative_amounts_rejected(self) -> None:
        with pytest.raises(ValueError):
            EBAY_ES_PRIVATE.fees(D("-1"))


class TestComputeMargin:
    def test_reference_example_wallapop(self) -> None:
        b = compute_margin(purchase_price="220", sale_price="300", channel=WALLAPOP)
        assert b.vinted_fee == D("11.70")
        assert b.inbound_shipping == D("4.50")
        assert b.resale_fees == D("0.00")
        assert b.outbound_shipping == D("0.00")  # en Wallapop el envío lo paga el comprador
        assert b.packaging == D("1.50")
        assert b.risk_reserve == D("6.60")
        assert b.total_costs == D("244.30")
        assert b.cash_invested == D("237.70")
        assert b.net_margin == D("55.70")
        assert b.roi == D("0.2343")

    def test_reference_example_ebay(self) -> None:
        b = compute_margin(purchase_price="220", sale_price="300", channel=EBAY_ES_PRIVATE)
        assert b.resale_fees == D("35.54")
        assert b.net_margin == D("20.16")

    def test_formula_identity(self) -> None:
        b = compute_margin(purchase_price="137.45", sale_price="249.99", channel=EBAY_ES_PRIVATE)
        expected = (
            b.sale_price
            - b.purchase_price
            - b.vinted_fee
            - b.resale_fees
            - b.inbound_shipping
            - b.outbound_shipping
            - b.packaging
            - b.promotion
            - b.risk_reserve
            - b.vat_on_margin
        )
        assert b.net_margin == expected

    def test_exact_service_fee_from_api_overrides_formula(self) -> None:
        b = compute_margin(purchase_price="320", sale_price="420", vinted_fee_override="15.10")
        assert b.vinted_fee == D("15.10")

    def test_seller_paid_shipping_is_a_cost_and_not_fee_base(self) -> None:
        free_shipping_ebay = ResaleChannel(
            name="ebay_free_shipping",
            rate=D("0.115"),
            fixed_per_order=D("0.35"),
            fee_includes_shipping=True,
            seller_pays_shipping=True,
        )
        b = compute_margin(purchase_price="200", sale_price="300", channel=free_shipping_ebay)
        assert b.outbound_shipping == D("6.00")
        assert b.resale_fees == D("34.85")  # 11,5 % de 300 (sin envío) + 0,35
        assert b.cash_invested == D("200") + b.vinted_fee + D("4.50") + D("1.50") + D("6.00")

    def test_rebu_vat_applies_on_margin(self) -> None:
        costs = DealCosts(rebu_vat_rate=D("0.21"))
        b = compute_margin(purchase_price="220", sale_price="300", costs=costs)
        assert b.vat_on_margin == D("13.88")  # 80 × 21/121
        assert b.net_margin == D("41.82")

    def test_loss_making_deal_has_negative_margin_and_roi(self) -> None:
        b = compute_margin(purchase_price="300", sale_price="290")
        assert b.net_margin < 0
        assert b.roi < 0

    def test_vinted_resale_with_bump(self) -> None:
        costs = DealCosts(promotion=D("2.45"))
        b = compute_margin(purchase_price="220", sale_price="300", channel=VINTED, costs=costs)
        assert b.resale_fees == D("0.00")  # en Vinted el vendedor no paga comisión
        assert b.outbound_shipping == D("0.00")  # el envío lo paga el comprador
        assert b.promotion == D("2.45")
        assert b.net_margin == D("53.25")  # 55,70 − 2,45
        assert b.cash_invested == D("240.15")

    def test_negative_costs_rejected(self) -> None:
        with pytest.raises(ValueError):
            DealCosts(packaging=D("-1"))


class TestRebuVat:
    def test_no_vat_without_margin(self) -> None:
        assert rebu_vat(D("100"), D("120"), D("0.21")) == D("0.00")

    def test_vat_inside_margin(self) -> None:
        assert rebu_vat(D("221"), D("100"), D("0.21")) == D("21.00")


class TestMaxPurchasePrice:
    @pytest.mark.parametrize(
        ("sale", "channel", "rebu", "expected"),
        [
            # Tabla de ARCHITECTURE.md §2.2 (≥30 € netos y ROI ≥20 %)
            ("80", WALLAPOP, False, "40.09"),
            ("150", WALLAPOP, False, "104.90"),
            ("400", WALLAPOP, False, "303.85"),
            ("900", WALLAPOP, False, "691.47"),
            ("400", EBAY_ES_PRIVATE, False, "267.39"),
            ("400", WALLAPOP, True, "288.90"),
        ],
    )
    def test_matches_architecture_table(
        self, sale: str, channel: ResaleChannel, rebu: bool, expected: str
    ) -> None:
        costs = DealCosts(rebu_vat_rate=D("0.21") if rebu else None)
        result = max_purchase_price(
            sale_price=sale,
            channel=channel,
            min_net_margin="30",
            min_roi=D("0.20"),
            costs=costs,
        )
        assert result == D(expected)

    def test_result_is_the_exact_boundary(self) -> None:
        limit = max_purchase_price(sale_price="400", min_net_margin="30", min_roi=D("0.20"))
        assert limit is not None
        ok = compute_margin(purchase_price=limit, sale_price="400")
        assert ok.net_margin >= 30 and ok.roi >= D("0.20")
        over = compute_margin(purchase_price=limit + D("0.01"), sale_price="400")
        assert over.net_margin < 30 or over.roi < D("0.20")

    def test_unreachable_targets_return_none(self) -> None:
        assert max_purchase_price(sale_price="20", min_net_margin="30") is None

    def test_negative_thresholds_rejected(self) -> None:
        with pytest.raises(ValueError):
            max_purchase_price(sale_price="100", min_roi=D("-0.1"))
