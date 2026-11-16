from unittest.mock import patch

import pytest

from service.db.matching import relevance, normalize


def test_matching():
    assert normalize("ČOKOLADA") == "cokolada"
    assert relevance("mlijeko", "Mlijeko svježe", "") is not None
    assert relevance("mljeko", "Mlijeko svježe", "") is not None
    assert relevance("limun pivo", "Limun svježi", "") is None
    ingredient = relevance("limun", "Limun svježi", "")
    flavored = relevance("limun", "Pivo limun", "")
    assert ingredient is not None and flavored is not None
    assert ingredient < flavored
    assert relevance("limun pivo", "Pivo limun", "") is not None
    assert relevance("bez laktoze", "Mlijeko laktoza", "") is None
    assert relevance("mlijeko 1 l", "Mlijeko 6 x 1 l", "", "6", "l") is not None
    assert relevance("mlijeko 6 l", "Mlijeko 6 x 1 l", "", "6", "l") is not None
    assert relevance("mlijeko 2 l", "Mlijeko 6 x 1 l", "", "6", "l") is None
    assert relevance("mlijeko 1 l", "Mlijeko", "", "abc", "l") is None
    assert relevance("snan l", "Snan l", "") is not None


def test_authored_query_retrieval_report():
    import json
    from pathlib import Path

    cases = json.loads(Path(__file__).with_name("sorting-fixtures.json").read_text())
    failures = []
    for case in cases:
        if case["q"].isdigit():
            continue
        ranked = sorted(
            (score, candidate["ean"])
            for candidate in case["candidates"]
            if (
                score := relevance(
                    case["q"], candidate["name"], candidate.get("brand", "")
                )
            )
            is not None
        )
        retrieved = {ean for _, ean in ranked}
        if case["acceptable"] and (
            not ranked or ranked[0][1] not in case["acceptable"]
        ):
            failures.append((case["id"], case["q"], "ranking", ranked))
        if not set(case["acceptable"]) <= retrieved:
            failures.append((case["id"], case["q"], "retrieval", ranked))
        if case["category"] == "negative" and retrieved:
            failures.append((case["id"], case["q"], "negative", ranked))
    print("Authored query fixture judgments:", failures)
    assert len(cases) == 80
    assert failures == []


async def insert_offers(db, offers):
    async with db.import_transaction():
        async with db._get_conn() as conn:
            await conn.execute("INSERT INTO chains(code) VALUES('test')")
            await conn.execute(
                "INSERT INTO stores(chain_id,code,city) VALUES(1,'1','City'),(1,'2','City')"
            )
            for ean, store, regular, special in offers:
                product = await conn.fetchval(
                    "INSERT INTO products(ean) VALUES($1) ON CONFLICT(ean) DO UPDATE SET ean=EXCLUDED.ean RETURNING id",
                    ean,
                )
                chain_product = await conn.fetchval(
                    "INSERT INTO chain_products(chain_id,product_id,code,name,brand,quantity,unit) VALUES(1,$1,$2,'Mlijeko','Brand','1000','g') ON CONFLICT(chain_id,code) DO UPDATE SET code=EXCLUDED.code RETURNING id",
                    product,
                    ean,
                )
                await conn.execute(
                    "INSERT INTO latest_offers(chain_product_id,store_id,price_date,regular_price,special_price) VALUES($1,$2,'2026-11-14',$3,$4)",
                    chain_product,
                    store,
                    regular,
                    special,
                )


def test_scoped_summary_and_filters(database):
    import asyncio
    from decimal import Decimal

    from service.db.search import search

    async def run():
        async with database() as db:
            await db.create_tables()
            await insert_offers(
                db,
                [
                    ("1", 1, Decimal("1.00"), Decimal("1.40")),
                    ("1", 2, Decimal("1.40"), None),
                ],
            )
            result = await search(db, q="mlijeko", city=["City"], order="price_asc")
            product = result["products"][0]
            assert product["shop_count"] == 2 and product["chain_count"] == 1
            assert product["min_price"] == Decimal("1.00")
            assert product["avg_price"] == Decimal("1.20")
            assert product["unit_prices"] == {"mass": Decimal("1.20")}
            filtered = await search(db, q="mlijeko", city=["City"], brand=["Other"])
            assert filtered["total"] == 0

    asyncio.run(run())


