import asyncio
from decimal import Decimal
from unittest.mock import patch

import pytest

from service.db.search import CursorExpired, search
from tests.service.import_fixtures import importer, write_day


def test_real_import_scoped_average_and_pinned_pages(database, tmp_path):
    async def run():
        async with database() as db:
            await db.create_tables()
            with patch.object(importer, "db", db):
                await importer.import_directory(
                    write_day(tmp_path, "1.20", "Mlijeko"), False
                )
            first = await search(db, q="mlijeko", city=["City"])
            assert first["products"][0]["avg_price"] == Decimal("1.20")
            assert first["products"][0]["shop_count"] == 1
            assert first["total"] == 1
            assert first["facets"]["city"] == {"City": 1}
            assert (await search(db, q="mlijeko", city=["Elsewhere"]))["total"] == 0
            assert (
                first["products"][0]["chains"][0]["price_date"].isoformat()
                == "2026-11-14"
            )

    asyncio.run(run())


@pytest.mark.parametrize("page_size", [137, 10000])
def test_complete_relation_paging_and_expired_cursor(database, page_size):
    async def run():
        async with database() as db:
            await db.create_tables()
            async with db.import_transaction():
                async with db._get_conn() as conn:
                    await conn.execute("INSERT INTO chains(code) VALUES('test')")
                    await conn.execute(
                        "INSERT INTO stores(chain_id,code,city) VALUES(1,'1','City')"
                    )
                    await conn.execute(
                        "INSERT INTO products(ean,name) SELECT i::text,'Mlijeko' FROM generate_series(1,10001) i"
                    )
                    await conn.execute(
                        "INSERT INTO chain_products(chain_id,product_id,code,name) SELECT 1,id,ean,'Mlijeko' FROM products"
                    )
                    await conn.execute(
                        "INSERT INTO latest_offers(chain_product_id,store_id,price_date,regular_price) SELECT id,1,'2026-11-14',CASE WHEN id=10001 THEN 0 ELSE 1.20 END FROM chain_products"
                    )
            first = await search(
                db, q="mlijeko", page_size=page_size, order="price_asc"
            )
            assert first["total"] == 10001
            assert first["products"][0]["ean"] == "10001"
            assert first["products"][0]["avg_price"] == 0
            ids = [p["ean"] for p in first["products"]]
            cursor = first["next_cursor"]
            while cursor:
                page = await search(
                    db,
                    q="mlijeko",
                    page_size=page_size,
                    order="price_asc",
                    cursor=cursor,
                )
                assert page["generation"] == first["generation"]
                ids.extend(p["ean"] for p in page["products"])
                cursor = page["next_cursor"]
            assert len(ids) == len(set(ids)) == 10001
            with pytest.raises(ValueError, match="scope"):
                await search(
                    db, q="mlijeko", city=["Other"], cursor=first["next_cursor"]
                )

            async with db.import_transaction():
                await db._fetchval(
                    "UPDATE latest_offers SET regular_price=9 WHERE chain_product_id=10001 RETURNING store_id"
                )
            with pytest.raises(CursorExpired):
                await search(
                    db,
                    q="mlijeko",
                    page_size=page_size,
                    order="price_asc",
                    cursor=first["next_cursor"],
                )
            restarted = await search(db, q="mlijeko", order="price_asc")
            assert restarted["generation"] > first["generation"]
            assert restarted["products"][0]["ean"] != "10001"

    asyncio.run(run())


def test_authored_queries_through_postgres_retrieval(database):
    import json
    from pathlib import Path

    cases = json.loads(Path(__file__).with_name("sorting-fixtures.json").read_text())

    async def run():
        async with database() as db:
            await db.create_tables()
            async with db.import_transaction():
                async with db._get_conn() as conn:
                    chain = await conn.fetchval(
                        "INSERT INTO chains(code) VALUES('fixture') RETURNING id"
                    )
                    store = await conn.fetchval(
                        "INSERT INTO stores(chain_id,code) VALUES($1,'1') RETURNING id",
                        chain,
                    )
                    for case in cases:
                        if case["q"].isdigit():
                            continue
                        for candidate in case["candidates"]:
                            identity = f"fixture:{case['id']}:{candidate['ean']}"
                            product = await conn.fetchval(
                                "INSERT INTO products(ean) VALUES($1) RETURNING id",
                                identity,
                            )
                            cp = await conn.fetchval(
                                "INSERT INTO chain_products(chain_id,product_id,code,name,brand,category) VALUES($1,$2,$3,$4,$5,$6) RETURNING id",
                                chain,
                                product,
                                identity,
                                candidate["name"],
                                candidate.get("brand", ""),
                                str(case["id"]),
                            )
                            await conn.execute(
                                "INSERT INTO latest_offers(chain_product_id,store_id,price_date,regular_price) VALUES($1,$2,'2026-11-14',1)",
                                cp,
                                store,
                            )
            for case in cases:
                if case["q"].isdigit():
                    continue
                result = await search(db, q=case["q"], category=[str(case["id"])])
                retrieved = {p["ean"].split(":")[-1] for p in result["products"]}
                assert set(case["acceptable"]) <= retrieved, case["id"]
                if case["acceptable"]:
                    assert (
                        result["products"][0]["ean"].split(":")[-1]
                        in case["acceptable"]
                    ), case["id"]
                if case["category"] == "negative":
                    assert result["total"] == 0, case["id"]

    asyncio.run(run())
