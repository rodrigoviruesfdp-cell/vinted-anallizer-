"""Accesorios incluidos y señales de alarma (red flags) en el texto de un anuncio.

Todas las funciones esperan texto ya normalizado (:func:`normalize_text`). Las señales
que suelen aparecer negadas ("sin iCloud", "libre de iCloud", "no tiene golpes") se
evalúan con una ventana de negación antes y, cuando aplica, después del término.
"""

from __future__ import annotations

import re
from enum import StrEnum

from vinted_arb.normalization.condition import is_for_parts, preceded_by_negation


class Accessory(StrEnum):
    BOX = "box"
    CHARGER = "charger"
    INVOICE = "invoice"
    WARRANTY = "warranty"
    CONTROLLER = "controller"
    GAMES = "games"
    CASE = "case"


class RedFlag(StrEnum):
    FOR_PARTS = "for_parts"
    LOCKED = "locked"
    BOX_ONLY = "box_only"
    REPLICA = "replica"
    ACCESSORY_ONLY = "accessory_only"
    PARTIAL_ITEM = "partial_item"
    WANTED_AD = "wanted_ad"
    SCREEN_DAMAGE = "screen_damage"
    SWAP_ONLY = "swap_only"
    OFF_PLATFORM = "off_platform"
    LOW_BATTERY = "low_battery"
    AMBIGUOUS_BUNDLE = "ambiguous_bundle"


# Señales que descartan la oportunidad directamente (ARCHITECTURE.md §3.3).
HARD_FLAGS: frozenset[RedFlag] = frozenset(
    {
        RedFlag.FOR_PARTS,
        RedFlag.LOCKED,
        RedFlag.BOX_ONLY,
        RedFlag.REPLICA,
        RedFlag.ACCESSORY_ONLY,
        RedFlag.PARTIAL_ITEM,
        RedFlag.WANTED_AD,
    }
)

LOW_BATTERY_THRESHOLD = 80

_ACCESSORY_PATTERNS: dict[Accessory, re.Pattern[str]] = {
    Accessory.BOX: re.compile(r"\b(caja|embalaje|packaging|box|boite|carton)\b"),
    Accessory.CHARGER: re.compile(r"\b(cargador|charger|chargeur|cable de carga)\b"),
    Accessory.INVOICE: re.compile(r"\b(factura|ticket( de compra)?|invoice|receipt|facture)\b"),
    Accessory.WARRANTY: re.compile(r"\b(garantia|warranty|applecare|garantie)\b"),
    Accessory.CONTROLLER: re.compile(
        r"\b(mandos?|dualsense|joy ?-?cons?|controllers?|manettes?)\b"
    ),
    Accessory.GAMES: re.compile(r"\b(juegos?|games?|jeux)\b"),
    Accessory.CASE: re.compile(r"\b(funda|housse|carcasa)\b"),
}

# Palabras tras el término que lo niegan: "icloud libre", "cuenta eliminada"…
_POST_NEGATORS = frozenset(
    {
        "libre", "libres", "desvinculado", "desvinculada", "desactivado", "desactivada",
        "eliminado", "eliminada", "quitado", "quitada", "fuera", "off", "limpio", "limpia",
        "free", "clean", "removed",
    }
)  # fmt: skip

_LOCKED = re.compile(
    r"\b(icloud|bloquead[oa]s?|locked|frp|cuenta (de )?google|lista negra|blacklist(ed)?"
    r"|imei (bloqueado|reportado|baneado)|reportad[oa]|bloque (icloud|operateur|sim|reseau))\b"
)
_BOX_ONLY = re.compile(
    r"\b((solo|solamente|unicamente) (la |el )?(caja|embalaje)|caja vacia|empty box|box only"
    r"|boite (vide|seule)|caja sin (consola|movil|telefono|producto|el (movil|telefono)))\b"
)
_REPLICA = re.compile(r"\b(replica|imitacion|clon|fals[oa](?! contacto)|fake|copia|1:1)\b")
_SCREEN_DAMAGE = re.compile(
    r"\b(pantalla (rota|rajada|agrietada|quemada|danada|con (golpes?|grietas?|rajas?|marcas?))"
    r"|cristal (roto|rajado|agrietado)|(cracked|broken) screen|screen (cracked|broken)"
    r"|ecran (casse|fissure)|pixel(es|s)? muertos?|dead pixels?|burn ?-?in)\b"
)
_PARTIAL = re.compile(
    r"\b((auricular|airpod|casco) (derecho|izquierdo)"
    r"|(solo|solamente|unicamente) (el |la |un |una )?(estuche|caja de carga|auricular|airpod)"
    r"|(left|right) (airpod|earbud)|(charging )?case only)\b"
)
_WANTED_START = re.compile(r"^(busco|compro|se busca|wtb|recherche|cherche)\b")
_WANTED_ANY = re.compile(r"\bbusco\b")
_SWAP = re.compile(r"\b(solo cambio|solo intercambio|cambio por|intercambio por|swap|echange)\b")
_OFF_PLATFORM = re.compile(
    r"\b(whats ?app|wasap|wsp|telegram|bizum|wa\.me|paypal amigos|transferencia)\b"
    r"|\b[67](?:[ .-]?\d){8}\b"
)


def _negated(text: str, match: re.Match[str], *, check_after: bool = False) -> bool:
    if preceded_by_negation(text, match.start()):
        return True
    if check_after:
        following = text[match.end() :].split()[:2]
        return any(token in _POST_NEGATORS for token in following)
    return False


def _any_affirmed(pattern: re.Pattern[str], text: str, *, check_after: bool = False) -> bool:
    return any(not _negated(text, m, check_after=check_after) for m in pattern.finditer(text))


def extract_accessories(text: str) -> frozenset[Accessory]:
    found = {
        accessory
        for accessory, pattern in _ACCESSORY_PATTERNS.items()
        if _any_affirmed(pattern, text)
    }
    return frozenset(found)


def detect_red_flags(
    title: str,
    full_text: str,
    *,
    battery_health: int | None = None,
) -> frozenset[RedFlag]:
    """``title`` y ``full_text`` (título + descripción) normalizados."""
    flags: set[RedFlag] = set()
    if is_for_parts(full_text):
        flags.add(RedFlag.FOR_PARTS)
    if _any_affirmed(_LOCKED, full_text, check_after=True):
        flags.add(RedFlag.LOCKED)
    if _BOX_ONLY.search(full_text):
        flags.add(RedFlag.BOX_ONLY)
    if _any_affirmed(_REPLICA, full_text):
        flags.add(RedFlag.REPLICA)
    if _any_affirmed(_SCREEN_DAMAGE, full_text):
        flags.add(RedFlag.SCREEN_DAMAGE)
    if _PARTIAL.search(title):
        flags.add(RedFlag.PARTIAL_ITEM)
    if _WANTED_START.search(title) or _WANTED_ANY.search(title):
        flags.add(RedFlag.WANTED_AD)
    if _SWAP.search(full_text):
        flags.add(RedFlag.SWAP_ONLY)
    if _OFF_PLATFORM.search(full_text):
        flags.add(RedFlag.OFF_PLATFORM)
    if battery_health is not None and battery_health < LOW_BATTERY_THRESHOLD:
        flags.add(RedFlag.LOW_BATTERY)
    return frozenset(flags)