def test_cursor_retains_exact_decimal_comparator(database):
    import asyncio
    from decimal import Decimal

    from service.db.search import decode_cursor, search

    async def run():
        async with database() as db:
            await db.create_tables()
            await insert_offers(
                db,
                [
                    ("a", 1, Decimal("1.20"), None),
                    ("b", 1, Decimal("1.21"), None),
                ],
            )
            first = await search(db, order="price_asc", page_size=1)
            cursor = first["next_cursor"]
            assert Decimal(decode_cursor(cursor)["key"][2]) == Decimal("1.20")
            second = await search(db, order="price_asc", page_size=1, cursor=cursor)
            assert [p["ean"] for p in first["products"] + second["products"]] == [
                "a",
                "b",
            ]

    asyncio.run(run())


@pytest.mark.parametrize(
    ("order", "expected"), [("price_asc", ["a", "b"]), ("price_desc", ["b", "a"])]
)
def test_price_orders_use_lowest_scoped_price(database, order, expected):
    import asyncio
    from decimal import Decimal

    from service.db.search import search

    async def run():
        async with database() as db:
            await db.create_tables()
            await insert_offers(
                db,
                [
                    ("a", 1, Decimal("1.00"), None),
                    ("a", 2, Decimal("9.00"), None),
                    ("b", 1, Decimal("3.00"), None),
                ],
            )
            result = await search(db, q="mlijeko", order=order)
            assert [p["ean"] for p in result["products"]] == expected
            assert result["products"][expected.index("a")]["avg_price"] == 5

    asyncio.run(run())


def test_transposition_with_another_edit():
    from service.db.matching import token_distance

    assert token_distance("ockolada", "cokolade") == 2
    assert token_distance("abcd", "badc") == 2
    assert relevance("ockolada", "Cokolade", "") is not None


def test_impossible_lengths_skip_distance_matrix(monkeypatch):
    from service.db import matching

    def unexpected(*args, **kwargs):
        raise AssertionError("Impossible edit distance should be rejected first")

    monkeypatch.setattr(matching, "token_distance", unexpected)
    assert matching.relevance("x" * 200, "Mlijeko", "") is None


def test_long_query_rejected_before_database_access():
    import asyncio
    import pytest
    from service.db.search import search

    with pytest.raises(ValueError, match="256"):
        asyncio.run(search(None, q="x" * 257))


def test_http_long_query_rejected(database, service_client):
    import asyncio

    async def run():
        async with database() as db:
            async with service_client(db) as client:
                async with db._get_conn() as conn:
                    await conn.execute(
                        "INSERT INTO users(name,api_key) VALUES('fixture','length-fixture')"
                    )
                result = await client.get(
                    "/v1/products/",
                    params={"q": "x" * 257},
                    headers={"Authorization": "Bearer length-fixture"},
                )
                assert result.status_code == 422

    asyncio.run(run())


def test_later_pages_reuse_the_cached_ordering(database):
    import asyncio
    from contextlib import asynccontextmanager
    from decimal import Decimal

    from service.db.search import search

    async def run():
        async with database() as db:
            await db.create_tables()
            await insert_offers(
                db, [(ean, 1, Decimal("1.00"), None) for ean in ("a", "b", "c")]
            )
            orderings = []
            original = db._get_conn

            class Counted:
                def __init__(self, conn):
                    self.conn = conn

                def __getattr__(self, name):
                    return getattr(self.conn, name)

                async def fetchrow(self, query, *args):
                    orderings.append(query)
                    return await self.conn.fetchrow(query, *args)

            @asynccontextmanager
            async def counted():
                async with original() as conn:
                    yield Counted(conn)

            with patch.object(db, "_get_conn", counted):
                first = await search(db, q="mlijeko", page_size=1)
                second = await search(
                    db, q="mlijeko", page_size=1, cursor=first["next_cursor"]
                )
                third = await search(
                    db, q="mlijeko", page_size=1, cursor=second["next_cursor"]
                )
            assert len(orderings) == 1
            assert [p["ean"] for p in first["products"] + second["products"]] == [
                "a",
                "b",
            ]
            assert third["products"][0]["ean"] == "c" and third["next_cursor"] is None

    asyncio.run(run())


