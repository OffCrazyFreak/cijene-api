"""Complete latest-offer matching, scoped aggregation and stable pagination."""

import asyncio
import base64
import hashlib
import json
from bisect import bisect_right
from collections.abc import Iterable
from decimal import Decimal
from typing import Any

from common.barcodes import normalize_barcode
from service.db.matching import normalize
from service.db.scoped_offers import (
    LATEST_OFFERS,
    Statement,
    ordering_sql,
    page_sql,
)
from service.db.search_cache import Catalogue, Ordering, SortKey, search_cache

MAX_QUERY_LENGTH = 256
PRICE_COLUMNS = """
    p.ean,c.id AS chain_id,c.code AS chain,s.code AS store_code,
    s.city,s.address,s.lat,s.lon,s.type,s.zipcode,s.phone,
    cp.id AS chain_product_id,cp.code,
    o.price_date,o.regular_price,o.special_price,o.unit_price,
    o.best_price_30,o.anchor_price,'unknown' AS listing_status
"""


class CursorExpired(Exception):
    """The data changed since the cursor was issued; the client restarts."""


def selection(value: str | None) -> set[str]:
    return literal_selection((value or "").split(","))


def literal_selection(values: Iterable[str] | None) -> set[str]:
    """Repeated filter values stay whole, because category and brand names contain commas."""
    return {normalize(v) for v in values or [] if normalize(v)}


def decode_cursor(value: str) -> dict[str, Any]:
    try:
        if len(value) > 4096:
            raise ValueError
        result = json.loads(base64.b64decode(value, altchars=b"-_", validate=True))
        if not isinstance(result, dict) or set(result) != {
            "v",
            "epoch",
            "scope",
            "key",
        }:
            raise ValueError
        if (
            result["v"] != 2
            or type(result["epoch"]) is not int
            or not 0 <= result["epoch"] <= 9223372036854775807
        ):
            raise ValueError
        if not isinstance(result["scope"], str) or not isinstance(result["key"], list):
            raise ValueError
        return result
    except (ValueError, TypeError, UnicodeError) as exc:
        raise ValueError("Invalid cursor") from exc


def cursor_boundary(key: list[Any]) -> SortKey:
    if (
        len(key) != 6
        or any(type(key[i]) is not int for i in (0, 1, 3, 4))
        or not isinstance(key[2], str)
        or not isinstance(key[5], str)
    ):
        raise ValueError("Invalid cursor comparator")
    try:
        price = Decimal(key[2])
    except ArithmeticError as exc:
        raise ValueError("Invalid cursor price") from exc
    if not price.is_finite():
        raise ValueError("Invalid cursor price")
    return key[0], key[1], price, key[3], key[4], key[5]


def cursor_key(key: SortKey) -> list[Any]:
    group, quality, price, shops, chains, ean = key
    return [group, quality, str(price), shops, chains, ean]


def encode_cursor(epoch: int, scope: str, key: list[Any]) -> str:
    return base64.urlsafe_b64encode(
        json.dumps(
            {
                "v": 2,
                "epoch": epoch,
                "scope": scope,
                "key": key,
            },
            separators=(",", ":"),
        ).encode()
    ).decode()


async def search(
    database: Any,
    *,
    q: str = "",
    chains: str | None = None,
    city: list[str] | None = None,
    brand: list[str] | None = None,
    category: list[str] | None = None,
    order: str = "relevance",
    page_size: int = 100,
    cursor: str | None = None,
    identity: str | None = None,
    city_labels: dict[str, str] | None = None,
) -> dict[str, Any]:
    if city_labels is not None and (
        not isinstance(city_labels, dict)
        or len(city_labels) > 1000
        or any(
            not isinstance(k, str)
            or not isinstance(v, str)
            or not k.strip()
            or not v.strip()
            or len(k) > 256
            or len(v) > 256
            for k, v in city_labels.items()
        )
    ):
        raise ValueError("Invalid city labels")
    if len(q) > MAX_QUERY_LENGTH:
        raise ValueError("Text search supports at most 256 characters")
    if order not in {"relevance", "price_asc", "price_desc"}:
        raise ValueError("Unsupported order")
    if not 1 <= page_size <= 10000:
        raise ValueError("page_size must be between 1 and 10000")
    if q and len(normalize(q).replace(" ", "")) < 2:
        raise ValueError("Text search requires two visible characters")

    wanted = {
        "chain": selection(chains),
        "city": literal_selection(city),
        "brand": literal_selection(brand),
        "category": literal_selection(category),
    }
    scope_data = [
        normalize(q),
        *(sorted(values) for values in wanted.values()),
        order,
        identity,
        sorted((city_labels or {}).items()),
    ]
    scope = hashlib.sha256(json.dumps(scope_data).encode()).hexdigest()
    decoded = decode_cursor(cursor) if cursor else None
    if decoded and decoded["scope"] != scope:
        raise ValueError("Cursor scope or order changed")
    boundary = cursor_boundary(decoded["key"]) if decoded else None

    async with database._get_conn() as conn:
        async with conn.transaction(isolation="repeatable_read", readonly=True):
            epoch = await conn.fetchval("SELECT epoch FROM import_state")
            if decoded and decoded["epoch"] != epoch:
                raise CursorExpired

            cache = search_cache(database)
            catalogue = (
                await cache.catalogue(epoch, conn)
                if normalize(q) or any(wanted.values())
                else None
            )
            filters = {
                facet: catalogue.resolve(facet, values)
                if catalogue and values
                else None
                for facet, values in wanted.items()
            }

            ordering = cache.ordering(epoch, scope)
            if ordering is None:
                ordering = await scope_ordering(
                    conn,
                    catalogue,
                    q,
                    filters,
                    normalize_barcode(identity) or identity if identity else None,
                    city_labels or {},
                    order,
                )
                cache.remember(epoch, scope, ordering)

            start = bisect_right(ordering.keys, boundary) if boundary else 0
            page = ordering.keys[start : start + page_size]
            products = await page_products(conn, filters, [key[5] for key in page])

    has_more = start + len(page) < len(ordering.keys)
    return dict(
        products=products,
        generation=epoch,
        total=len(ordering.keys),
        next_cursor=encode_cursor(epoch, scope, cursor_key(page[-1]))
        if has_more
        else None,
        facets=ordering.facets,
        outside_location_total=ordering.outside,
        complete=True,
        partial=True,
    )


