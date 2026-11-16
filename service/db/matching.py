"""Accent-insensitive token matching shared by search and its vocabulary index."""

import re
import unicodedata
from collections.abc import Sequence
from decimal import Decimal

INTENT_MODIFIERS = {
    "grickalica",
    "umak",
    "ketchup",
    "kiseli",
    "mljevena",
    "pivo",
    "radler",
    "sok",
    "caj",
    "cokolada",
    "bombon",
    "bomboni",
    "cips",
    "keks",
    "keksi",
    "sladoled",
    "deterdzent",
    "sapun",
    "sampon",
    "krema",
    "jogurt",
    "napitak",
    "sirup",
    "aroma",
}
STRICT = {"bez", "bio", "vegan", "light", "zero", "integralno", "laktoze", "glutena"}
PACK_UNITS = {
    "ml": ("l", Decimal(".001")),
    "l": ("l", Decimal(1)),
    "g": ("kg", Decimal(".001")),
    "kg": ("kg", Decimal(1)),
}
PREFIX_MIN_LENGTH = 3
EDIT_MIN_LENGTH = 4


def normalize(value: str) -> str:
    value = value.lower().replace("đ", "d").replace(",", ".")
    return " ".join(
        re.findall(
            r"[0-9]+(?:[.,][0-9]+)?|[a-z]+",
            "".join(
                c
                for c in unicodedata.normalize("NFKD", value)
                if not unicodedata.combining(c)
            ),
        )
    )


def token_distance(left: str, right: str, max_distance: int | None = None) -> int:
    if max_distance is not None and abs(len(left) - len(right)) > max_distance:
        return max_distance + 1
    ceiling = (
        max_distance + 1 if max_distance is not None else len(left) + len(right) + 1
    )
    previous = list(range(len(right) + 1))
    before_previous = None
    for i, a in enumerate(left, 1):
        current = [ceiling] * (len(right) + 1)
        current[0] = i
        start = max(1, i - max_distance) if max_distance is not None else 1
        end = (
            min(len(right), i + max_distance)
            if max_distance is not None
            else len(right)
        )
        for j in range(start, end + 1):
            current[j] = min(
                current[j - 1] + 1,
                previous[j] + 1,
                previous[j - 1] + (a != right[j - 1]),
            )
            if (
                before_previous is not None
                and j > 1
                and a == right[j - 2]
                and left[i - 2] == right[j - 1]
            ):
                current[j] = min(current[j], before_previous[j - 2] + 1)
        if max_distance is not None and min(current) > max_distance:
            return max_distance + 1
        before_previous, previous = previous, current
    return previous[-1]


def edit_budget(token: str) -> int:
    return 2 if len(token) >= 8 else 1


PackSize = tuple[str, Decimal]


def pack_size(amount: str, unit: str) -> PackSize | None:
    if unit not in PACK_UNITS:
        return None
    try:
        value = Decimal(amount)
    except ArithmeticError:
        return None
    if not value.is_finite():
        return None
    target_unit, factor = PACK_UNITS[unit]
    return target_unit, value * factor


def pack_pairs(tokens: list[str]) -> list[int]:
    """Indexes of number and unit token pairs that must match a pack size."""
    return [
        index
        for index in range(len(tokens) - 1)
        if pack_size(tokens[index], tokens[index + 1]) is not None
    ]


def pack_sizes(
    words: Sequence[str], quantity: str | None, unit: str | None
) -> list[PackSize]:
    """Pack sizes written in the name plus the structured quantity, if any."""
    sizes = [
        size
        for i in range(len(words) - 1)
        if (size := pack_size(words[i], words[i + 1])) is not None
    ]
    if quantity is not None and unit is not None:
        structured = pack_size(quantity, unit)
        if structured is not None:
            sizes.append(structured)
    return sizes


def relevance(
    query: str,
    name: str,
    brand: str,
    quantity: str | None = None,
    unit: str | None = None,
    known_brands: set[str] | None = None,
) -> tuple[int, int] | None:
    return score_words(
        normalize(query).split(),
        normalize(name + " " + brand).split(),
        normalize(brand).split(),
        quantity,
        unit,
        known_brands,
    )


def score_words(
    tokens: list[str],
    words: Sequence[str],
    brand_words: Sequence[str],
    quantity: str | None = None,
    unit: str | None = None,
    known_brands: set[str] | None = None,
) -> tuple[int, int] | None:
    """relevance() over already normalized query tokens and name and brand words."""
    words = list(words)
    requested_brands = (known_brands or set()) & set(tokens)
    if requested_brands and not requested_brands <= set(brand_words):
        return None
    pairs = pack_pairs(tokens)
    packs = pack_sizes(words, quantity, unit) if pairs else []
    for index in pairs:
        if pack_size(tokens[index], tokens[index + 1]) not in packs:
            return None
        words.extend(tokens[index : index + 2])
    substitution_words = [word for word in words if word not in brand_words]
    quality = 0
    for token in tokens:
        if token in words:
            continue
        if token in STRICT or any(c.isdigit() for c in token):
            return None
        budget = edit_budget(token)
        if len(token) >= PREFIX_MIN_LENGTH and any(
            word.startswith(token) for word in substitution_words
        ):
            quality = max(quality, 1)
        elif len(token) >= EDIT_MIN_LENGTH and any(
            abs(len(token) - len(word)) <= budget
            and token_distance(token, word, budget) <= budget
            for word in substitution_words
        ):
            quality = max(quality, 2)
        else:
            return None
    modifiers = set(words) & INTENT_MODIFIERS
    penalty = int(bool(modifiers - set(tokens))) if tokens else 0
    return penalty, quality