def test_time_budget_and_statement_timeout_return_503(
    database, service_client, monkeypatch
):
    import asyncio
    from contextlib import asynccontextmanager
    from decimal import Decimal

    from service.db import search as searching

    async def run():
        async with database() as db:
            async with service_client(db) as client:
                await db._fetchval(
                    "INSERT INTO users(name,api_key) VALUES('slow','slow-fixture') RETURNING id"
                )
                await insert_offers(db, [("a", 1, Decimal("1.00"), None)])
                original = db._get_conn

                class Slow:
                    def __init__(self, conn):
                        self.conn = conn

                    def __getattr__(self, name):
                        return getattr(self.conn, name)

                    async def fetchrow(self, query, *args):
                        await self.conn.execute("SELECT pg_sleep(2)")
                        return await self.conn.fetchrow(query, *args)

                @asynccontextmanager
                async def slow():
                    async with original() as conn:
                        yield Slow(conn)

                async def status(**budget):
                    with monkeypatch.context() as changes:
                        for name, value in budget.items():
                            changes.setattr(searching, name, value)
                        with patch.object(db, "_get_conn", slow):
                            response = await client.get(
                                "/v1/products/",
                                params={"q": "mlijeko"},
                                headers={"Authorization": "Bearer slow-fixture"},
                            )
                    return response.status_code, response.json()

                assert await status(SEARCH_STATEMENT_TIMEOUT="50ms") == (
                    503,
                    {"detail": "search_timeout"},
                )
                assert await status(SEARCH_BUDGET_SECONDS=0.2) == (
                    503,
                    {"detail": "search_timeout"},
                )
                with patch.object(db, "_get_conn", original):
                    response = await client.get(
                        "/v1/products/",
                        params={"q": "mlijeko"},
                        headers={"Authorization": "Bearer slow-fixture"},
                    )
                assert response.status_code == 200

    asyncio.run(run())


def test_cold_requests_share_one_catalogue_build(database, monkeypatch):
    import asyncio
    from decimal import Decimal

    from service.db import search_cache
    from service.db.search import search

    async def run():
        async with database() as db:
            await db.create_tables()
            await insert_offers(db, [("a", 1, Decimal("1.00"), None)])
            original = search_cache.fetch_catalogue
            fetches = []
            release = asyncio.Event()

            async def slow(conn):
                fetches.append(conn)
                await release.wait()
                return await original(conn)

            monkeypatch.setattr(search_cache, "fetch_catalogue", slow)
            abandoned = asyncio.create_task(search(db, q="mlijeko"))
            while not fetches:
                await asyncio.sleep(0.01)
            waiter = asyncio.create_task(search(db, q="mlijeko", order="price_asc"))
            await asyncio.sleep(0.05)
            abandoned.cancel()
            await asyncio.gather(abandoned, return_exceptions=True)
            while len(fetches) < 2:
                await asyncio.sleep(0.01)
            concurrent = asyncio.create_task(search(db, q="mlijeko", city=["City"]))
            await asyncio.sleep(0.05)
            release.set()
            results = await asyncio.wait_for(asyncio.gather(waiter, concurrent), 10)
            assert len(fetches) == 2
            assert all(result["total"] == 1 for result in results)

    asyncio.run(run())


def test_barcode_filters_skip_the_catalogue(database, monkeypatch):
    import asyncio
    from decimal import Decimal

    from service.db import search_cache
    from service.db.search import search

    async def run():
        async with database() as db:
            await db.create_tables()
            await insert_offers(db, [("12345678", 1, Decimal("1.00"), None)])

            async def unexpected(conn):
                raise AssertionError("barcode lookups must not load the catalogue")

            monkeypatch.setattr(search_cache, "fetch_catalogue", unexpected)
            found = await search(
                db, identity="12345678", city=["city"], brand=["BRAND"], chains="test"
            )
            assert found["total"] == 1
            missing = await search(db, identity="12345678", city=["Other"])
            assert missing["total"] == 0

    asyncio.run(run())
