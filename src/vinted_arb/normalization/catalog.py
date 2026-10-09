"""Catálogo de reglas: texto normalizado → producto canónico (SKU) con variantes de precio.

Cada *matcher* reconoce una línea de producto y resuelve solo las variantes que mueven el
precio (edición de PS5, capacidad de un iPhone, VRAM de una 3060…). Las variantes que no
se pueden resolver se listan en ``missing`` y bajan la confianza.

Formato de SKU: ``familia:modelo[:variante…]``, p. ej. ``console:ps5-slim:digital`` o
``phone:iphone-15-pro:256gb``.

Este es el nivel 1 (reglas) del matching escalonado de ARCHITECTURE.md §3.3. El objetivo
de M2 es mover el catálogo a YAML y ampliarlo a ~100 SKUs con un dataset etiquetado.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from vinted_arb.normalization.text import (
    extract_capacities_gb,
    extract_storage_gb,
    storage_label,
)

BASE_CONFIDENCE = 0.95
MISSING_VARIANT_PENALTY = 0.15
DESCRIPTION_ONLY_PENALTY = 0.10


class Family(StrEnum):
    CONSOLE = "console"
    PHONE = "phone"
    GPU = "gpu"
    AUDIO = "audio"
    CAMERA = "camera"
    DRONE = "drone"


@dataclass(frozen=True, slots=True)
class ProductMatch:
    family: Family
    brand: str
    model: str
    variant: tuple[tuple[str, str], ...] = ()
    missing: tuple[str, ...] = ()
    confidence: float = BASE_CONFIDENCE
    span_start: int | None = None
    """Posición de la mención del dispositivo en el título (``None`` si vino de la descripción)."""

    @property
    def sku(self) -> str:
        return ":".join([self.family.value, self.model, *(value for _, value in self.variant)])

    @property
    def variant_dict(self) -> dict[str, str]:
        return dict(self.variant)


@dataclass(frozen=True, slots=True)
class MatchContext:
    """Textos normalizados sobre los que trabajan los matchers."""

    title: str
    full: str
    """Título + descripción."""

    def find(self, pattern: re.Pattern[str]) -> re.Match[str] | None:
        """Busca primero en el título y, si no, en el texto completo."""
        return pattern.search(self.title) or pattern.search(self.full)

    def has(self, pattern: re.Pattern[str]) -> bool:
        return self.find(pattern) is not None

    def in_title(self, match: re.Match[str]) -> bool:
        return match.string is self.title

    def storage_gb(self) -> int | None:
        return extract_storage_gb(self.title) or extract_storage_gb(self.full)


Matcher = Callable[[MatchContext], "ProductMatch | None"]


def _build(
    ctx: MatchContext,
    family: Family,
    brand: str,
    model: str,
    *,
    match: re.Match[str],
    variant: dict[str, str | None] | None = None,
    required: tuple[str, ...] = (),
    base: float = BASE_CONFIDENCE,
) -> ProductMatch:
    resolved = {k: v for k, v in (variant or {}).items() if v}
    missing = tuple(k for k in required if k not in resolved)
    in_title = ctx.in_title(match)
    confidence = base - MISSING_VARIANT_PENALTY * len(missing)
    if not in_title:
        confidence -= DESCRIPTION_ONLY_PENALTY
    return ProductMatch(
        family=family,
        brand=brand,
        model=model,
        variant=tuple(resolved.items()),
        missing=missing,
        confidence=round(max(0.0, confidence), 2),
        span_start=match.start() if in_title else None,
    )


# ====================================================================== consolas
_PS5 = re.compile(r"\b(ps ?5|playstation ?5|play station 5|play 5)\b")
_PS5_PRO = re.compile(r"\b(ps ?5|playstation ?5|play 5) pro\b")
_PS5_CFI = re.compile(r"\bcfi-? ?([127])\d{3} ?([ab])?\b")
_PS5_SLIM = re.compile(r"\bslim\b")
_PS5_FAT = re.compile(
    r"\b(fat|primera (generacion|version)|1a generacion|modelo (antiguo|original))\b"
)
_PS5_NO_DRIVE = re.compile(r"\bsin (lector|disco|unidad( de disco)?)\b")
_PS5_DIGITAL = re.compile(r"\b(digital|edicion digital|digital edition)\b")
_PS5_DRIVE_ADDED = re.compile(r"(\+ ?|\bcon |\by )(el )?lector\b|\blector incluido\b")
_PS5_DISC = re.compile(
    r"\b(con lector|lector|con disco|estandar|standard|disc edition|edicion (estandar|disco)"
    r"|blu ?-?ray|fisica)\b"
)


def match_ps5(ctx: MatchContext) -> ProductMatch | None:
    m = ctx.find(_PS5)
    if not m:
        return None
    gen: str | None = None
    edition: str | None = None

    cfi = ctx.find(_PS5_CFI)
    if cfi:
        gen = {"1": "original", "2": "slim", "7": "pro"}[cfi.group(1)]
        if cfi.group(2):
            edition = "disc" if cfi.group(2) == "a" else "digital"

    if gen is None:
        capacities = set(extract_capacities_gb(ctx.title))
        if ctx.has(_PS5_PRO) or 2048 in capacities:
            gen = "pro"
        elif ctx.has(_PS5_SLIM) or 1024 in capacities:
            gen = "slim"
        elif ctx.has(_PS5_FAT) or 825 in capacities:
            gen = "original"

    if gen == "pro":
        # La PS5 Pro es digital de serie; el lector es un accesorio aparte.
        return _build(ctx, Family.CONSOLE, "sony", "ps5-pro", match=m, required=())

    if edition is None:
        if ctx.has(_PS5_NO_DRIVE):
            edition = "digital"
        elif ctx.has(_PS5_DIGITAL):
            edition = "disc" if ctx.has(_PS5_DRIVE_ADDED) else "digital"
        elif ctx.has(_PS5_DISC):
            edition = "disc"

    model = f"ps5-{gen}" if gen else "ps5"
    return _build(
        ctx,
        Family.CONSOLE,
        "sony",
        model,
        match=m,
        variant={"edition": edition},
        required=("generation", "edition") if gen is None else ("edition",),
    )


_SWITCH = re.compile(r"\b(nintendo switch|switch)\b")
_SWITCH_CONTEXT = re.compile(r"\b(nintendo|oled|lite|consola|console|joy ?-?cons?)\b")
_SWITCH_2 = re.compile(r"\bswitch ?2\b(?! ?(juegos?|mandos?|joy|games?|fundas?|x\b|unidades|uds))")
_SWITCH_OLED = re.compile(r"\b(oled|heg-?001)\b")
_SWITCH_LITE = re.compile(r"\b(lite|hdh-?001)\b")
_SWITCH_V2 = re.compile(r"\b(v2|hac-?001 ?-?01|bateria mejorada|caja roja)\b")
_SWITCH_V1 = re.compile(r"\b(v1|hac-?001)\b")


def match_switch(ctx: MatchContext) -> ProductMatch | None:
    m = ctx.find(_SWITCH)
    if not m or not ctx.has(_SWITCH_CONTEXT):
        return None  # "switch" a secas puede ser un switch de red
    if ctx.has(_SWITCH_2):
        model: str | None = "switch-2"
    elif ctx.has(_SWITCH_OLED):
        model = "switch-oled"
    elif ctx.has(_SWITCH_LITE):
        model = "switch-lite"
    elif ctx.has(_SWITCH_V2):
        model = "switch-v2"
    elif ctx.has(_SWITCH_V1):
        model = "switch-v1"
    else:
        model = None
    return _build(
        ctx,
        Family.CONSOLE,
        "nintendo",
        model or "switch",
        match=m,
        required=() if model else ("revision",),
    )


_XBOX = re.compile(r"\bxbox ?series? ?(x|s)\b")
_XBOX_DIGITAL = re.compile(r"\b(digital|all digital)\b")


def match_xbox(ctx: MatchContext) -> ProductMatch | None:
    m = ctx.find(_XBOX)
    if not m:
        return None
    letter = m.group(1)
    storage = ctx.storage_gb()
    if letter == "s":
        edition = "1tb" if storage == 1024 else "512gb"
    elif storage == 2048:
        edition = "galaxy-2tb"
    else:
        edition = "digital" if ctx.has(_XBOX_DIGITAL) else "standard"
    return _build(
        ctx,
        Family.CONSOLE,
        "microsoft",
        f"xbox-series-{letter}",
        match=m,
        variant={"edition": edition},
        base=0.9,
    )


_STEAM_DECK = re.compile(r"\bsteam ?deck\b")
_STEAM_DECK_OLED = re.compile(r"\boled\b")
_STEAM_DECK_LCD = re.compile(r"\blcd\b")


def match_steam_deck(ctx: MatchContext) -> ProductMatch | None:
    m = ctx.find(_STEAM_DECK)
    if not m:
        return None
    storage = ctx.storage_gb()
    screen: str | None = None
    if ctx.has(_STEAM_DECK_OLED):
        screen = "oled"
    elif ctx.has(_STEAM_DECK_LCD):
        screen = "lcd"
    elif storage in (64, 256):
        screen = "lcd"  # la OLED solo existe en 512 GB y 1 TB
    elif storage == 1024:
        screen = "oled"  # la LCD nunca tuvo 1 TB
    return _build(
        ctx,
        Family.CONSOLE,
        "valve",
        f"steam-deck-{screen}" if screen else "steam-deck",
        match=m,
        variant={"storage": storage_label(storage) if storage else None},
        required=("storage",) if screen else ("screen", "storage"),
    )


# ====================================================================== móviles
_IPHONE = re.compile(
    r"\biphone ?(1[1-7]|x[rs]?|se|air)\b(?: ?(e)\b)?(?: ?(pro max|pro|plus|mini|max)\b)?"
)
_IPHONE_SE_GEN = re.compile(r"\bse ?(2020|2022|2|3|2a|3a|segunda|tercera)\b")
_PHONE_STORAGE_AFTER_MODEL = re.compile(r"^ ?(64|128|256|512)\b")


def match_iphone(ctx: MatchContext) -> ProductMatch | None:
    m = ctx.find(_IPHONE)
    if not m:
        return None
    number, e_suffix, tier = m.group(1), m.group(2), m.group(3)
    if e_suffix and number not in ("16", "17"):
        e_suffix = None  # "iphone 15 e ipad": la "e" es una conjunción
    required: list[str] = ["storage"]
    if number == "se":
        se_gen = ctx.find(_IPHONE_SE_GEN)
        gen = None
        if se_gen:
            gen = "3" if se_gen.group(1) in ("2022", "3", "3a", "tercera") else "2"
        model = f"iphone-se-{gen}" if gen else "iphone-se"
        if not gen:
            required.append("generation")
    else:
        model = f"iphone-{number}{e_suffix or ''}"
        if tier:
            model += "-" + tier.replace(" ", "-")

    storage = ctx.storage_gb()
    if storage is None:
        # "iphone 13 pro 256" sin unidad, justo detrás del modelo
        after = _PHONE_STORAGE_AFTER_MODEL.match(m.string[m.end() :])
        if after:
            storage = int(after.group(1))
    return _build(
        ctx,
        Family.PHONE,
        "apple",
        model,
        match=m,
        variant={"storage": storage_label(storage) if storage else None},
        required=tuple(required),
    )


_GALAXY_S = re.compile(r"\b(?:galaxy )?s ?(2[0-6])(?: ?(ultra|plus|fe|edge|\+))?(?![\w+])")
_SAMSUNG_CONTEXT = re.compile(r"\b(samsung|galaxy)\b")
_SAMSUNG_EXCLUDE = re.compile(r"\b(tab|watch|buds|book)\b")


def match_galaxy_s(ctx: MatchContext) -> ProductMatch | None:
    if not ctx.has(_SAMSUNG_CONTEXT) or ctx.has(_SAMSUNG_EXCLUDE):
        return None
    m = ctx.find(_GALAXY_S)
    if not m:
        return None
    tier = m.group(2)
    model = f"galaxy-s{m.group(1)}"
    if tier:
        model += "-plus" if tier in ("plus", "+") else f"-{tier}"
    storage = ctx.storage_gb()
    return _build(
        ctx,
        Family.PHONE,
        "samsung",
        model,
        match=m,
        variant={"storage": storage_label(storage) if storage else None},
        required=("storage",),
    )


# ====================================================================== GPUs
_RTX = re.compile(
    r"\b(?:geforce )?rtx ?(20[5-8]0|30[5-9]0|40[6-9]0|50[5-9]0)(?: ?(ti super|ti|super))?\b"
)
_RX = re.compile(r"\b(?:radeon )?rx ?(6[4-9][05]0|7[6-9]00|90[67]0)(?: ?(xtx|xt|gre))?\b")
# Equipos completos: la GPU es un componente, no el producto.
_FOR_PC = re.compile(r"\b(para|for|pour) (pc|ordenador|sobremesa)\b")
_COMPLETE_SYSTEM = re.compile(
    r"\b(portatil|laptop|notebook|pc|ordenador|torre|sobremesa|mini ?pc|all in one"
    r"|ryzen \d|core i[3579]|i[3579] ?-?\d{4,5}\w?)\b|^equipo\b"
)
_VRAM_OPTIONS: dict[str, tuple[int, ...]] = {
    "rtx-2060": (6, 12),
    "rtx-3060": (8, 12),
    "rtx-3080": (10, 12),
    "rtx-4060-ti": (8, 16),
    "rtx-5060-ti": (8, 16),
    "rx-9060-xt": (8, 16),
}
_VRAM = re.compile(r"\b(4|6|8|10|11|12|16|20|24|32) ?(gb|go)\b")


def _is_complete_system(ctx: MatchContext) -> bool:
    return _COMPLETE_SYSTEM.search(_FOR_PC.sub(" ", ctx.title)) is not None


def _gpu(
    ctx: MatchContext, pattern: re.Pattern[str], prefix: str, brand: str
) -> ProductMatch | None:
    m = ctx.find(pattern)
    if not m or _is_complete_system(ctx):
        return None
    model = f"{prefix}-{m.group(1)}"
    if m.group(2):
        model += "-" + m.group(2).replace(" ", "-")
    options = _VRAM_OPTIONS.get(model)
    vram: str | None = None
    if options:
        for vm in _VRAM.finditer(ctx.title + " " + ctx.full):
            if int(vm.group(1)) in options:
                vram = f"{vm.group(1)}gb"
                break
    return _build(
        ctx,
        Family.GPU,
        brand,
        model,
        match=m,
        variant={"vram": vram},
        required=("vram",) if options else (),
    )


def match_rtx(ctx: MatchContext) -> ProductMatch | None:
    return _gpu(ctx, _RTX, "rtx", "nvidia")


def match_radeon(ctx: MatchContext) -> ProductMatch | None:
    return _gpu(ctx, _RX, "rx", "amd")


# ====================================================================== audio
_SONY_1000X = re.compile(r"\b(wh|wf) ?-? ?1000 ?x?m ?([3-6])\b")
_SONY_XM_SHORT = re.compile(r"\bx ?m ?([3-6])\b")
_SONY_CONTEXT = re.compile(r"\bsony\b")
_IN_EAR = re.compile(r"\b(in ?-?ear|true wireless|tws|boton|botones|earbuds)\b")


def match_sony_1000x(ctx: MatchContext) -> ProductMatch | None:
    m = ctx.find(_SONY_1000X)
    base = BASE_CONFIDENCE
    if m:
        kind, mark = m.group(1), m.group(2)
    else:
        m = ctx.find(_SONY_XM_SHORT) if ctx.has(_SONY_CONTEXT) else None
        if not m:
            return None
        kind, mark, base = ("wf" if ctx.has(_IN_EAR) else "wh"), m.group(1), 0.8
    return _build(ctx, Family.AUDIO, "sony", f"sony-{kind}-1000xm{mark}", match=m, base=base)


_AIRPODS = re.compile(r"\bair ?pods?\b")
_AIRPODS_MAX = re.compile(r"\bair ?pods? max\b")
_AIRPODS_PRO = re.compile(r"\bair ?pods? pro\b")
_USB_C = re.compile(r"\busb ?-?c\b")
_GEN_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "3",
        re.compile(
            r"\bpro ?3\b|\b(3a|3rd|tercera|third) (gen|generacion)\b|\b(gen|generacion) ?3\b"
        ),
    ),
    (
        "2",
        re.compile(
            r"\bpro ?2\b|\b(2a|2nd|segunda|second) (gen|generacion)\b|\b(gen|generacion) ?2\b"
        ),
    ),
    (
        "1",
        re.compile(
            r"\bpro ?1\b|\b(1a|1st|primera|first) (gen|generacion)\b|\b(gen|generacion) ?1\b"
        ),
    ),
)
_AIRPODS_PLAIN_GEN = re.compile(r"\bair ?pods? ?([234])\b|\b([234])a (gen|generacion)\b")
_ANC = re.compile(r"\b(anc|cancelacion (activa )?de ruido|noise cancell?ing)\b")


def match_airpods(ctx: MatchContext) -> ProductMatch | None:
    m = ctx.find(_AIRPODS)
    if not m:
        return None
    if ctx.has(_AIRPODS_MAX):
        connector = "usb-c" if ctx.has(_USB_C) else None
        return _build(
            ctx, Family.AUDIO, "apple", "airpods-max", match=m, variant={"connector": connector}
        )
    if ctx.has(_AIRPODS_PRO):
        gen = next((g for g, pattern in _GEN_PATTERNS if ctx.has(pattern)), None)
        return _build(
            ctx,
            Family.AUDIO,
            "apple",
            f"airpods-pro-{gen}" if gen else "airpods-pro",
            match=m,
            required=() if gen else ("generation",),
        )
    plain = ctx.find(_AIRPODS_PLAIN_GEN)
    gen = (plain.group(1) or plain.group(2)) if plain else None
    variant: dict[str, str | None] = {"anc": "anc" if gen == "4" and ctx.has(_ANC) else None}
    return _build(
        ctx,
        Family.AUDIO,
        "apple",
        f"airpods-{gen}" if gen else "airpods",
        match=m,
        variant=variant,
        required=() if gen else ("generation",),
    )


# ====================================================================== cámaras y drones
_FUJI_CONTEXT = re.compile(r"\bfuji(film)?\b")
_X100 = re.compile(r"\bx ?100 ?(vi|v|f|t|s)?\b")


def match_fujifilm_x100(ctx: MatchContext) -> ProductMatch | None:
    if not ctx.has(_FUJI_CONTEXT):
        return None
    m = ctx.find(_X100)
    if not m:
        return None
    gen = m.group(1)
    return _build(
        ctx,
        Family.CAMERA,
        "fujifilm",
        f"fujifilm-x100{gen}" if gen else "fujifilm-x100",
        match=m,
        required=() if gen else ("generation",),
    )


_SONY_A7 = re.compile(r"\b(?:alpha ?)?a ?7 ?(r|s|c)? ?(v|iv|iii|ii|[2-5])\b")
_SONY_ILCE7 = re.compile(r"\bilce-? ?7 ?(r|s|c)?m ?([2-5])\b")
_SONY_A7_CONTEXT = re.compile(r"\b(sony|alpha|ilce)\b")
_ROMAN = {"ii": "2", "iii": "3", "iv": "4", "v": "5"}
_BODY_ONLY = re.compile(r"\b(solo cuerpo|cuerpo|body only|body|boitier( nu)?)\b")
_WITH_LENS = re.compile(r"\b(kit|con objetivo|\+ ?objetivo|28 ?-? ?70|24 ?-? ?70)\b")


def match_sony_a7(ctx: MatchContext) -> ProductMatch | None:
    if not ctx.has(_SONY_A7_CONTEXT):
        return None
    m = ctx.find(_SONY_ILCE7) or ctx.find(_SONY_A7)
    if not m:
        return None
    letter, mark = m.group(1) or "", _ROMAN.get(m.group(2), m.group(2))
    package = "body" if ctx.has(_BODY_ONLY) else ("kit" if ctx.has(_WITH_LENS) else None)
    return _build(
        ctx,
        Family.CAMERA,
        "sony",
        f"sony-a7{letter}-{mark}",
        match=m,
        variant={"package": package},
    )


_GOPRO = re.compile(r"\bgo ?pro (?:hero ?)?(\d{1,2})\b")


def match_gopro(ctx: MatchContext) -> ProductMatch | None:
    m = ctx.find(_GOPRO)
    if not m or not 5 <= int(m.group(1)) <= 13:
        return None
    return _build(ctx, Family.CAMERA, "gopro", f"gopro-hero{m.group(1)}", match=m)


_DJI_MINI = re.compile(r"\bdji (?:mavic )?mini ?([2-5])(?: ?(pro|se))?\b")
_FLY_MORE = re.compile(r"\bfly ?more\b")


def match_dji_mini(ctx: MatchContext) -> ProductMatch | None:
    m = ctx.find(_DJI_MINI)
    if not m:
        return None
    model = f"dji-mini-{m.group(1)}" + (f"-{m.group(2)}" if m.group(2) else "")
    kit = "fly-more" if ctx.has(_FLY_MORE) else None
    return _build(ctx, Family.DRONE, "dji", model, match=m, variant={"kit": kit})


MATCHERS: tuple[Matcher, ...] = (
    match_ps5,
    match_switch,
    match_xbox,
    match_steam_deck,
    match_iphone,
    match_galaxy_s,
    match_rtx,
    match_radeon,
    match_sony_1000x,
    match_airpods,
    match_fujifilm_x100,
    match_sony_a7,
    match_gopro,
    match_dji_mini,
)


# ====================================================================== accesorios
_LEADING_FILLER = re.compile(
    r"^(?:\d+ ?x |pack (?:de )?|lote (?:de )?|kit (?:de )?|nuev[oa]s? |original(?:es)? "
    r"|oficial(?:es)? |genuin[oa]s? )+"
)
_ACCESSORY_NOUNS: dict[Family, str] = {
    Family.CONSOLE: (
        r"mandos?|dualsense|dualshock|controller|joy ?-?cons?|juegos?|game|funda|carcasa|skin"
        r"|vinilo|pegatinas?|soporte|base( de)? carga|estacion de carga|cargador|cable|volante"
        r"|auriculares|headset|cascos|disco duro|ssd|ventilador|camara|mochila|bolsa|estuche"
        r"|protector|lector|grip|dock|adaptador|tapas?|placa|fuente|steelbook|amiibo|tarjeta"
    ),
    Family.PHONE: (
        r"funda|carcasa|protector|cristal templado|cargador|cable|soporte|correa|lentes?"
        r"|pantalla|bateria|placa|tapa|caja|adaptador|power ?bank|cubre"
    ),
    Family.GPU: (
        r"backplate|soporte|bracket|cable|adaptador|disipador|bloque de agua|waterblock"
        r"|ventiladores?|caja"
    ),
    Family.AUDIO: (
        r"almohadillas|ear ?pads|funda|estuche|cable|cargador|soporte|puntas|tips|caja|carcasa"
    ),
    Family.CAMERA: (
        r"funda|correa|bateria|cargador|parasol|filtro|tapa|grip|adaptador|caja|soporte"
        r"|tripode|bolsa|jaula|cage"
    ),
    Family.DRONE: r"helices|baterias?|cargador|funda|maleta|mando|filtros|protector|caja|bolsa",
}
_DEVICE_NOUNS: dict[Family, str] = {
    Family.CONSOLE: r"consola|console|konsole",
    Family.PHONE: r"movil|telefono|smartphone",
    Family.GPU: r"tarjeta grafica|grafica|gpu",
    Family.AUDIO: r"auriculares|cascos",
    Family.CAMERA: r"camara|cuerpo",
    Family.DRONE: r"drone?",
}
_FOR_DEVICE = re.compile(r"\b(para|compatibles?( con)?|for|pour|per)$")


def looks_like_accessory(title: str, match: ProductMatch) -> bool:
    """¿El título vende un accesorio *del* dispositivo y no el dispositivo?

    * Empieza por un sustantivo de accesorio ("Mando PS5", "Funda iPhone 15") → accesorio,
      salvo que el dispositivo aparezca unido como lote ("Juegos y consola PS5").
    * "para/compatible con" justo antes del dispositivo ("cargador compatible iphone") → accesorio.
    """
    stripped = _LEADING_FILLER.sub("", title)
    if re.match(rf"^({_ACCESSORY_NOUNS[match.family]})\b", stripped):
        device = _DEVICE_NOUNS[match.family]
        bundled = re.search(
            rf"(\by|\+|\bcon|\band|\bet|,) ?(?:la |una |el |un )?({device})\b", stripped
        )
        return bundled is None
    if match.span_start is not None:
        return bool(_FOR_DEVICE.search(title[: match.span_start].rstrip()))
    return False