async def scope_ordering(
    conn: Any,
    catalogue: Catalogue | None,
    q: str,
    filters: dict[str, list[str] | None],
    identity: str | None,
    city_labels: dict[str, str],
    order: str,
) -> Ordering:
    matched = (
        await asyncio.to_thread(catalogue.vocabulary.match, q)
        if catalogue and normalize(q)
        else None
    )
    statement = Statement(filters)
    sql = ordering_sql(statement, identity, matched, city_labels, order)
    row = await conn.fetchrow(sql, *statement.args)

    keys: list[SortKey] = []
    for ean, rank, min_price, shops, chains in zip(
        row["ean"],
        row["rank"],
        row["min_price"],
        row["shop_count"],
        row["chain_count"],
        strict=True,
    ):
        price = {"relevance": Decimal(0), "price_asc": min_price}.get(order, -min_price)
        keys.append((rank // 10, rank % 10, price, -shops, -chains, ean))

    facets = {facet: json.loads(row[facet]) for facet in filters}
    return Ordering(keys, facets, row["outside"])


def unit_prices(row: dict[str, Any]) -> dict[str, Any]:
    prices = {k: row.pop(k) for k in ("mass", "volume", "pieces")}
    return {k: v for k, v in prices.items() if v is not None}


async def page_products(
    conn: Any, filters: dict[str, list[str] | None], eans: list[str]
) -> list[dict[str, Any]]:
    if not eans:
        return []

    statement = Statement(filters)
    sql = page_sql(statement, eans)
    products: dict[str, dict[str, Any]] = {}
    for record in await conn.fetch(sql, *statement.args):
        row = dict(record)
        row["unit_prices"] = unit_prices(row)
        row.update(partial=True, listing_status="unknown")
        if row["chain"] is None:
            products[row["ean"]] = product_summary(row)
        else:
            chain = {k: v for k, v in row.items() if not k.startswith("product_")}
            del chain["ean"]
            products[row["ean"]]["chains"].append(chain)
    return [products[ean] for ean in eans]


def product_summary(row: dict[str, Any]) -> dict[str, Any]:
    product = {
        k: v
        for k, v in row.items()
        if k not in ("chain", "code", "category") and not k.startswith("product_")
    }
    for field in ("name", "brand", "quantity", "unit"):
        own = row["product_" + field]
        product[field] = own if own is not None else row[field]
    if product["quantity"] is not None:
        product["quantity"] = str(product["quantity"])
    product["chains"] = []
    return product


STORE_COLUMNS = "s.id,s.chain_id,s.code,s.type,s.address,s.city,s.zipcode,s.lat,s.lon,s.phone,c.code AS chain"


async def directory(database: Any, kind: str) -> list[dict[str, Any]]:
    if kind not in {"chain", "store"}:
        raise ValueError("Unsupported directory")
    query = (
        "SELECT id,code,created_at FROM chains ORDER BY id"
        if kind == "chain"
        else f"SELECT {STORE_COLUMNS} FROM stores s JOIN chains c ON c.id=s.chain_id ORDER BY s.id"
    )
    async with database._get_conn() as conn:
        return [dict(row) for row in await conn.fetch(query)]


def canonical_offer_order(row: dict[str, Any]) -> tuple[Any, ...]:
    return (
        row["chain"],
        row["store_code"],
        -row["price_date"].toordinal(),
        row["chain_product_id"],
    )


async def latest_prices(
    database: Any, eans: set[str], store_filter: Any
) -> list[dict[str, Any]]:
    async with database._get_conn() as conn:
        async with conn.transaction(isolation="repeatable_read", readonly=True):
            stores = await conn.fetch(
                f"SELECT {STORE_COLUMNS} FROM stores s JOIN chains c ON c.id=s.chain_id"
            )
            store_ids = [r["id"] for r in store_filter([dict(r) for r in stores])]
            rows = [
                dict(row)
                for row in await conn.fetch(
                    f"SELECT {PRICE_COLUMNS} {LATEST_OFFERS} "
                    "WHERE p.ean=ANY($1::text[]) AND s.id=ANY($2::bigint[])",
                    list(eans),
                    store_ids,
                )
            ]
            shops = {}
            for row in sorted(rows, key=canonical_offer_order):
                shops.setdefault((row["ean"], row["chain"], row["store_code"]), row)
            return list(shops.values())


async def chain_statistics(database: Any) -> list[Any]:
    async with database._get_conn() as conn:
        return await conn.fetch(
            f"""
            WITH shops AS (
                SELECT DISTINCT ON (p.ean,c.code,s.code)
                    c.code AS chain,s.code AS store_code,o.price_date
                {LATEST_OFFERS}
                ORDER BY p.ean,c.code,s.code,o.price_date DESC,cp.id
            )
            SELECT chain,max(price_date) price_date,count(*) price_count,
                   count(DISTINCT store_code) store_count
            FROM shops GROUP BY chain ORDER BY chain
            """
        )
