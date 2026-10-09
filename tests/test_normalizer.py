"""Normalizador de texto y extractor de entidades (Módulo 2, núcleo)."""

from __future__ import annotations

import pytest

from vinted_arb.normalization import (
    Accessory,
    Condition,
    RedFlag,
    extract,
    normalize_text,
    resolve_condition,
)
from vinted_arb.normalization.condition import condition_from_label
from vinted_arb.normalization.text import (
    extract_battery_health,
    extract_capacities_gb,
    extract_storage_gb,
    storage_label,
)


class TestNormalizeText:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("iPhone 13 Pro 256GB 🔥🔥", "iphone 13 pro 256 gb"),
            ("PS5 Slim — Edición Digital", "ps5 slim edicion digital"),
            ("iphone 13promax", "iphone 13 pro max"),
            ("RTX 3060Ti", "rtx 3060 ti"),
            ("Galaxy S24FE", "galaxy s24 fe"),
            ("iPhone 16e", "iphone 16 e"),
            ("Batería: 87 %", "bateria: 87 %"),
            ("  AirPods   Pro\t2ª gen ", "airpods pro 2a gen"),
        ],
    )
    def test_normalization(self, raw: str, expected: str) -> None:
        assert normalize_text(raw) == expected


class TestQuantities:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("iphone 13 128 gb", 128),
            ("steam deck 512 gb", 512),
            ("ps5 slim 1 tb", 1024),
            ("galaxy s23 8/256", 256),
            ("galaxy s23 12 gb/512 gb", 512),
            ("switch oled + micro sd 256 gb", None),  # la tarjeta no es el almacenamiento
            ("portatil 16 gb ram 512 gb ssd", 512),
            ("steam deck ssd 512 gb", 512),  # "ssd" no es una tarjeta "sd"
            ("ps5 + disco duro 2 tb", None),
            ("iphone 12 5g", None),  # "5g" es la red, no una capacidad
            ("sin capacidad", None),
        ],
    )
    def test_storage(self, text: str, expected: int | None) -> None:
        assert extract_storage_gb(text) == expected

    def test_capacities_keep_non_commercial_values(self) -> None:
        assert extract_capacities_gb("ps5 825 gb") == [825]

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("bateria al 87 %", 87),
            ("salud de bateria 100%", 100),
            ("battery health: 91%", 91),
            ("85% de bateria", 85),
            ("bateria 30%", None),  # fuera de rango plausible de salud
            ("descuento del 20%", None),
        ],
    )
    def test_battery_health(self, text: str, expected: int | None) -> None:
        assert extract_battery_health(text) == expected

    def test_storage_label(self) -> None:
        assert storage_label(256) == "256gb"
        assert storage_label(1024) == "1tb"
        assert storage_label(2048) == "2tb"


class TestCondition:
    @pytest.mark.parametrize(
        ("label", "expected"),
        [
            ("Nuevo con etiquetas", Condition.NEW_SEALED),
            ("Nuevo sin etiquetas", Condition.NEW),
            ("Muy bueno", Condition.VERY_GOOD),
            ("Bueno", Condition.GOOD),
            ("Satisfactorio", Condition.FAIR),
            ("Très bon état", Condition.VERY_GOOD),
            ("Ottime condizioni", Condition.VERY_GOOD),
            ("Sehr gut", Condition.VERY_GOOD),
            ("Very good", Condition.VERY_GOOD),
            (None, Condition.UNKNOWN),
            ("Etiqueta desconocida", Condition.UNKNOWN),
        ],
    )
    def test_vinted_labels(self, label: str | None, expected: Condition) -> None:
        assert condition_from_label(label) is expected

    def test_for_parts_text_overrides_label(self) -> None:
        text = normalize_text("iPhone 12 no enciende, para piezas")
        assert resolve_condition("Muy bueno", text) is Condition.FOR_PARTS

    def test_negated_defect_is_not_for_parts(self) -> None:
        text = normalize_text("Funciona perfecto, no está averiado ni tiene golpes")
        assert resolve_condition("Bueno", text) is Condition.GOOD

    def test_sealed_text_upgrades_new(self) -> None:
        assert resolve_condition("Nuevo sin etiquetas", "ps5 precintada") is Condition.NEW_SEALED

    def test_text_used_when_no_label(self) -> None:
        assert resolve_condition(None, "switch 2 sin abrir") is Condition.NEW_SEALED
        assert resolve_condition(None, "switch 2") is Condition.UNKNOWN


