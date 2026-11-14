"""Complete latest-offer matching, scoped aggregation and stable pagination."""

import base64
import hashlib
import json
import re
import unicodedata
from collections import defaultdict
from decimal import Decimal
from pathlib import Path
from typing import Any

from common.barcodes import normalize_barcode

SQL = Path(__file__).with_name("latest.sql").read_text()
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


def token_distance(left: str, right: str) -> int:
    if len(left) == len(right):
        mismatches = [i for i, (a, b) in enumerate(zip(left, right)) if a != b]
        if len(mismatches) == 2:
            a, b = mismatches
            if b == a + 1 and left[a] == right[b] and left[b] == right[a]:
                return 1
    previous = list(range(len(right) + 1))
    for i, a in enumerate(left, 1):
        current = [i]
        for j, b in enumerate(right, 1):
            current.append(
                min(current[-1] + 1, previous[j] + 1, previous[j - 1] + (a != b))
            )
        previous = current
    return previous[-1]


def relevance(query: str, name: str, brand: str) -> tuple[int, int] | None:
    tokens = normalize(query).split()
    words = normalize(name + " " + brand).split()
    quality = 0
    for token in tokens:
        if token in words:
            continue
        if token in STRICT or any(c.isdigit() for c in token):
            return None
        if len(token) >= 3 and any(word.startswith(token) for word in words):
            quality = max(quality, 1)
        elif len(token) >= 4 and any(
            token_distance(token, word) <= (2 if len(token) >= 8 else 1)
            for word in words
        ):
            quality = max(quality, 2)
        else:
            return None
    modifiers = set(words) & INTENT_MODIFIERS
    penalty = int(bool(modifiers - set(tokens))) if tokens else 0
    return quality, penalty


def selection(value: str | None) -> set[str]:
    return {normalize(v) for v in (value or "").split(",") if normalize(v)}


def decode_cursor(value: str) -> dict[str, Any]:
    try:
        if len(value) > 4096:
            raise ValueError
        result = json.loads(base64.b64decode(value, altchars=b"-_", validate=True))
        if not isinstance(result, dict) or set(result) != {
            "v",
            "generation",
            "scope",
            "key",
        }:
            raise ValueError
        if (
            result["v"] != 1
            or type(result["generation"]) is not int
            or not 1 <= result["generation"] <= 9223372036854775807
        ):
            raise ValueError
        if not isinstance(result["scope"], str) or not isinstance(result["key"], list):
            raise ValueError
        return result
    except (ValueError, TypeError, UnicodeError) as exc:
        raise ValueError("Invalid cursor") from exc


def encode_cursor(generation: int, scope: str, key: list[Any]) -> str:
    return base64.urlsafe_b64encode(
        json.dumps(
            {
                "v": 1,
                "generation": generation,
                "scope": scope,
                "key": key,
            },
            separators=(",", ":"),
        ).encode()
    ).decode()


def price(row: dict[str, Any]) -> Decimal:
    values = [row[k] for k in ("regular_price", "special_price") if row[k] is not None]
    return min(values)


def summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    prices = [price(r) for r in rows]
    dates = [r["price_date"] for r in rows]
    units: dict[str, list[Decimal]] = defaultdict(list)
    conversions = {
        "g": ("mass", Decimal(".001")),
        "kg": ("mass", Decimal(1)),
        "ml": ("volume", Decimal(".001")),
        "l": ("volume", Decimal(1)),
        "kom": ("pieces", Decimal(1)),
    }
    for row in rows:
        try:
            group, factor = conversions.get(
                normalize(row["unit"] or ""), ("unknown", Decimal(0))
            )
            quantity = Decimal(row["quantity"] or "0") * factor
            if quantity > 0:
                units[group].append(price(row) / quantity)
        except ArithmeticError:
            pass
    return {
        "min_price": min(prices),
        "max_price": max(prices),
        "avg_price": sum(prices) / len(prices),
        "shop_count": len(rows),
        "chain_count": len({r["chain"] for r in rows}),
        "min_price_date": min(dates),
        "max_price_date": max(dates),
        "price_date": max(dates),
        "unit_prices": {k: sum(v) / len(v) for k, v in units.items()},
        "partial": True,
        "listing_status": "unknown",
    }


