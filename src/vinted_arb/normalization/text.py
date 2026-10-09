"""Normalización de texto libre de anuncios y extracción de magnitudes (capacidad, batería)."""

from __future__ import annotations

import re
import unicodedata

# Capacidades de almacenamiento comerciales (GB). La RAM y las tarjetas SD se filtran aparte.
VALID_STORAGE_GB: frozenset[int] = frozenset({16, 32, 64, 128, 256, 512, 1024, 2048, 4096})

_KEEP = re.compile(r"[^a-z0-9\s+./%:-]")
_SPACES = re.compile(r"\s+")
_PROMAX = re.compile(r"pro ?max")
# "256gb" → "256 gb", "120hz" → "120 hz"
_UNIT_GLUE = re.compile(r"(?<=\d)(gb|go|tb|to|mb|mah|hz|mp|mm)\b")
# "13pro" → "13 pro", "3060ti" → "3060 ti", "16e" → "16 e", "s24fe" → "s24 fe"
_SUFFIX_GLUE = re.compile(
    r"(?<=\d)(pro|max|plus|mini|ultra|ti|super|xtx|xt|gre|lite|oled|slim|fe|e)\b"
)

_CAPACITY = re.compile(r"\b(\d{1,4}) ?(gb|go|tb|to)\b")
# "8/256", "8+256 gb", "12gb/512gb" (RAM/almacenamiento en móviles)
_RAM_ROM = re.compile(r"\b(\d{1,2}) ?(?:gb)? ?[/+] ?(\d{2,4}) ?(?:gb|tb)?\b")
# Contexto que indica que una capacidad NO es el almacenamiento principal
_NON_STORAGE_BEFORE = re.compile(
    r"\b(micro ?sd|sd|tarjeta|memoria ram|ram|vram|pendrive|usb|disco duro|externo|hdd)\W*$"
)
_NON_STORAGE_AFTER = re.compile(r"^\W*(ram|de ram|vram|gddr\d?x?|ddr\d)\b")

_BATTERY_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bbateria\D{0,25}?(\d{2,3}) ?%"),
    re.compile(r"\bbattery\D{0,25}?(\d{2,3}) ?%"),
    re.compile(r"\bbatterie\D{0,25}?(\d{2,3}) ?%"),
    re.compile(r"\b(\d{2,3}) ?% (?:de )?(?:salud (?:de )?)?(?:la )?bateria\b"),
    re.compile(r"\bsalud\D{0,15}?(\d{2,3}) ?%"),
)


def strip_accents(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def normalize_text(text: str) -> str:
    """Minúsculas, sin acentos ni emojis, unidades y sufijos de modelo separados."""
    t = strip_accents(text).lower()
    t = t.replace("’", "'").replace("´", "'")
    t = _KEEP.sub(" ", t)
    t = _PROMAX.sub("pro max", t)
    t = _UNIT_GLUE.sub(r" \1", t)
    t = _SUFFIX_GLUE.sub(r" \1", t)
    return _SPACES.sub(" ", t).strip()


def _to_gb(value: int, unit: str) -> int:
    return value * 1024 if unit in ("tb", "to") else value


def extract_capacities_gb(text: str) -> list[int]:
    """Todas las capacidades mencionadas (en GB), en orden de aparición.

    ``text`` debe venir ya normalizado. Descarta las que el contexto marca como RAM,
    VRAM o tarjeta de memoria.
    """
    capacities: list[int] = []
    cursor = 0  # el contexto previo no puede invadir la capacidad anterior ("16 gb ram 512 gb")
    for match in _CAPACITY.finditer(text):
        before = text[max(cursor, match.start() - 14) : match.start()]
        after = text[match.end() : match.end() + 12]
        after_match = _NON_STORAGE_AFTER.search(after)
        cursor = match.end() + (after_match.end() if after_match else 0)
        if _NON_STORAGE_BEFORE.search(before) or after_match:
            continue
        capacities.append(_to_gb(int(match.group(1)), match.group(2)))
    return capacities


def extract_storage_gb(text: str) -> int | None:
    """Almacenamiento principal (GB) o ``None``. Elige el mayor valor comercial plausible."""
    candidates = [c for c in extract_capacities_gb(text) if c in VALID_STORAGE_GB]
    for match in _RAM_ROM.finditer(text):
        rom = int(match.group(2))
        if rom in VALID_STORAGE_GB:
            candidates.append(rom)
    return max(candidates) if candidates else None


def extract_battery_health(text: str) -> int | None:
    """Salud de batería declarada (50-100 %) o ``None``."""
    for pattern in _BATTERY_PATTERNS:
        match = pattern.search(text)
        if match:
            value = int(match.group(1))
            if 50 <= value <= 100:
                return value
    return None


def storage_label(gb: int) -> str:
    """1024 → '1tb', 256 → '256gb'."""
    if gb >= 1024 and gb % 1024 == 0:
        return f"{gb // 1024}tb"
    return f"{gb}gb"
