"""Complete latest-offer matching, scoped aggregation and stable pagination."""

import asyncio
import base64
import hashlib
import json
from collections import defaultdict
from collections.abc import Iterable
from decimal import Decimal
from pathlib import Path
from typing import Any

from common.barcodes import normalize_barcode
from service.db.matching import normalize
from service.db.vocabulary import CatalogueRow, vocabulary_cache

SQL = Path(__file__).with_name("latest.sql").read_text()
MAX_QUERY_LENGTH = 256


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
    scope_data = [
        normalize(q),
        sorted(selection(chains)),
        sorted(literal_selection(city)),
        sorted(literal_selection(brand)),
        sorted(literal_selection(category)),
        order,
        identity,
        sorted((city_labels or {}).items()),
    ]
    scope = hashlib.sha256(json.dumps(scope_data).encode()).hexdigest()
    decoded = decode_cursor(cursor) if cursor else None
    if decoded and decoded["scope"] != scope:
        raise ValueError("Cursor scope or order changed")
    async with database._get_conn() as conn:
        async with conn.transaction(isolation="repeatable_read", readonly=True):
            epoch = await conn.fetchval("SELECT epoch FROM import_state")
            if decoded and decoded["epoch"] != epoch:
                raise CursorExpired
            return await serving_search(
                database,
                conn,
                epoch,
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
                city_labels,
            )


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
                    SQL + " AND p.ean=ANY($1::text[]) AND s.id=ANY($2::bigint[])",
                    list(eans),
                    store_ids,
                )
            ]
            shops = {}
            for row in sorted(rows, key=canonical_offer_order):
                shops.setdefault((row["ean"], row["chain"], row["store_code"]), row)
            return list(shops.values())


async def matched_chain_products(
    database: Any, conn: Any, epoch: int, q: str
) -> tuple[list[int], list[int]] | None:
    if not normalize(q):
        return None

    async def load() -> list[CatalogueRow]:
        rows = await conn.fetch(
            "SELECT id,name,brand,quantity,unit FROM chain_products"
        )
        return [tuple(row) for row in rows]

    index = await vocabulary_cache(database).get(epoch, load)
    return await asyncio.to_thread(index.match, q)