async def search(
    database: Any,
    *,
    q: str = "",
    chains: str | None = None,
    city: str | None = None,
    brand: str | None = None,
    category: str | None = None,
    order: str = "relevance",
    page_size: int = 100,
    cursor: str | None = None,
    identity: str | None = None,
) -> dict[str, Any]:
    if order not in {"relevance", "price_asc", "price_desc"}:
        raise ValueError("Unsupported order")
    if not 1 <= page_size <= 10000:
        raise ValueError("page_size must be between 1 and 10000")
    if q and len(normalize(q).replace(" ", "")) < 2:
        raise ValueError("Text search requires two visible characters")
    scope_data = [
        normalize(q),
        sorted(selection(chains)),
        sorted(selection(city)),
        sorted(selection(brand)),
        sorted(selection(category)),
        order,
        identity,
    ]
    scope = hashlib.sha256(json.dumps(scope_data).encode()).hexdigest()
    decoded = decode_cursor(cursor) if cursor else None
    if decoded and decoded["scope"] != scope:
        raise ValueError("Cursor scope or order changed")
    async with database._get_conn() as conn:
        async with conn.transaction(isolation="repeatable_read", readonly=True):
            generation = (
                decoded["generation"]
                if decoded
                else await conn.fetchval(
                    "SELECT active_id FROM publication_state WHERE singleton"
                )
            )
            if generation is None:
                rows = []
            else:
                if not await conn.fetchval(
                    "SELECT EXISTS(SELECT 1 FROM generations WHERE id=$1 AND accepted_at IS NOT NULL)",
                    generation,
                ):
                    raise ValueError("Generation unavailable; restart search")
                rows = [dict(r) for r in await conn.fetch(SQL, generation)]
    return evaluate(
        rows,
        generation,
        scope,
        decoded,
        q,
        chains,
        city,
        brand,
        category,
        order,
        page_size,
        identity,
    )


