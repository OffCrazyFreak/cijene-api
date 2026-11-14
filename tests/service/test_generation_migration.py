import asyncio
from datetime import date
from pathlib import Path

import pytest


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
            VALUES(1,1,'2026-01-01',2,false,0),(1,2,'2026-01-02',3,true,NULL);
        """)


def test_legacy_baseline_repeat_and_immutable(database):
    async def run():
        async with database() as db:
            await legacy(db)
            await db.create_tables()
            async with db._get_conn() as conn:
                head = await conn.fetchval("SELECT active_id FROM publication_state")
                assert head is not None
                assert (
                    await conn.fetchval("SELECT previous_id FROM publication_state")
                    is None
                )
                assert (
                    await conn.fetchval("SELECT count(*) FROM generation_offers") == 2
                )
                assert await conn.fetchval("SELECT count(*) FROM users") == 1
                assert (
                    await conn.fetchval(
                        "SELECT count(*) FROM offer_versions WHERE revision_id IS NULL"
                    )
                    == 2
                )
                assert (
                    await conn.fetchval(
                        "SELECT min(price_date)::text FROM offer_versions"
                    )
                    == "2026-01-01"
                )
                with pytest.raises(Exception, match="immutable"):
                    await conn.execute("UPDATE offer_versions SET regular_price=9")
                with pytest.raises(Exception, match="immutable"):
                    await conn.execute("DELETE FROM generation_offers")
            await db.create_tables()
            assert await db._fetchval("SELECT count(*) FROM generations") == 1
            assert await db._fetchval("SELECT active_id FROM publication_state") == head
            assert await db.count_prices_before(date(2027, 1, 1)) == {}
            with pytest.raises(RuntimeError, match="generation"):
                await db.delete_prices_before(date(2027, 1, 1))

    asyncio.run(run())


def test_empty_initialization_is_repeat_safe(database):
    async def run():
        async with database() as db:
            await db.create_tables()
            await db.create_tables()
            assert await db._fetchval("SELECT count(*) FROM generations") == 0
            assert (
                await db._fetchval("SELECT baseline_complete FROM publication_state")
                is True
            )
            await legacy(db)
            await db.create_tables()
            assert await db._fetchval("SELECT count(*) FROM generations") == 0

    asyncio.run(run())


@pytest.mark.parametrize("populated", [False, True])
def test_concurrent_initialization(database, populated):
    async def run():
        async with database() as db:
            if populated:
                await legacy(db)
            await asyncio.gather(db.create_tables(), db.create_tables())
            assert await db._fetchval("SELECT count(*) FROM generations") == int(
                populated
            )
            assert await db._fetchval("SELECT count(*) FROM generation_offers") == (
                2 if populated else 0
            )

    asyncio.run(run())


def test_failed_backfill_is_atomic(database):
    async def run():
        async with database() as db:
            await db.create_tables()
            await legacy(db)
            async with db._get_conn() as conn:
                await conn.execute("""
                    UPDATE publication_state SET baseline_complete=FALSE;
                    CREATE FUNCTION fail_stats() RETURNS trigger LANGUAGE plpgsql AS $$
                    BEGIN RAISE EXCEPTION 'injected failure'; END; $$;
                    CREATE TRIGGER fail_stats BEFORE INSERT ON generation_stats
                    FOR EACH ROW EXECUTE FUNCTION fail_stats();
                """)
            with pytest.raises(Exception, match="injected failure"):
                await db.create_tables()
            assert await db._fetchval("SELECT count(*) FROM generations") == 0
            assert await db._fetchval("SELECT count(*) FROM offer_versions") == 0
            assert await db._fetchval("SELECT count(*) FROM prices") == 2
            assert (
                await db._fetchval("SELECT baseline_complete FROM publication_state")
                is False
            )
            async with db._get_conn() as conn:
                await conn.execute("DROP TRIGGER fail_stats ON generation_stats")
            await db.create_tables()
            assert await db._fetchval("SELECT count(*) FROM generation_offers") == 2

    asyncio.run(run())


def test_legacy_full_payload_and_snapshot_isolation(database):
    async def run():
        async with database() as db:
            await legacy(db)
            async with db._get_conn() as conn:
                await conn.execute("""
                    UPDATE prices SET unit_price=1.23,best_price_30=0,
                        anchor_price=4.56,anchor_price_date='2025-05-02',special_sale_type='promo';
                    UPDATE stores SET type='shop',address='Street',city='City',zipcode='10000',phone='123';
                    UPDATE chain_products SET brand='Brand',category='Food',unit='kg',quantity='1';
                """)
                original = await conn.fetch("SELECT * FROM users")
            await db.create_tables()
            async with db._get_conn() as conn:
                for kind, source in [
                    ("chain", "chains"),
                    ("product", "products"),
                    ("store", "stores"),
                    ("chain_product", "chain_products"),
                    ("offer", "prices"),
                ]:
                    rows = await conn.fetch(
                        f"SELECT * FROM {kind}_versions ORDER BY id"
                    )
                    old = await conn.fetch(f"SELECT * FROM {source} ORDER BY id")
                    for version, prior in zip(rows, old, strict=True):
                        for key in prior.keys():
                            if key != "earth_point":
                                assert version[key] == prior[key]
                assert await conn.fetch("SELECT * FROM users") == original
                assert (
                    await conn.fetchval("SELECT min(avg_price) FROM generation_stats")
                    == 1.5
                )
                assert await conn.fetchval("SELECT count(*) FROM generation_stats") == 1
                assert (
                    await conn.fetchval(
                        "SELECT count(*) FROM offer_versions WHERE listing_status='unknown'"
                    )
                    == 2
                )
                sizes = await conn.fetch(
                    "SELECT relname,pg_total_relation_size(oid) bytes FROM pg_class WHERE relname IN ('offer_versions','generation_offers')"
                )
                print("bounded baseline relation sizes:", [dict(row) for row in sizes])
                await conn.execute(
                    "UPDATE chain_products SET name='Later'; UPDATE prices SET regular_price=99"
                )
                assert (
                    await conn.fetchval("SELECT name FROM chain_product_versions")
                    == "Corrected"
                )
                assert (
                    await conn.fetchval("SELECT max(regular_price) FROM offer_versions")
                    == 3
                )
            await db.create_tables()
            assert (
                await db._fetchval("SELECT max(regular_price) FROM offer_versions") == 3
            )

    asyncio.run(run())


def test_cancelled_backfill_rolls_back_all_publication_state(database, monkeypatch):
    from service.db import psql

    async def run():
        async with database() as db:
            await legacy(db)
            reached = asyncio.Event()
            release = asyncio.Event()
            original = psql.backfill_legacy

            async def paused(conn):
                result = await original(conn)
                reached.set()
                await release.wait()
                return result

            with monkeypatch.context() as changes:
                changes.setattr(psql, "backfill_legacy", paused)
                task = asyncio.create_task(db.create_tables())
                await reached.wait()
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            assert await db._fetchval("SELECT to_regclass('publication_state')") is None
            assert await db._fetchval("SELECT count(*) FROM prices") == 2
            await db.create_tables()
            assert await db._fetchval("SELECT count(*) FROM generations") == 1

    asyncio.run(run())


def test_cleanup_cli_reports_protected_state(database, monkeypatch, caplog):
    from service.db import clean_prices

    async def run():
        async with database() as db:
            await legacy(db)
            await db.create_tables()
            monkeypatch.setattr(clean_prices, "db", db)
            await clean_prices.clean_prices(30, True)
            assert "protect all prices" in caplog.text
            assert "would delete" not in caplog.text.lower()
            with pytest.raises(RuntimeError, match="generation"):
                await clean_prices.clean_prices(30, False)
            assert await db._fetchval("SELECT count(*) FROM prices") == 2

    asyncio.run(run())


@pytest.mark.parametrize("special", [None, "0", "1.00", "3.00"])
def test_baseline_uses_lower_supplied_price(database, special):
    from decimal import Decimal

    async def run():
        async with database() as db:
            await legacy(db)
            async with db._get_conn() as conn:
                await conn.execute(
                    "UPDATE prices SET regular_price=2.01,special_price=$1 WHERE store_id=1",
                    Decimal(special) if special is not None else None,
                )
            await db.create_tables()
            first = (
                min(Decimal("2.01"), Decimal(special))
                if special is not None
                else Decimal("2.01")
            )
            async with db._get_conn() as conn:
                stats = await conn.fetchrow("SELECT * FROM generation_stats")
                assert stats["min_price"] == first
                assert stats["max_price"] == Decimal("3")
                assert stats["avg_price"] == (first + Decimal("3")) / 2
                assert stats["offer_count"] == 2

    asyncio.run(run())


def test_acceptance_waits_for_membership_edits(database):
    from tests.service.async_helpers import task_scope

    async def run():
        async with database() as db:
            await legacy(db)
            await db.create_tables()
            async with db._get_conn() as editing, db._get_conn() as accepting:
                generation = await editing.fetchval(
                    "INSERT INTO generations(origin) VALUES('test') RETURNING id"
                )
                await editing.execute(
                    "INSERT INTO generation_products SELECT $1,id,version_id FROM generation_products WHERE generation_id=(SELECT active_id FROM publication_state)",
                    generation,
                )
                version = await editing.fetchval(
                    "INSERT INTO product_versions(id,ean,name) VALUES(1,'123','Revised') RETURNING version_id"
                )
                async with editing.transaction():
                    await editing.execute(
                        "UPDATE generation_products SET version_id=$1 WHERE generation_id=$2",
                        version,
                        generation,
                    )
                    pid = await accepting.fetchval("SELECT pg_backend_pid()")
                    async with task_scope() as workers:
                        task = asyncio.create_task(
                            accepting.execute(
                                "UPDATE generations SET accepted_at=CURRENT_TIMESTAMP WHERE id=$1",
                                generation,
                            )
                        )
                        workers.append(task)
                        async with db._get_conn() as observer:
                            async with asyncio.timeout(5):
                                while not task.done():
                                    if await observer.fetchval(
                                        "SELECT cardinality(pg_blocking_pids($1)) > 0",
                                        pid,
                                    ):
                                        break
                                else:
                                    await task
                                    pytest.fail(
                                        "Acceptance committed before the membership edit"
                                    )
                        task.cancel()
                        with pytest.raises(asyncio.CancelledError):
                            await task
                await accepting.execute(
                    "UPDATE generations SET accepted_at=CURRENT_TIMESTAMP WHERE id=$1",
                    generation,
                )
                with pytest.raises(Exception, match="immutable"):
                    await editing.execute(
                        "DELETE FROM generation_products WHERE generation_id=$1",
                        generation,
                    )

    asyncio.run(run())
