"""Complete latest-offer matching, scoped aggregation and stable pagination."""

import base64
import hashlib
import json
from collections import defaultdict
from decimal import Decimal
from pathlib import Path
from typing import Any

from common.barcodes import normalize_barcode
from service.db.matching import normalize, relevance

SQL = Path(__file__).with_name("latest.sql").read_text()
MAX_QUERY_LENGTH = 256


class CursorExpired(Exception):
    """The data changed since the cursor was issued; the client restarts."""


def selection(value: str | None) -> set[str]:
    return {normalize(v) for v in (value or "").split(",") if normalize(v)}


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
        sorted(selection(city)),
        sorted(selection(brand)),
        sorted(selection(category)),
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


async def serving_search(
    conn: Any,
    epoch: int,
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
    city_labels: dict[str, str] | None = None,
) -> dict[str, Any]:
    metadata = await conn.fetch(
        "SELECT DISTINCT name,brand,quantity,unit FROM chain_products"
    )
    known_brands = {
        token for row in metadata for token in normalize(row["brand"] or "").split()
    }
    scores = []
    for row in metadata:
        score = relevance(
            q,
            row["name"],
            row["brand"] or "",
            row["quantity"],
            row["unit"],
            known_brands,
        )
        if score is not None:
            scores.append(
                [row["name"], row["brand"], row["quantity"], row["unit"], *score]
            )
    accepted = (
        """WITH variants AS (
        SELECT x->>0 name,x->>1 brand,x->>2 quantity,x->>3 unit,
               (x->>4)::int intent,(x->>5)::int quality
        FROM jsonb_array_elements($1::jsonb) x
    ), candidates AS MATERIALIZED (
        SELECT p.ean,min(v.intent*10+v.quality) rank
        FROM chain_products cp
        JOIN variants v ON cp.name=v.name AND cp.brand IS NOT DISTINCT FROM v.brand
          AND cp.quantity IS NOT DISTINCT FROM v.quantity AND cp.unit IS NOT DISTINCT FROM v.unit
        JOIN products p ON p.id=cp.product_id
        WHERE ($2::text IS NULL OR p.ean=$2)
        GROUP BY p.ean
    ), raw AS MATERIALIZED ("""
        + SQL
        + " AND p.ean IN (SELECT ean FROM candidates)"
        + """), matching AS (
        SELECT raw.*,((SELECT jsonb_object_agg(ean,rank) FROM candidates)->>raw.ean)::int rank FROM raw
    ) """
    )
    args: list[Any] = [
        json.dumps(scores, default=str),
        normalize_barcode(identity) or identity if identity else None,
    ]
    filters = dict(
        chain=selection(chains),
        city=selection(city),
        brand=selection(brand),
        category=selection(category),
    )
    clauses = {}
    for facet, wanted in filters.items():
        values = (
            await conn.fetch(
                accepted + f"SELECT DISTINCT {facet} FROM matching", *args[:2]
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
        + " AND ".join(f"${i}::text[] IS NOT NULL" for i in range(3, 7)),
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
        price_key = row["avg_price"] if order != "relevance" else Decimal(0)
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
