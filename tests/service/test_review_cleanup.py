import pytest
import asyncio
from datetime import date
from unittest.mock import AsyncMock

from tests.service.async_helpers import task_scope, wait_for_event


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


@pytest.mark.parametrize("method", ["count_prices_before", "delete_prices_before"])
def test_cleanup_holds_writer_lock(database, monkeypatch, method):
    async def run():
        async with database() as db:
            await db.create_tables()
            original = db._get_kept_chains
            entered = asyncio.Event()
            resume = asyncio.Event()

            async def paused(before):
                entered.set()
                await resume.wait()
                return await original(before)

            monkeypatch.setattr(db, "_get_kept_chains", paused)
            task = asyncio.create_task(getattr(db, method)(date(2027, 1, 1)))
            try:
                await wait_for_event(entered, task)
                async with db._get_conn() as conn:
                    async with conn.transaction():
                        assert not await conn.fetchval(
                            "SELECT pg_try_advisory_xact_lock(73198421)"
                        )
                resume.set()
                await asyncio.wait_for(task, timeout=5)
            finally:
                resume.set()
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())


@pytest.mark.parametrize("cleanup_first", [True, False])
def test_import_cleanup_interleaving(database, tmp_path, monkeypatch, cleanup_first):
    from unittest.mock import patch
    from tests.service.import_fixtures import importer, write_day

    async def run():
        async with database() as db:
            await db.create_tables()
            monkeypatch.setattr(db, "_vacuum_prices", AsyncMock())
            reached = asyncio.Event()
            release = asyncio.Event()
            original = db._get_kept_chains

            async def paused(before):
                reached.set()
                await release.wait()
                return await original(before)

            async with task_scope() as workers:
                with patch.object(importer, "db", db):
                    if cleanup_first:
                        monkeypatch.setattr(db, "_get_kept_chains", paused)
                        cleanup = asyncio.create_task(
                            db.delete_prices_before(date(2027, 1, 1))
                        )
                        workers.append(cleanup)
                        await wait_for_event(reached, cleanup)
                        importing = asyncio.create_task(
                            importer.import_directory(write_day(tmp_path), False)
                        )
                        workers.append(importing)
                        async with db._get_conn() as conn:
                            assert (
                                await conn.fetchval("SELECT count(*) FROM prices") == 0
                            )
                        release.set()
                        await asyncio.gather(cleanup, importing)
                    else:

                        async def importing():
                            async with db.import_transaction():
                                reached.set()
                                await release.wait()

                        writer = asyncio.create_task(importing())
                        workers.append(writer)
                        await wait_for_event(reached, writer)
                        cleanup = asyncio.create_task(
                            db.delete_prices_before(date(2027, 1, 1))
                        )
                        workers.append(cleanup)
                        release.set()
                        await asyncio.gather(writer, cleanup)
                        await importer.import_directory(write_day(tmp_path), False)
            assert await db._fetchval("SELECT count(*) FROM prices") == 1

    asyncio.run(run())


@pytest.mark.parametrize("interruption", ["failure", "cancel"])
def test_cleanup_keeps_committed_dates_after_interruption(
    database, monkeypatch, interruption
):
    async def run():
        async with database() as db:
            await db.create_tables()
            monkeypatch.setattr(db, "_vacuum_prices", AsyncMock())
            async with db._get_conn() as conn:
                await conn.execute("""
                    INSERT INTO chains(code) VALUES('test');
                    INSERT INTO chain_products(chain_id,code,name) VALUES(1,'p','Milk');
                    INSERT INTO stores(chain_id,code) VALUES(1,'s');
                    INSERT INTO prices(chain_product_id,store_id,price_date,regular_price)
                    VALUES(1,1,'2026-01-01',1),(1,1,'2026-01-02',1),(1,1,'2026-01-03',1);
                """)
            reached, release = asyncio.Event(), asyncio.Event()
            original = db._price_dates_before

            async def paused(before):
                async for day in original(before):
                    if day == date(2026, 1, 2):
                        reached.set()
                        await release.wait()
                        if interruption == "failure":
                            raise RuntimeError("second date failed")
                    yield day

            monkeypatch.setattr(db, "_price_dates_before", paused)
            async with task_scope() as workers:
                task = asyncio.create_task(db.delete_prices_before(date(2026, 1, 3)))
                workers.append(task)
                await wait_for_event(reached, task)
                async with db._get_conn() as observer:
                    assert (
                        await observer.fetchval(
                            "SELECT count(*) FROM prices WHERE price_date='2026-01-01'"
                        )
                        == 0
                    )
                    async with observer.transaction():
                        assert not await observer.fetchval(
                            "SELECT pg_try_advisory_xact_lock(73198421)"
                        )
                if interruption == "cancel":
                    task.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await task
                else:
                    release.set()
                    with pytest.raises(RuntimeError, match="second date failed"):
                        await task
            async with db._get_conn() as observer:
                async with observer.transaction():
                    assert await observer.fetchval(
                        "SELECT pg_try_advisory_xact_lock(73198421)"
                    )
            monkeypatch.setattr(db, "_price_dates_before", original)
            assert await db.delete_prices_before(date(2026, 1, 3)) == {
                date(2026, 1, 2): 1
            }
            assert await db._fetchval("SELECT count(*) FROM prices") == 1

    asyncio.run(run())


def test_cancelled_cleanup_lock_wait_releases_connection(database):
    async def run():
        async with database() as db:
            await db.create_tables()
            async with db._get_conn() as blocker:
                async with blocker.transaction():
                    await blocker.execute("SELECT pg_advisory_xact_lock(73198421)")
                    async with task_scope() as workers:
                        task = asyncio.create_task(
                            db.delete_prices_before(date(2027, 1, 1))
                        )
                        workers.append(task)
                        async with db._get_conn() as observer:
                            async with asyncio.timeout(5):
                                while not task.done():
                                    if await observer.fetchval(
                                        "SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE datname=current_database() AND cardinality(pg_blocking_pids(pid))>0)"
                                    ):
                                        break
                                else:
                                    await task
                                    pytest.fail(
                                        "Cleanup did not wait for the writer lock"
                                    )
                        task.cancel()
                        with pytest.raises(asyncio.CancelledError):
                            await task

            async def writer():
                async with db.import_transaction():
                    assert await db._fetchval("SELECT 1") == 1

            await asyncio.wait_for(writer(), timeout=5)

    asyncio.run(run())
