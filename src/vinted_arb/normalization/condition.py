"""Estado canónico del producto a partir de la etiqueta de Vinted y del texto."""

from __future__ import annotations

import re
from enum import StrEnum

from vinted_arb.normalization.text import normalize_text


class Condition(StrEnum):
    NEW_SEALED = "new_sealed"
    NEW = "new"
    VERY_GOOD = "very_good"
    GOOD = "good"
    FAIR = "fair"
    FOR_PARTS = "for_parts"
    UNKNOWN = "unknown"


# Etiquetas localizadas de Vinted (ya normalizadas: minúsculas, sin acentos).
VINTED_STATUS_LABELS: dict[str, Condition] = {
    # es
    "nuevo con etiquetas": Condition.NEW_SEALED,
    "nuevo con embalaje": Condition.NEW_SEALED,
    "nuevo sin etiquetas": Condition.NEW,
    "nuevo sin embalaje": Condition.NEW,
    "nuevo": Condition.NEW,
    "como nuevo": Condition.VERY_GOOD,
    "muy bueno": Condition.VERY_GOOD,
    "bueno": Condition.GOOD,
    "satisfactorio": Condition.FAIR,
    # en
    "new with tags": Condition.NEW_SEALED,
    "new without tags": Condition.NEW,
    "very good": Condition.VERY_GOOD,
    "good": Condition.GOOD,
    "satisfactory": Condition.FAIR,
    # fr
    "neuf avec etiquette": Condition.NEW_SEALED,
    "neuf sans etiquette": Condition.NEW,
    "tres bon etat": Condition.VERY_GOOD,
    "bon etat": Condition.GOOD,
    "satisfaisant": Condition.FAIR,
    # it
    "nuovo con cartellino": Condition.NEW_SEALED,
    "nuovo senza cartellino": Condition.NEW,
    "ottime condizioni": Condition.VERY_GOOD,
    "buone condizioni": Condition.GOOD,
    "discrete condizioni": Condition.FAIR,
    # de
    "neu mit etikett": Condition.NEW_SEALED,
    "neu ohne etikett": Condition.NEW,
    "sehr gut": Condition.VERY_GOOD,
    "gut": Condition.GOOD,
    "zufriedenstellend": Condition.FAIR,
}

# status_ids del filtro de catálogo de Vinted
VINTED_STATUS_IDS: dict[int, Condition] = {
    6: Condition.NEW_SEALED,
    1: Condition.NEW,
    2: Condition.VERY_GOOD,
    3: Condition.GOOD,
    4: Condition.FAIR,
}

_FOR_PARTS_PHRASES = re.compile(
    r"\b("
    r"para (piezas|reparar|despiece|recambios)"
    r"|no (funciona|enciende|carga|arranca|da imagen|lee (discos|juegos))"
    r"|for parts|not working|does ?n.?t work|spares or repair"
    r"|pour pieces|hors service|ne (fonctionne|marche|s.allume) pas|en panne"
    r")\b"
)
# Palabras sueltas: se descartan si van negadas ("no está averiado").
_FOR_PARTS_WORDS = re.compile(r"\b(averiad[oa]s?|estropead[oa]s?|defectuos[oa]s?|broken|hs)\b")
_SEALED = re.compile(
    r"\b(precintad[oa]s?|sin abrir|sin desprecintar|sealed|sous blister|neuf scelle|blister)\b"
)

# "libre de iCloud", "libre de golpes"… también niegan lo que sigue.
_NEGATORS = frozenset(
    {"no", "sin", "ni", "nunca", "nada", "cero", "libre", "libres", "not", "never", "sans", "pas"}
)


def preceded_by_negation(text: str, start: int, window: int = 3) -> bool:
    """¿Alguna de las ``window`` palabras previas a ``start`` es una negación?"""
    preceding = text[:start].split()[-window:]
    return any(token in _NEGATORS for token in preceding)


def condition_from_label(label: str | None) -> Condition:
    if not label:
        return Condition.UNKNOWN
    key = normalize_text(label)
    if key in VINTED_STATUS_LABELS:
        return VINTED_STATUS_LABELS[key]
    # Coincidencia por prefijo, de la más larga a la más corta ("muy bueno" antes que "bueno").
    for known in sorted(VINTED_STATUS_LABELS, key=len, reverse=True):
        if key.startswith(known):
            return VINTED_STATUS_LABELS[known]
    return Condition.UNKNOWN


def is_for_parts(text: str) -> bool:
    """``text`` normalizado. Señales de producto averiado o para piezas."""
    if _FOR_PARTS_PHRASES.search(text):
        return True
    return any(not preceded_by_negation(text, m.start()) for m in _FOR_PARTS_WORDS.finditer(text))


def condition_from_text(text: str) -> Condition | None:
    if is_for_parts(text):
        return Condition.FOR_PARTS
    for match in _SEALED.finditer(text):
        if not preceded_by_negation(text, match.start()):
            return Condition.NEW_SEALED
    return None


def resolve_condition(label: str | None, text: str) -> Condition:
    """Combina la etiqueta declarada con el texto (normalizado).

    * "Para piezas" en el texto manda siempre: es la señal más cara de ignorar.
    * Si hay etiqueta, se respeta (es lo que declara el vendedor), salvo que una etiqueta
      "nuevo" venga acompañada de "precintado" en el texto.
    * Sin etiqueta, se usa lo que diga el texto.
    """
    textual = condition_from_text(text)
    if textual is Condition.FOR_PARTS:
        return Condition.FOR_PARTS
    declared = condition_from_label(label)
    if declared is Condition.NEW and textual is Condition.NEW_SEALED:
        return Condition.NEW_SEALED
    if declared is not Condition.UNKNOWN:
        return declared
    return textual or Condition.UNKNOWN