@pytest.mark.parametrize(
    ("title", "expected_sku", "expected_missing"),
    [
        # Consolas
        ("PS5 Slim Digital 1TB como nueva", "console:ps5-slim:digital", ()),
        ("PlayStation 5 CFI-1216A", "console:ps5-original:disc", ()),
        ("PS5 slim sin lector", "console:ps5-slim:digital", ()),
        ("PS5 slim digital + lector", "console:ps5-slim:disc", ()),
        ("PS5 Pro 2TB", "console:ps5-pro", ()),
        ("Consola PS5", "console:ps5", ("generation", "edition")),
        ("Nintendo Switch 2 nueva", "console:switch-2", ()),
        ("Nintendo Switch 2 juegos mario", "console:switch", ("revision",)),
        ("Switch OLED blanca", "console:switch-oled", ()),
        ("Nintendo Switch Lite turquesa", "console:switch-lite", ()),
        ("Xbox Series S 1TB negra", "console:xbox-series-s:1tb", ()),
        ("Steam Deck 256GB", "console:steam-deck-lcd:256gb", ()),
        ("Steam Deck OLED 1TB", "console:steam-deck-oled:1tb", ()),
        # Móviles
        ("iPhone 13 Pro 128GB", "phone:iphone-13-pro:128gb", ()),
        ("iphone 13promax 256", "phone:iphone-13-pro-max:256gb", ()),
        ("iPhone 16e 128 GB", "phone:iphone-16e:128gb", ()),
        ("iPhone 15 e iPad", "phone:iphone-15", ("storage",)),
        ("iPhone SE 2022 64GB", "phone:iphone-se-3:64gb", ()),
        ("Samsung Galaxy S23+ 8/256", "phone:galaxy-s23-plus:256gb", ()),
        ("Galaxy S24 Ultra 512GB", "phone:galaxy-s24-ultra:512gb", ()),
        # GPUs
        ("RTX 3060 12GB Gigabyte", "gpu:rtx-3060:12gb", ()),
        ("Gráfica RTX 4070ti super para pc", "gpu:rtx-4070-ti-super", ()),
        ("Radeon RX 7900 XTX", "gpu:rx-7900-xtx", ()),
        ("RTX 4060 Ti", "gpu:rtx-4060-ti", ("vram",)),
        # Audio
        ("Sony WH-1000XM4 negros", "audio:sony-wh-1000xm4", ()),
        ("Sony WF1000XM5", "audio:sony-wf-1000xm5", ()),
        ("AirPods Pro (2ª generación) USB-C", "audio:airpods-pro-2", ()),
        ("AirPods 4 con cancelación de ruido", "audio:airpods-4:anc", ()),
        ("AirPods Max", "audio:airpods-max", ()),
        # Cámaras y drones
        ("Fujifilm X100V plata", "camera:fujifilm-x100v", ()),
        ("Fuji X100VI", "camera:fujifilm-x100vi", ()),
        ("Sony A7 III cuerpo", "camera:sony-a7-3:body", ()),
        ("Sony ILCE-7M4 kit 28-70", "camera:sony-a7-4:kit", ()),
        ("GoPro Hero 12 Black", "camera:gopro-hero12", ()),
        ("DJI Mini 4 Pro Fly More", "drone:dji-mini-4-pro:fly-more", ()),
    ],
)
def test_product_matching(title: str, expected_sku: str, expected_missing: tuple[str, ...]) -> None:
    entity = extract(title)
    assert entity.sku == expected_sku
    assert entity.product is not None
    assert entity.product.missing == expected_missing


@pytest.mark.parametrize(
    "title",
    [
        "PC Gaming RTX 4070 Ryzen 7",
        "Portátil MSI RTX 4060",
        "Galaxy Tab S9",
        "Switch TP-Link 8 puertos",  # switch de red
        "Camiseta talla S",
    ],
)
def test_non_products_are_not_matched(title: str) -> None:
    assert extract(title).product is None


class TestConfidence:
    def test_missing_variants_lower_confidence(self) -> None:
        assert extract("iPhone 13 Pro 128GB").match_confidence == 0.95
        assert extract("iPhone 13 Pro").match_confidence == 0.80
        assert extract("Consola PS5").match_confidence == pytest.approx(0.65)

    def test_model_only_in_description_is_penalised(self) -> None:
        entity = extract("Consola en perfecto estado", "Es una PS5 slim digital")
        assert entity.sku == "console:ps5-slim:digital"
        assert entity.match_confidence == pytest.approx(0.85)

    def test_storage_from_description(self) -> None:
        entity = extract("iPhone 14 Pro", "Capacidad 256GB, batería 89%")
        assert entity.sku == "phone:iphone-14-pro:256gb"
        assert entity.battery_health == 89


