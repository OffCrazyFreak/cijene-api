import asyncio
from secrets import token_hex
from unittest.mock import patch

from tests.service.import_fixtures import importer, write_day


def test_latest_http_contract(database, service_client, tmp_path):
    async def run():
        async with database() as db:
            async with service_client(db) as client:
                token = token_hex(32)
                await db._fetchval(
                    "INSERT INTO users(name,api_key) VALUES('test',$1) RETURNING id",
                    token,
                )
                with patch.object(importer, "db", db):
                    await importer.import_directory(
                        write_day(tmp_path, "1.20", "Mlijeko"), False
                    )
                headers = {"Authorization": f"Bearer {token}"}
                response = await client.get(
                    "/v1/products/",
                    params={"q": "mlijeko", "page_size": 137},
                    headers=headers,
                )
                assert response.status_code == 200, response.text
                data = response.json()
                assert data["total"] == 1 and data["generation"] is not None
                assert data["products"][0]["shop_count"] == 1
                assert data["products"][0]["chains"][0]["price_date"] == "2026-11-14"
                for params in [
                    {"q": "m"},
                    {"q": "mlijeko", "date": "2026-11-13"},
                    {"q": "mlijeko", "limit": 4, "page_size": 5},
                    {"q": "mlijeko", "cursor": "malformed"},
                ]:
                    assert (
                        await client.get(
                            "/v1/products/", params=params, headers=headers
                        )
                    ).status_code == 400
                assert (
                    await client.get(
                        "/v1/products/", params={"page_size": 10001}, headers=headers
                    )
                ).status_code == 422
                for path in [
                    "/v1/products/12345678/",
                    "/v1/stores/",
                    "/v1/test/stores/",
                    "/v1/chain-stats/",
                    "/v1/prices/?eans=12345678",
                ]:
                    response = await client.get(path, headers=headers)
                    assert response.status_code == 200, response.text

    asyncio.run(run())


def test_request_keeps_snapshot_during_import(database, tmp_path):
    from contextlib import asynccontextmanager

    from service.db.search import search

    async def run():
        async with database() as db:
            await db.create_tables()
            with patch.object(importer, "db", db):
                day = write_day(tmp_path, "2.00", "Mlijeko")
                await importer.import_directory(day, False)
                original = db._get_conn
                pinned, published = asyncio.Event(), asyncio.Event()

                class Connection:
                    def __init__(self, conn):
                        self.conn = conn

                    def __getattr__(self, name):
                        return getattr(self.conn, name)

                    async def fetchrow(self, query, *args):
                        pinned.set()
                        await asyncio.wait_for(published.wait(), 10)
                        return await self.conn.fetchrow(query, *args)

                @asynccontextmanager
                async def connection():
                    async with original() as conn:
                        yield Connection(conn)

                async def correct():
                    await asyncio.wait_for(pinned.wait(), 10)
                    try:
                        await importer.import_directory(
                            write_day(tmp_path, "3.00", "Mlijeko"), False
                        )
                    finally:
                        published.set()

                with patch.object(db, "_get_conn", connection):
                    writer = asyncio.create_task(correct())
                    try:
                        result = await search(db, q="mlijeko")
                        await writer
                    finally:
                        if not writer.done():
                            writer.cancel()
                        await asyncio.gather(writer, return_exceptions=True)
                assert result["products"][0]["avg_price"] == 2
                assert (await search(db, q="mlijeko"))["products"][0]["avg_price"] == 3

    asyncio.run(run())