def evaluate(
    rows: list[dict[str, Any]],
    generation: int | None,
    scope: str,
    decoded: dict[str, Any] | None,
    q: str,
    chains: str | None,
    city: str | None,
    brand: str | None,
    category: str | None,
    order: str,
    page_size: int,
    identity: str | None,
) -> dict[str, Any]:
    all_offers: dict[str, list[dict[str, Any]]] = defaultdict(list)
    qualities = {}
    for row in rows:
        ean = row["ean"]
        if identity is not None and ean != (normalize_barcode(identity) or identity):
            continue
        all_offers[ean].append(row)
        quality = relevance(q, row["name"], row["brand"] or "")
        if quality is not None:
            qualities[ean] = min(qualities.get(ean, quality), quality)
    accepted = {ean: all_offers[ean] for ean in qualities}
    filters = {
        "chain": selection(chains),
        "city": selection(city),
        "brand": selection(brand),
        "category": selection(category),
    }

    def eligible(row: dict[str, Any], exclude: str | None = None) -> bool:
        return all(
            not values or facet == exclude or normalize(row[facet] or "") in values
            for facet, values in filters.items()
        )

    facets: dict[str, dict[str, int]] = {}
    for facet in filters:
        counts: dict[str, set[str]] = defaultdict(set)
        for ean, offers in accepted.items():
            for row in offers:
                if eligible(row, facet) and row[facet]:
                    counts[row[facet]].add(ean)
        facets[facet] = {k: len(v) for k, v in sorted(counts.items())}
    outside = sum(
        any(eligible(r, "city") for r in offers) for offers in accepted.values()
    )
    products = []
    for ean, offers in accepted.items():
        shops: dict[tuple[str, str], dict[str, Any]] = {}
        for row in offers:
            if not eligible(row):
                continue
            key = row["chain"], row["store_code"]
            previous = shops.get(key)
            if previous and any(
                previous[k] != row[k]
                for k in (
                    "regular_price",
                    "special_price",
                    "price_date",
                    "unit",
                    "quantity",
                )
            ):
                raise ValueError("Conflicting canonical offers for one physical shop")
            if previous and previous["code"] < row["code"]:
                continue
            shops[key] = row
        if not shops:
            continue
        scoped = sorted(
            shops.values(),
            key=lambda row: (row["chain"], row["code"], row["store_code"]),
        )
        first = scoped[0]
        chain_rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in scoped:
            chain_rows[row["chain"]].append(row)
        chain_products = []
        for chain, members in sorted(chain_rows.items()):
            metadata = members[0]
            chain_products.append(
                {
                    **{
                        k: metadata[k]
                        for k in (
                            "code",
                            "name",
                            "brand",
                            "category",
                            "quantity",
                            "unit",
                        )
                    },
                    "chain": chain,
                    **summary(members),
                }
            )
        product: dict[str, Any] = {
            "ean": ean,
            "name": first["product_name"] or first["name"],
            "brand": first["product_brand"] or first["brand"],
            "quantity": str(first["product_quantity"])
            if first["product_quantity"] is not None
            else first["quantity"],
            "unit": first["product_unit"] or first["unit"],
            "chains": chain_products,
            **summary(scoped),
        }
        quality = qualities[ean]
        price_key = product["avg_price"] if order != "relevance" else Decimal(0)
        if order == "price_desc":
            price_key = -price_key
        key = [
            *quality,
            str(price_key),
            -product["shop_count"],
            -product["chain_count"],
            ean,
        ]
        products.append((key, product))

    def comparator(key: list[Any]) -> tuple[Any, ...]:
        if (
            len(key) != 6
            or any(type(key[i]) is not int for i in (0, 1, 3, 4))
            or not isinstance(key[2], str)
            or not isinstance(key[5], str)
        ):
            raise ValueError("Invalid cursor comparator")
        try:
            numeric = Decimal(key[2])
            if not numeric.is_finite():
                raise ValueError("Invalid cursor price")
        except ArithmeticError as exc:
            raise ValueError("Invalid cursor price") from exc
        return key[0], key[1], numeric, key[3], key[4], key[5]

    products.sort(key=lambda item: comparator(item[0]))
    total = len(products)
    if decoded:
        boundary = comparator(decoded["key"])
        products = [item for item in products if comparator(item[0]) > boundary]
    page = products[:page_size]
    next_cursor = (
        encode_cursor(generation, scope, page[-1][0])
        if generation and len(products) > len(page)
        else None
    )
    return {
        "products": [p for _, p in page],
        "generation": generation,
        "total": total,
        "next_cursor": next_cursor,
        "facets": facets,
        "outside_location_total": outside,
        "complete": True,
        "partial": True,
    }


async def directory(database: Any, kind: str) -> list[dict[str, Any]]:
    if kind not in {"chain", "store"}:
        raise ValueError("Unsupported directory")
    async with database._get_conn() as conn:
        return [
            dict(row)
            for row in await conn.fetch(
                f"SELECT v.*{',c.code AS chain' if kind == 'store' else ''} FROM publication_state ps JOIN generation_{kind}s m ON m.generation_id=ps.active_id JOIN {kind}_versions v USING(version_id) "
                + (
                    "JOIN generation_chains cm ON cm.generation_id=m.generation_id AND cm.id=v.chain_id JOIN chain_versions c ON c.version_id=cm.version_id "
                    if kind == "store"
                    else ""
                )
                + "ORDER BY v.id"
            )
        ]


async def latest_prices(database: Any) -> list[dict[str, Any]]:
    async with database._get_conn() as conn:
        async with conn.transaction(isolation="repeatable_read", readonly=True):
            generation = await conn.fetchval(
                "SELECT active_id FROM publication_state WHERE singleton"
            )
            rows = (
                [dict(row) for row in await conn.fetch(SQL, generation)]
                if generation
                else []
            )
            shops = {}
            for row in sorted(
                rows, key=lambda row: (row["chain"], row["code"], row["store_code"])
            ):
                shops.setdefault((row["ean"], row["chain"], row["store_code"]), row)
            return list(shops.values())