class TestRedFlags:
    @pytest.mark.parametrize(
        ("title", "flag"),
        [
            ("Mando PS5 DualSense blanco", RedFlag.ACCESSORY_ONLY),
            ("Funda iPhone 15 Pro Max", RedFlag.ACCESSORY_ONLY),
            ("Lote de juegos PS5", RedFlag.ACCESSORY_ONLY),
            ("Cargador compatible iPhone 15", RedFlag.ACCESSORY_ONLY),
            ("Caja vacía iPhone 14 Pro", RedFlag.BOX_ONLY),
            ("Auricular derecho AirPods Pro 2", RedFlag.PARTIAL_ITEM),
            ("Busco PS5 slim", RedFlag.WANTED_AD),
            ("iPhone 14 bloqueado por iCloud", RedFlag.LOCKED),
            ("iPhone 12 para piezas", RedFlag.FOR_PARTS),
            ("AirPods Pro réplica 1:1", RedFlag.REPLICA),
            ("Galaxy S22 pantalla rota", RedFlag.SCREEN_DAMAGE),
            ("PS5 slim, escríbeme por WhatsApp", RedFlag.OFF_PLATFORM),
            ("PS5 slim llamar al 612 345 678", RedFlag.OFF_PLATFORM),
            ("Switch OLED solo cambio por PS5", RedFlag.SWAP_ONLY),
            ("PS5 y Nintendo Switch OLED", RedFlag.AMBIGUOUS_BUNDLE),
        ],
    )
    def test_flag_detected(self, title: str, flag: RedFlag) -> None:
        assert flag in extract(title).red_flags

    @pytest.mark.parametrize(
        ("title", "flag"),
        [
            ("Juegos y consola PS5", RedFlag.ACCESSORY_ONLY),
            ("Consola PS5 + 2 mandos", RedFlag.ACCESSORY_ONLY),
            ("iPhone 13 libre de iCloud", RedFlag.LOCKED),
            ("iPhone 13 sin cuenta iCloud", RedFlag.LOCKED),
            ("iPhone 13 iCloud desvinculado", RedFlag.LOCKED),
            ("iPhone 13 desbloqueado", RedFlag.LOCKED),
            ("AirPods Pro 2 originales, no es réplica", RedFlag.REPLICA),
            ("Mando Xbox con pilas AAA", RedFlag.REPLICA),
            ("Galaxy S22 sin pantalla rota ni golpes", RedFlag.SCREEN_DAMAGE),
            ("RTX 3080 con bloque de agua", RedFlag.LOCKED),
        ],
    )
    def test_flag_not_detected(self, title: str, flag: RedFlag) -> None:
        assert flag not in extract(title).red_flags

    def test_low_battery_flag(self) -> None:
        entity = extract("iPhone 12 128GB batería 76%")
        assert entity.battery_health == 76
        assert RedFlag.LOW_BATTERY in entity.red_flags
        assert entity.is_candidate  # bandera blanda: valoración con descuento, no descarte

    def test_hard_flag_disqualifies(self) -> None:
        entity = extract("iPhone 14 128GB bloqueado por iCloud")
        assert entity.hard_flags == frozenset({RedFlag.LOCKED})
        assert not entity.is_candidate


class TestAccessories:
    def test_positive_accessories(self) -> None:
        entity = extract("PS5 slim con caja, factura, 2 mandos y garantía")
        assert entity.accessories >= {
            Accessory.BOX,
            Accessory.INVOICE,
            Accessory.CONTROLLER,
            Accessory.WARRANTY,
        }

    def test_negated_accessories(self) -> None:
        entity = extract("iPhone 13 128GB sin caja ni cargador, no incluye factura")
        assert Accessory.BOX not in entity.accessories
        assert Accessory.CHARGER not in entity.accessories
        assert Accessory.INVOICE not in entity.accessories


def test_full_entity_example() -> None:
    entity = extract(
        "iPhone 13 Pro 256GB Grafito",
        "Batería al 88%. Libre de iCloud. Con caja y factura. Sin golpes.",
        status_label="Muy bueno",
    )
    assert entity.sku == "phone:iphone-13-pro:256gb"
    assert entity.condition is Condition.VERY_GOOD
    assert entity.battery_health == 88
    assert entity.accessories == frozenset({Accessory.BOX, Accessory.INVOICE})
    assert entity.red_flags == frozenset()
    assert entity.is_candidate