def test_review_blockers_http_and_bounded_matching(database, service_client):
    import time
    import tracemalloc
    from contextlib import asynccontextmanager

    from service.db import vocabulary as matcher

    async def run():
        async with database() as db:
            async with service_client(db) as client:
                token = token_hex(32)
                await db._fetchval(
                    "INSERT INTO users(name,api_key) VALUES('test',$1) RETURNING id",
                    token,
                )
                async with db.import_transaction():
                    async with db._get_conn() as conn:
                        await conn.execute("INSERT INTO chains(code) VALUES('test')")
                        await conn.execute(
                            "INSERT INTO stores(chain_id,code,city) SELECT 1,i::text,CASE WHEN i=1 THEN 'Here' ELSE 'Elsewhere' END FROM generate_series(1,100) i"
                        )
                        candidates = [
                            ("Limuni svjezi", "", "1", "kg"),
                            ("Pivo limun", "", "1", "l"),
                            ("Krumpiri svjezi", "", "1", "kg"),
                            ("Krumpir cips", "", "1", "kg"),
                            ("Mlijeko trajno", "", "1", "l"),
                            ("Mlijeko trajno", "", "1000", "ml"),
                            ("Mlijeko trajno", "", "2", "l"),
                            ("Milkana sir", "Milkana", "1", "kg"),
                            ("Cokolada", "Milka", "1", "kg"),
                            ("Unrelated", "", "1", "kg"),
                            ("Mlijeko 2l 1kom", "", None, None),
                        ]
                        for i, (name, brand, quantity, unit) in enumerate(
                            candidates, 1
                        ):
                            await conn.execute(
                                "INSERT INTO products(ean) VALUES($1)", str(i)
                            )
                            await conn.execute(
                                "INSERT INTO chain_products(chain_id,product_id,code,name,brand,quantity,unit) VALUES(1,$1,$2,$3,$4,$5,$6)",
                                i,
                                str(i),
                                name,
                                brand,
                                quantity,
                                unit,
                            )
                        await conn.execute(
                            "INSERT INTO latest_offers(chain_product_id,store_id,price_date,regular_price) SELECT cp.id,s.id,'2026-11-14',1 FROM chain_products cp CROSS JOIN stores s WHERE cp.id<>9"
                        )
                headers = {"Authorization": f"Bearer {token}"}

                async def get(q):
                    response = await client.get(
                        "/v1/products/", params=dict(q=q, city="Here"), headers=headers
                    )
                    assert response.status_code == 200, response.text
                    return response.json()

                calls = []
                original = matcher.score_words

                def counted(*args, **kwargs):
                    calls.append(args)
                    return original(*args, **kwargs)

                fetch_sizes = []
                original_connection = db._get_conn

                class ObservedConnection:
                    def __init__(self, conn):
                        self.conn = conn

                    def __getattr__(self, name):
                        return getattr(self.conn, name)

                    async def fetch(self, query, *args):
                        rows = await self.conn.fetch(query, *args)
                        fetch_sizes.append(len(rows))
                        return rows

                @asynccontextmanager
                async def observed_connection():
                    async with original_connection() as conn:
                        yield ObservedConnection(conn)

                offer_count = await db._fetchval("SELECT count(*) FROM latest_offers")
                tracemalloc.start()
                started = time.monotonic()
                with (
                    patch.object(matcher, "score_words", counted),
                    patch.object(db, "_get_conn", observed_connection),
                ):
                    data = await get("limun")
                elapsed = time.monotonic() - started
                _, peak = tracemalloc.get_traced_memory()
                tracemalloc.stop()
                assert len(calls) == 2
                assert max(fetch_sizes) <= len(candidates)
                assert data["products"][0]["ean"] == "1"
                assert data["products"][0]["shop_count"] == 1
                assert (await get("krumpir"))["products"][0]["ean"] == "3"
                assert (await get("limun pivo"))["products"][0]["ean"] == "2"
                assert {p["ean"] for p in (await get("mlijeko 1l"))["products"]} == {
                    "5",
                    "6",
                }
                assert (await get("milka"))["products"] == []
                print(
                    f"review smoke offers={offer_count} max_fetched_rows={max(fetch_sizes)} variants={len(calls)} selected_shops=1 seconds={elapsed:.3f} python_peak_bytes={peak}"
                )

    asyncio.run(run())


