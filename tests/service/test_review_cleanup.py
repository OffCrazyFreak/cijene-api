import asyncio
from datetime import date
from unittest.mock import AsyncMock


def test_cleanup_keeps_raw_and_stats_dates(database, monkeypatch):
    async def run():
        async with database() as db:
            await db.create_tables()
            monkeypatch.setattr(db, "_vacuum_prices", AsyncMock())
            expected = set()
            async with db._get_conn() as conn:
                for chain, stats, latest in [
                    ("none", None, 3),
                    ("older", 2, 3),
                    ("recent", 2, 6),
                    ("matching", 3, 3),
                    ("boundary", 2, 5),
                ]:
                    cid = await conn.fetchval(
                        "INSERT INTO chains(code) VALUES($1) RETURNING id", chain
                    )
                    pid = await conn.fetchval(
                        "INSERT INTO chain_products(chain_id,code,name) VALUES($1,'1','Milk') RETURNING id",
                        cid,
                    )
                    sid = await conn.fetchval(
                        "INSERT INTO stores(chain_id,code) VALUES($1,'1') RETURNING id",
                        cid,
                    )
                    for day in sorted({1, latest} | ({stats} if stats else set())):
                        await conn.execute(
                            "INSERT INTO prices(chain_product_id,store_id,price_date,regular_price) VALUES($1,$2,$3,1)",
                            pid,
                            sid,
                            date(2026, 1, day),
                        )
                        if day in (latest, stats):
                            expected.add((cid, date(2026, 1, day)))
                    if stats:
                        await conn.execute(
                            "INSERT INTO chain_stats(chain_id,price_date,price_count,store_count) VALUES($1,$2,1,1)",
                            cid,
                            date(2026, 1, stats),
                        )
            counts = await db.count_prices_before(date(2026, 1, 5))
            deleted = await db.delete_prices_before(date(2026, 1, 5))
            assert counts == deleted == {date(2026, 1, 1): 5}
            async with db._get_conn() as conn:
                rows = await conn.fetch(
                    "SELECT cp.chain_id,p.price_date FROM prices p JOIN chain_products cp ON cp.id=p.chain_product_id"
                )
            assert {(r["chain_id"], r["price_date"]) for r in rows} == expected

    asyncio.run(run())