async def serving_search(
    database: Any,
    conn: Any,
    epoch: int,
    scope: str,
    decoded: dict[str, Any] | None,
    q: str,
    chains: str | None,
    city: list[str] | None,
    brand: list[str] | None,
    category: list[str] | None,
    order: str,
    page_size: int,
    identity: str | None,
    city_labels: dict[str, str] | None = None,
) -> dict[str, Any]:
    identity_value = normalize_barcode(identity) or identity if identity else None
    matched = await matched_chain_products(database, conn, epoch, q)
    if matched is None:
        candidates = (
            "SELECT ean,0 rank FROM products WHERE ($1::text IS NULL OR ean=$1)"
        )
        args: list[Any] = [identity_value]
    else:
        candidates = """
        SELECT p.ean,min(m.rank) rank
        FROM unnest($2::bigint[],$3::int[]) m(id,rank)
        JOIN chain_products cp ON cp.id=m.id
        JOIN products p ON p.id=cp.product_id
        WHERE ($1::text IS NULL OR p.ean=$1)
        GROUP BY p.ean"""
        args = [identity_value, *matched]
    candidate_args = len(args)
    accepted = (
        "WITH candidates AS MATERIALIZED ("
        + candidates
        + "), raw AS MATERIALIZED ("
        + SQL
        + " AND p.ean IN (SELECT ean FROM candidates)"
        + """), matching AS (
        SELECT raw.*,((SELECT jsonb_object_agg(ean,rank) FROM candidates)->>raw.ean)::int rank FROM raw
    ) """
    )
    filters = dict(
        chain=selection(chains),
        city=literal_selection(city),
        brand=literal_selection(brand),
        category=literal_selection(category),
    )
    clauses = {}
    for facet, wanted in filters.items():
        values = (
            await conn.fetch(
                accepted + f"SELECT DISTINCT {facet} FROM matching",
                *args[:candidate_args],
            )
            if wanted
            else []
        )
        allowed = sorted(
            {r[facet] for r in values if r[facet] and normalize(r[facet]) in wanted}
        )
        args.append(allowed)
        clauses[facet] = f"{facet}=ANY(${len(args)}::text[])" if wanted else "TRUE"
    accepted = accepted.replace(
        "FROM raw",
        "FROM raw WHERE "
        + " AND ".join(
            f"${i}::text[] IS NOT NULL"
            for i in range(candidate_args + 1, candidate_args + 5)
        ),
    )
    # Every facet excludes only its own selection from the same accepted relation.
    facets = {}
    for facet in filters:
        predicate = " AND ".join(v for k, v in clauses.items() if k != facet)
        value = (
            f"COALESCE((${len(args) + 1}::jsonb)->>city,city)"
            if facet == "city"
            else facet
        )
        facet_args = [*args, json.dumps(city_labels or {})] if facet == "city" else args
        records = await conn.fetch(
            accepted
            + f"SELECT {value} value,count(DISTINCT ean) count FROM matching WHERE {predicate} AND {facet} IS NOT NULL AND {facet}<>'' GROUP BY {value} ORDER BY {value}",
            *facet_args,
        )
        facets[facet] = {r["value"]: r["count"] for r in records}
    outside = await conn.fetchval(
        accepted
        + "SELECT count(DISTINCT ean) FROM matching WHERE "
        + " AND ".join(v for k, v in clauses.items() if k != "city"),
        *args,
    )
    relation = (
        accepted
        + ", eligible AS (SELECT DISTINCT ON(ean,chain,store_code) * FROM matching WHERE "
        + " AND ".join(clauses.values())
        + " ORDER BY ean,chain,store_code,price_date DESC,chain_product_id), priced AS (SELECT *,least(regular_price,special_price) effective FROM eligible) "
    )
    aggregate = """ min(effective) min_price,max(effective) max_price,avg(effective) avg_price,
        count(*) shop_count,count(DISTINCT chain) chain_count,
        min(price_date) min_price_date,max(price_date) max_price_date,max(price_date) price_date,
        avg(effective / NULLIF(quantity::numeric * CASE lower(unit) WHEN 'g' THEN .001 ELSE 1 END,0)) FILTER(WHERE lower(unit) IN ('g','kg') AND quantity ~ '^[0-9]+([.][0-9]+)?$') mass,
        avg(effective / NULLIF(quantity::numeric * CASE lower(unit) WHEN 'ml' THEN .001 ELSE 1 END,0)) FILTER(WHERE lower(unit) IN ('ml','l') AND quantity ~ '^[0-9]+([.][0-9]+)?$') volume,
        avg(effective / NULLIF(quantity::numeric,0)) FILTER(WHERE lower(unit)='kom' AND quantity ~ '^[0-9]+([.][0-9]+)?$') pieces """
    rows = await conn.fetch(
        relation
        + "SELECT ean,min(rank) rank,"
        + aggregate
        + " FROM priced GROUP BY ean",
        *args,
    )
    products = []
    for record in rows:
        row = dict(record)
        price_key = row["min_price"] if order != "relevance" else Decimal(0)
        if order == "price_desc":
            price_key = -price_key
        key = [
            row.pop("rank") // 10,
            record["rank"] % 10,
            str(price_key),
            -row["shop_count"],
            -row["chain_count"],
            row["ean"],
        ]
        products.append((key, row))

    def comparator(key: list[Any]) -> tuple[Any, ...]:
        if (
            len(key) != 6
            or any(type(key[i]) is not int for i in (0, 1, 3, 4))
            or not isinstance(key[2], str)
            or not isinstance(key[5], str)
        ):
            raise ValueError("Invalid cursor comparator")
        try:
            number = Decimal(key[2])
            if not number.is_finite():
                raise ValueError("Invalid cursor price")
        except ArithmeticError as exc:
            raise ValueError("Invalid cursor price") from exc
        return key[0], key[1], number, key[3], key[4], key[5]

    products.sort(key=lambda item: comparator(item[0]))
    total = len(products)
    if decoded:
        boundary = comparator(decoded["key"])
        products = [item for item in products if comparator(item[0]) > boundary]
    page = products[:page_size]
    next_cursor = (
        encode_cursor(epoch, scope, page[-1][0]) if len(products) > len(page) else None
    )
    args.append([r["ean"] for _, r in page])
    chain_rows = await conn.fetch(
        relation
        + "SELECT ean,chain,(array_agg(code ORDER BY code,store_code))[1] code,"
        + ",".join(
            f"(array_agg({k} ORDER BY code,store_code))[1] {k}"
            for k in (
                "name",
                "brand",
                "category",
                "quantity",
                "unit",
                "product_name",
                "product_brand",
                "product_quantity",
                "product_unit",
            )
        )
        + ","
        + aggregate
        + f" FROM priced WHERE ean=ANY(${len(args)}::text[]) GROUP BY ean,chain ORDER BY ean,chain",
        *args,
    )

    def finish(row: dict[str, Any]) -> dict[str, Any]:
        row["unit_prices"] = {
            k: row.pop(k) for k in ("mass", "volume", "pieces") if row[k] is not None
        }
        for k in ("mass", "volume", "pieces"):
            row.pop(k, None)
        row.update(partial=True, listing_status="unknown")
        return row

    by_ean: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in chain_rows:
        by_ean[record["ean"]].append(dict(record))
    for _, product in page:
        members = by_ean[product["ean"]]
        first = members[0]
        for field in ("name", "brand", "quantity", "unit"):
            product[field] = (
                first["product_" + field]
                if first["product_" + field] is not None
                else first[field]
            )
        if product["quantity"] is not None:
            product["quantity"] = str(product["quantity"])
        product["chains"] = [
            finish(
                {
                    k: v
                    for k, v in r.items()
                    if not k.startswith("product_") and k != "ean"
                }
            )
            for r in members
        ]
        finish(product)
    return dict(
        products=[p for _, p in page],
        generation=epoch,
        total=total,
        next_cursor=next_cursor,
        facets=facets,
        outside_location_total=outside,
        complete=True,
        partial=True,
    )


async def chain_statistics(database: Any) -> list[Any]:
    async with database._get_conn() as conn:
        rows = await conn.fetch(
            "WITH offers AS ("
            + SQL
            + "), shops AS (SELECT DISTINCT ON(ean,chain,store_code) * FROM offers ORDER BY ean,chain,store_code,price_date DESC,chain_product_id) SELECT chain,max(price_date) price_date,count(*) price_count,count(DISTINCT store_code) store_count FROM shops GROUP BY chain ORDER BY chain"
        )

    return rows
