import asyncio
from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from tests.service.async_helpers import wait_for_event


async def legacy(db):
    async with db._get_conn() as conn:
        await conn.execute((Path("service/db/psql.sql")).read_text())
        await conn.execute(
            Path("service/db/migrations/001_price_provenance.sql").read_text()
        )
        await conn.execute("""
            INSERT INTO users(name,api_key) VALUES('fixture','fixture');
            INSERT INTO chains(code) VALUES('test');
            INSERT INTO products(ean,name) VALUES('123','Global');
            INSERT INTO chain_products(chain_id,product_id,code,name) VALUES(1,1,'p','Corrected');
            INSERT INTO stores(chain_id,code,lat,lon) VALUES(1,'old',45,16),(1,'new',46,17);
            INSERT INTO prices(chain_product_id,store_id,price_date,regular_price,available,special_price)
            VALUES(1,1,'2026-01-01',2,false,0),(1,2,'2026-01-02',3,true,NULL),
                  (1,2,'2025-12-31',5,true,NULL);
        """)


def test_legacy_backfill_repeat_and_cleanup(database, monkeypatch):
    async def run():
        async with database() as db:
            await legacy(db)
            await db.create_tables()
            monkeypatch.setattr(db, "_vacuum_prices", AsyncMock())
            offers = await db._fetchval(
                "SELECT array_agg(regular_price ORDER BY store_id) FROM latest_offers"
            )
            assert offers == [Decimal("2.00"), Decimal("3.00")]
            assert await db._fetchval("SELECT epoch FROM import_state") == 1
            assert await db._fetchval("SELECT count(*) FROM users") == 1
            assert await db._fetchval("SELECT to_regclass('generations')") is None
            from service.db.search import search

            product = (await search(db, q="corrected", order="price_asc"))["products"][
                0
            ]
            assert product["min_price"] == Decimal("2.00")

            await db.create_tables()
            assert await db._fetchval("SELECT epoch FROM import_state") == 1
            assert await db.count_prices_before(date(2026, 1, 2)) == {
                date(2025, 12, 31): 1,
                date(2026, 1, 1): 1,
            }
            assert await db.delete_prices_before(date(2026, 1, 2)) == {
                date(2025, 12, 31): 1,
                date(2026, 1, 1): 1,
            }
            assert await db._fetchval("SELECT count(*) FROM latest_offers") == 2
            assert await db._fetchval("SELECT epoch FROM import_state") == 1

    asyncio.run(run())


def test_empty_initialization_is_repeat_safe(database):
    async def run():
        async with database() as db:
            await db.create_tables()
            await db.create_tables()
            assert (
                await db._fetchval("SELECT baseline_complete FROM import_state") is True
            )
            assert await db._fetchval("SELECT epoch FROM import_state") == 0
            await legacy(db)
            await db.create_tables()
            assert await db._fetchval("SELECT count(*) FROM latest_offers") == 0

    asyncio.run(run())


@pytest.mark.parametrize("populated", [False, True])
def test_concurrent_initialization(database, populated):
    async def run():
        async with database() as db:
            if populated:
                await legacy(db)
            await asyncio.gather(db.create_tables(), db.create_tables())
            assert await db._fetchval("SELECT count(*) FROM latest_offers") == (
                2 if populated else 0
            )
            assert await db._fetchval("SELECT epoch FROM import_state") == int(
                populated
            )

    asyncio.run(run())


def test_failed_backfill_is_atomic(database):
    async def run():
        async with database() as db:
            await db.create_tables()
            await legacy(db)
            async with db._get_conn() as conn:
                await conn.execute("""
                    UPDATE import_state SET baseline_complete=FALSE;
                    CREATE FUNCTION fail_offers() RETURNS trigger LANGUAGE plpgsql AS $$
                    BEGIN RAISE EXCEPTION 'injected failure'; END; $$;
                    CREATE TRIGGER fail_offers BEFORE INSERT ON latest_offers
                    FOR EACH ROW EXECUTE FUNCTION fail_offers();
                """)
            with pytest.raises(Exception, match="injected failure"):
                await db.create_tables()
            assert await db._fetchval("SELECT count(*) FROM latest_offers") == 0
            assert await db._fetchval("SELECT count(*) FROM prices") == 3
            assert (
                await db._fetchval("SELECT baseline_complete FROM import_state")
                is False
            )
            async with db._get_conn() as conn:
                await conn.execute("DROP TRIGGER fail_offers ON latest_offers")
            await db.create_tables()
            assert await db._fetchval("SELECT count(*) FROM latest_offers") == 2

    asyncio.run(run())