def test_city_alias_facets_keep_labels_and_unique_products(database, service_client):
    import asyncio
    import json
    from tests.service.test_latest_migration import legacy

    async def run():
        async with database() as db:
            await legacy(db)
            async with db._get_conn() as conn:
                await conn.execute(
                    "UPDATE stores SET city=CASE id WHEN 1 THEN '6C KRK' ELSE 'KRK' END"
                )
            async with service_client(db) as client:
                async with db._get_conn() as conn:
                    await conn.execute(
                        "INSERT INTO users(name,api_key) VALUES('alias','alias-fixture')"
                    )
                labels = json.dumps({"6C KRK": "Krk", "KRK": "Krk"})
                response = await client.get(
                    "/v1/products/",
                    params={
                        "q": "Corrected",
                        "city": ["6C KRK", "KRK"],
                        "city_labels": labels,
                    },
                    headers={"Authorization": "Bearer alias-fixture"},
                )
                assert response.status_code == 200
                data = response.json()
                assert data["facets"]["city"] == {"Krk": 1}
                assert data["total"] == 1
                assert data["products"][0]["shop_count"] == 2
                ean = await db._fetchval("SELECT ean FROM products")
                barcode = await client.get(
                    f"/v1/products/{ean}/",
                    params={"city": ["6C KRK", "KRK"], "city_labels": labels},
                    headers={"Authorization": "Bearer alias-fixture"},
                )
                assert barcode.status_code == 200, barcode.text
                assert barcode.json()["shop_count"] == 2
                invalid_barcode = await client.get(
                    f"/v1/products/{ean}/",
                    params={"city_labels": "[]"},
                    headers={"Authorization": "Bearer alias-fixture"},
                )
                assert invalid_barcode.status_code == 400
                for invalid in ["not-json", "[]", '{"KRK": 4}']:
                    invalid_response = await client.get(
                        "/v1/products/",
                        params={"q": "Corrected", "city_labels": invalid},
                        headers={"Authorization": "Bearer alias-fixture"},
                    )
                    assert invalid_response.status_code == 400

    asyncio.run(run())


def test_repeated_literal_filters_keep_commas(database, service_client):
    async def run():
        async with database() as db:
            async with service_client(db) as client:
                await db._fetchval(
                    "INSERT INTO users(name,api_key) VALUES('filters','filters-fixture') RETURNING id"
                )
                async with db.import_transaction():
                    async with db._get_conn() as conn:
                        await conn.execute("""
                            INSERT INTO chains(code) VALUES('one'),('two');
                            INSERT INTO stores(chain_id,code,city) VALUES(1,'1','Split'),(2,'1','Zadar');
                            INSERT INTO products(ean) VALUES('1'),('2'),('3');
                            INSERT INTO chain_products(chain_id,product_id,code,name,category) VALUES
                                (1,1,'1','Jabuka','Voće, povrće'),
                                (1,2,'2','Jabuka sok','Voće'),
                                (2,3,'3','Jabuka zelena','Povrće');
                            INSERT INTO latest_offers(chain_product_id,store_id,price_date,regular_price)
                            VALUES(1,1,'2026-11-14',1),(2,1,'2026-11-14',1),(3,2,'2026-11-14',1);
                        """)

                async def eans(params):
                    response = await client.get(
                        "/v1/products/",
                        params={"q": "jabuka", **params},
                        headers={"Authorization": "Bearer filters-fixture"},
                    )
                    assert response.status_code == 200, response.text
                    return sorted(p["ean"] for p in response.json()["products"])

                assert await eans({"category": "Voće, povrće"}) == ["1"]
                assert await eans({"category": ["Voće, povrće", "Povrće"]}) == [
                    "1",
                    "3",
                ]
                assert await eans({"city": ["Split", "Zadar"]}) == ["1", "2", "3"]
                assert await eans({"chains": "one,two"}) == ["1", "2", "3"]
                assert await eans({"chains": "two"}) == ["3"]

                headers = {"Authorization": "Bearer filters-fixture"}
                first = await client.get(
                    "/v1/products/",
                    params={"q": "jabuka", "page_size": 1},
                    headers=headers,
                )
                cursor = first.json()["next_cursor"]
                async with db.import_transaction():
                    await db._fetchval(
                        "UPDATE latest_offers SET regular_price=2 WHERE chain_product_id=1 RETURNING store_id"
                    )
                expired = await client.get(
                    "/v1/products/",
                    params={"q": "jabuka", "page_size": 1, "cursor": cursor},
                    headers=headers,
                )
                assert expired.status_code == 409
                assert expired.json() == {"detail": "cursor_expired"}

    asyncio.run(run())
