import asyncio
from unittest.mock import patch

import pytest

from tests.service.import_fixtures import importer, write_day


def test_import_correction_replay_and_failure(database, tmp_path):
    async def run():
        async with database() as db:
            await db.create_tables()

            async def epoch():
                return await db._fetchval("SELECT epoch FROM import_state")

            async def latest_price():
                return await db._fetchval("SELECT regular_price FROM latest_offers")

            with patch.object(importer, "db", db):
                day = write_day(tmp_path)
                await importer.import_directory(day, False)
                first = await epoch()
                assert first > 0 and await latest_price() == 2

                write_day(tmp_path, "3.00", "Corrected")
                await importer.import_directory(day, False)
                second = await epoch()
                assert second > first and await latest_price() == 3

                await importer.import_directory(day, False)
                assert await epoch() == second

                with patch.object(
                    db,
                    "record_metadata_source_date",
                    side_effect=RuntimeError("import failed"),
                ):
                    with pytest.raises(RuntimeError, match="import failed"):
                        await importer.import_directory(
                            write_day(tmp_path, "4.00"), False
                        )
                assert await epoch() == second
                assert await latest_price() == 3
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


def test_duplicate_chain_codes_pick_newest_offer(database, tmp_path):
    from decimal import Decimal

    from service.db.search import latest_prices, search

    async def run():
        async with database() as db:
            await db.create_tables()
            with patch.object(importer, "db", db):
                await importer.import_directory(write_day(tmp_path), False)
            async with db.import_transaction():
                alias = await db._fetchval(
                    "INSERT INTO chain_products(chain_id,product_id,code,name,unit,quantity) SELECT chain_id,product_id,'alias',name,unit,quantity FROM chain_products RETURNING id"
                )
                await db._fetchval(
                    "INSERT INTO latest_offers(chain_product_id,store_id,price_date,regular_price) VALUES($1,1,'2026-11-15',1) RETURNING store_id",
                    alias,
                )
            product = (await search(db, q="original"))["products"][0]
            assert product["shop_count"] == 1
            assert product["min_price"] == Decimal("1.00")
            ean = await db._fetchval("SELECT ean FROM products")
            prices = await latest_prices(db, {ean}, lambda stores: stores)
            assert [row["regular_price"] for row in prices] == [Decimal("1.00")]
            older_alias = await search(db, q="original", category=["Food"])
            assert older_alias["total"] == 0

    asyncio.run(run())


def test_importer_keeps_two_codes_of_one_barcode(database, tmp_path, caplog):
    from decimal import Decimal

    from tests.service.import_fixtures import rewrite
    from service.db.search import search

    async def run():
        async with database() as db:
            await db.create_tables()
            day = write_day(tmp_path, "14.10", "Vino cuvee")
            rewrite(
                day / "test" / "products.csv",
                lambda rows: rows.append(
                    dict(rows[0], product_id="002", barcode="12345678")
                ),
            )
            rewrite(
                day / "test" / "prices.csv",
                lambda rows: rows.append(
                    dict(rows[0], product_id="002", price="14.05")
                ),
            )
            with patch.object(importer, "db", db):
                await importer.import_directory(day, False)

            assert await db._fetchval("SELECT count(*) FROM latest_offers") == 2
            assert "1 shops list one barcode under several chain codes" in caplog.text
            product = (await search(db, q="vino"))["products"][0]
            assert product["shop_count"] == 1
            assert product["min_price"] == Decimal("14.10")

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
            assert (
                str(
                    await db._fetchval(
                        "SELECT metadata_source_date FROM chain_products"
                    )
                )
                == "2026-06-10"
            )
            assert str(await db._fetchval("SELECT price_date FROM latest_offers")) == (
                "2026-06-05"
            )

            assert (
                str(await db._fetchval("SELECT metadata_source_date FROM stores"))
                == "2026-06-10"
            )

    asyncio.run(run())


@pytest.mark.parametrize("kind", ["product", "store"])
def test_direct_metadata_update_waits_for_writer_and_moves_epoch(database, kind):
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
            before = await db._fetchval("SELECT epoch FROM import_state")
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
                assert await asyncio.wait_for(worker, 5) is True
                table, column = (
                    ("stores", "city") if kind == "store" else ("products", "name")
                )
                assert await db._fetchval(f"SELECT {column} FROM {table}") == "After"
                assert await db._fetchval("SELECT epoch FROM import_state") == (
                    before + 1
                )
            finally:
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)

    asyncio.run(run())


def test_one_batch_with_two_dates_keeps_the_newest_latest_offer(database):
    from datetime import date
    from decimal import Decimal

    from service.db.models import Price

    async def run():
        async with database() as db:
            await db.create_tables()
            async with db.import_transaction():
                async with db._get_conn() as conn:
                    await conn.execute("""
                        INSERT INTO chains(code) VALUES('test');
                        INSERT INTO stores(chain_id,code) VALUES(1,'1');
                        INSERT INTO products(ean) VALUES('1');
                        INSERT INTO chain_products(chain_id,product_id,code,name) VALUES(1,1,'1','Mlijeko');
                    """)
                await db.add_many_prices(
                    [
                        Price(1, 1, date(2026, 11, 14), Decimal("2.00")),
                        Price(1, 1, date(2026, 11, 13), Decimal("1.00")),
                    ]
                )
            assert await db._fetchval("SELECT count(*) FROM prices") == 2
            latest = await db._fetchval(
                "SELECT price_date::text || ' ' || regular_price FROM latest_offers"
            )
            assert latest == "2026-11-14 2.00"

    asyncio.run(run())


def test_partial_marks_carried_forward_offers(database, tmp_path):
    import shutil

    from tests.service.import_fixtures import rewrite
    from service.db.search import search

    async def run():
        async with database() as db:
            await db.create_tables()
            first = write_day(tmp_path, "2.00", "Mlijeko").rename(
                tmp_path / "2026-11-13"
            )
            rewrite(
                first / "test" / "stores.csv",
                lambda rows: rows.append({"store_id": "02", "city": "Other"}),
            )
            rewrite(
                first / "test" / "prices.csv",
                lambda rows: rows.append(dict(rows[0], store_id="02")),
            )
            with patch.object(importer, "db", db):
                await importer.import_directory(first, False)
                complete = await search(db, q="mlijeko")
                assert complete["partial"] is False
                assert complete["products"][0]["partial"] is False

                second = tmp_path / "2026-11-14"
                shutil.copytree(first, second)
                rewrite(second / "test" / "prices.csv", lambda rows: rows.pop())
                await importer.import_directory(second, False)

            carried = await search(db, q="mlijeko")
            product = carried["products"][0]
            assert carried["partial"] is True and product["partial"] is True
            assert [chain["partial"] for chain in product["chains"]] == [True]
            assert (await search(db, q="mlijeko", city=["City"]))["partial"] is False

    asyncio.run(run())