def test_legacy_full_payload(database):
    async def run():
        async with database() as db:
            await legacy(db)
            async with db._get_conn() as conn:
                await conn.execute("""
                    UPDATE prices SET unit_price=1.23,best_price_30=0,
                        anchor_price=4.56,anchor_price_date='2025-05-02',special_sale_type='promo';
                """)
            await db.create_tables()
            async with db._get_conn() as conn:
                latest = await conn.fetch(
                    "SELECT * FROM latest_offers ORDER BY store_id"
                )
                expected = await conn.fetch(
                    "SELECT DISTINCT ON (store_id) * FROM prices ORDER BY store_id,price_date DESC"
                )
                for offer, price in zip(latest, expected, strict=True):
                    for key in offer.keys():
                        if key == "special_price" and price[key] == 0:
                            assert offer[key] is None
                        else:
                            assert offer[key] == price[key], key

    asyncio.run(run())


def test_cancelled_backfill_rolls_back(database, monkeypatch):
    from service.db import psql

    async def run():
        async with database() as db:
            await legacy(db)
            reached = asyncio.Event()
            release = asyncio.Event()
            original = psql._backfill_latest_offers

            async def paused(conn):
                await original(conn)
                reached.set()
                await release.wait()

            with monkeypatch.context() as changes:
                changes.setattr(psql, "_backfill_latest_offers", paused)
                task = asyncio.create_task(db.create_tables())
                await wait_for_event(reached, task)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            assert await db._fetchval("SELECT to_regclass('import_state')") is None
            assert await db._fetchval("SELECT count(*) FROM prices") == 3
            await db.create_tables()
            assert await db._fetchval("SELECT count(*) FROM latest_offers") == 2

    asyncio.run(run())


def test_cleanup_cli_deletes_old_dates(database, monkeypatch, caplog):
    import logging

    from service.db import clean_prices

    async def run():
        async with database() as db:
            await legacy(db)
            await db.create_tables()
            monkeypatch.setattr(clean_prices, "db", db)
            monkeypatch.setattr(db, "_vacuum_prices", AsyncMock())
            with caplog.at_level(logging.INFO):
                await clean_prices.clean_prices(30, True)
            assert "would delete 2 rows" in caplog.text.lower()
            await clean_prices.clean_prices(30, False)
            assert await db._fetchval("SELECT count(*) FROM prices") == 1
            assert await db._fetchval("SELECT count(*) FROM latest_offers") == 2

    asyncio.run(run())


def test_epoch_moves_once_per_changing_transaction(database):
    from service.db.models import Product

    async def run():
        async with database() as db:
            await legacy(db)
            await db.create_tables()

            async def epoch():
                return await db._fetchval("SELECT epoch FROM import_state")

            assert await epoch() == 1

            async with db.import_transaction():
                await db.update_store(1, "old", city="City")
                await db.update_product(Product(ean="123", name="Renamed"))
            assert await epoch() == 2

            async with db.import_transaction():
                await db.record_metadata_source_date("stores", [1], date(2026, 11, 14))
            assert await epoch() == 2

            async with db._get_conn() as conn:
                await conn.execute("UPDATE prices SET regular_price=7")
                assert await epoch() == 2

                await conn.execute("TRUNCATE latest_offers")
                assert await epoch() == 3

    asyncio.run(run())


def test_served_tables_have_no_row_copying_triggers(database):
    async def run():
        async with database() as db:
            await db.create_tables()
            await db.create_tables()
            triggers = await db._fetchval(
                """
                SELECT count(*) FROM pg_trigger
                WHERE NOT tgisinternal
                    AND (tgoldtable IS NOT NULL OR tgnewtable IS NOT NULL)
                """
            )
            assert triggers == 0

    asyncio.run(run())


def test_unchanged_metadata_updates_keep_the_epoch(database):
    from service.db.models import Product

    async def run():
        async with database() as db:
            await legacy(db)
            await db.create_tables()
            await db.update_store(1, "old", city="City")
            await db.update_product(Product(ean="123", name="Renamed"))
            before = await db._fetchval("SELECT epoch FROM import_state")

            assert await db.update_store(1, "old", city="City") is True
            assert await db.update_product(Product(ean="123", name="Renamed")) is False
            assert await db._fetchval("SELECT epoch FROM import_state") == before

    asyncio.run(run())
