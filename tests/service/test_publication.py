import asyncio
from unittest.mock import patch

import pytest

from tests.service.import_fixtures import importer, write_day


def test_import_publication_correction_and_failure(database, tmp_path):
    async def run():
        async with database() as db:
            await db.create_tables()
            with patch.object(importer, "db", db):
                day = write_day(tmp_path)
                await importer.import_directory(day, False)
                first = await db._fetchval("SELECT active_id FROM publication_state")
                assert first is not None
                write_day(tmp_path, "3.00", "Corrected")
                await importer.import_directory(day, False)
                second = await db._fetchval("SELECT active_id FROM publication_state")
                assert second != first
                await importer.import_directory(day, False)
                assert (
                    await db._fetchval("SELECT active_id FROM publication_state")
                    == second
                )
                assert (
                    await db._fetchval("SELECT previous_id FROM publication_state")
                    == first
                )
                assert (
                    await db._fetchval(
                        "SELECT regular_price FROM offer_versions v JOIN generation_offers m USING(version_id) WHERE m.generation_id=$1",
                        first,
                    )
                    == 2
                )
                with patch(
                    "service.db.psql.publish",
                    side_effect=RuntimeError("publication failed"),
                ):
                    with pytest.raises(RuntimeError, match="publication failed"):
                        await importer.import_directory(
                            write_day(tmp_path, "4.00"), False
                        )
                assert (
                    await db._fetchval("SELECT active_id FROM publication_state")
                    == second
                )
                assert await db._fetchval("SELECT regular_price FROM prices") == 3

    asyncio.run(run())


def test_older_import_preserves_latest_and_missing_branch(database, tmp_path):
    from tests.service.import_fixtures import rewrite
    from service.db.search import search

    async def run():
        async with database() as db:
            await db.create_tables()
            with patch.object(importer, "db", db):
                day = write_day(tmp_path, "2.00", "Mlijeko")
                rewrite(
                    day / "test" / "stores.csv",
                    lambda rows: rows.append({"store_id": "02", "city": "Other"}),
                )
                rewrite(
                    day / "test" / "prices.csv",
                    lambda rows: rows.append(
                        {**rows[0], "store_id": "02", "price": "0.40"}
                    ),
                )
                await importer.import_directory(day, False)
                await importer.import_directory(
                    write_day(tmp_path, "2.00", "Mlijeko"), False
                )
                result = await search(db, q="mlijeko")
                assert result["products"][0]["avg_price"] == Decimal("1.20")
                assert result["products"][0]["shop_count"] == 2
                older = tmp_path / "2026-11-13"
                day.rename(older)
                rewrite(
                    older / "test" / "products.csv",
                    lambda rows: rows[0].update(name="Old name"),
                )
                await importer.import_directory(older, False)
                assert (await search(db, q="mlijeko"))["products"][0]["chains"][0][
                    "name"
                ] == "Mlijeko"
                assert (await search(db, q="mlijeko"))["products"][0]["shop_count"] == 2

    from decimal import Decimal

    asyncio.run(run())


def test_canonical_conflict_rejects_publication(database, tmp_path):
    async def run():
        async with database() as db:
            await db.create_tables()
            with patch.object(importer, "db", db):
                await importer.import_directory(write_day(tmp_path), False)
            generation = await db._fetchval("SELECT active_id FROM publication_state")
            with pytest.raises(ValueError, match="Conflicting canonical"):
                async with db.import_transaction():
                    alias = await db._fetchval(
                        "INSERT INTO chain_products(chain_id,product_id,code,name,unit,quantity) SELECT chain_id,product_id,'alias',name,unit,quantity FROM chain_products RETURNING id"
                    )
                    await db._fetchval(
                        "INSERT INTO prices(chain_product_id,store_id,price_date,regular_price) VALUES($1,1,'2026-11-14',1) RETURNING id",
                        alias,
                    )
            assert (
                await db._fetchval("SELECT active_id FROM publication_state")
                == generation
            )
            assert await db._fetchval("SELECT count(*) FROM chain_products") == 1

    asyncio.run(run())


def test_metadata_source_date_without_new_offer(database, tmp_path):
    import shutil
    from tests.service.import_fixtures import rewrite

    async def run():
        async with database() as db:
            await db.create_tables()
            original = write_day(tmp_path, "1.00", "June one")
            first = original.rename(tmp_path / "2026-06-01")
            newer = tmp_path / "2026-06-10"
            shutil.copytree(first, newer)
            rewrite(
                newer / "test/products.csv",
                lambda rows: rows[0].update(name="June ten"),
            )
            rewrite(
                newer / "test/stores.csv", lambda rows: rows[0].update(city="June ten")
            )
            prices = newer / "test/prices.csv"
            prices.write_text(prices.read_text().splitlines()[0] + "\n")
            older = tmp_path / "2026-06-05"
            shutil.copytree(first, older)
            with patch.object(importer, "db", db):
                for day in (first, newer, older):
                    await importer.import_directory(day, False)
            assert await db._fetchval("SELECT name FROM chain_products") == "June ten"
            assert await db._fetchval("SELECT city FROM stores") == "June ten"
            for table, field in (("store", "city"), ("chain_product", "name")):
                assert (
                    await db._fetchval(
                        f"SELECT v.{field} FROM publication_state ps JOIN generation_{table}s m ON m.generation_id=ps.active_id JOIN {table}_versions v USING(version_id)"
                    )
                    == "June ten"
                )
                assert (
                    str(
                        await db._fetchval(
                            f"SELECT v.metadata_source_date FROM publication_state ps JOIN generation_{table}s m ON m.generation_id=ps.active_id JOIN {table}_versions v USING(version_id)"
                        )
                    )
                    == "2026-06-10"
                )

            assert (
                str(await db._fetchval("SELECT metadata_source_date FROM stores"))
                == "2026-06-10"
            )

    asyncio.run(run())


@pytest.mark.parametrize("kind", ["product", "store"])
def test_direct_metadata_update_serializes_with_first_publication(database, kind):
    from service.db.generations import publish
    from service.db.models import Product

    async def run():
        async with database() as db:
            await db.create_tables()
            await db._fetchval(
                "INSERT INTO products(ean,name) VALUES('1','Before') RETURNING id"
            )
            await db._fetchval("INSERT INTO chains(code) VALUES('test') RETURNING id")
            await db._fetchval(
                "INSERT INTO stores(chain_id,code,city) VALUES(1,'1','Before') RETURNING id"
            )
            start = asyncio.Event()

            async def update():
                await start.wait()
                if kind == "store":
                    return await db.update_store(1, "1", city="After")
                return await db.update_product(Product(ean="1", name="After"))

            worker = asyncio.create_task(update())
            try:
                async with db._writer_transaction("import") as conn:
                    start.set()

                    async def waiting_for_writer():
                        while not await conn.fetchval(
                            "SELECT EXISTS(SELECT 1 FROM pg_locks WHERE locktype='advisory' AND objid=73198421 AND NOT granted)"
                        ):
                            assert not worker.done()
                            await asyncio.sleep(0.01)

                    await asyncio.wait_for(waiting_for_writer(), 5)
                    assert not worker.done()
                    await publish(conn)
                with pytest.raises(RuntimeError, match="import_transaction"):
                    await asyncio.wait_for(worker, 5)
                assert (
                    await db._fetchval("SELECT name FROM products WHERE ean='1'")
                    == "Before"
                )
                assert (
                    await db._fetchval("SELECT city FROM stores WHERE code='1'")
                    == "Before"
                )
            finally:
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)

    asyncio.run(run())
