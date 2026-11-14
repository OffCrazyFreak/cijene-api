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
