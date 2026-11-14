from tests.service.import_fixtures import write_day
import asyncio
import csv
import importlib
from unittest.mock import patch

import pytest

importer = importlib.import_module("service.db.import")


def test_correction_and_replay(database, tmp_path):
    async def run():
        async with database() as db:
            await db.create_tables()
            with patch.object(importer, "db", db):
                day = write_day(tmp_path)
                await importer.import_directory(day, False)
                write_day(tmp_path, "3.00", "Corrected")
                await importer.import_directory(day, False)
                await importer.import_directory(day, False)
            async with db._get_conn() as conn:
                assert (
                    await conn.fetchval("SELECT name FROM chain_products")
                    == "Corrected"
                )
                assert await conn.fetchval("SELECT regular_price FROM prices") == 3
                assert await conn.fetchval("SELECT count(*) FROM prices") == 1
                assert await conn.fetchval("SELECT available FROM prices") is False

    asyncio.run(run())


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-1", "1.001", "", "100000000"])
def test_invalid_price_is_atomic(database, tmp_path, value):
    async def run():
        async with database() as db:
            await db.create_tables()
            with patch.object(importer, "db", db):
                with pytest.raises(ValueError):
                    await importer.import_directory(write_day(tmp_path, value), False)
            assert await db._fetchval("SELECT count(*) FROM chains") == 0

    asyncio.run(run())


@pytest.mark.parametrize("failure", ["stats", "chain"])
def test_write_failure_rolls_back(database, tmp_path, failure):
    async def run():
        from unittest.mock import AsyncMock

        async with database() as db:
            await db.create_tables()
            day = write_day(tmp_path)
            with patch.object(importer, "db", db):
                if failure == "stats":
                    context = patch.object(
                        importer,
                        "compute_stats",
                        AsyncMock(side_effect=RuntimeError("stats failed")),
                    )
                else:
                    original = importer.process_chain

                    async def fail_after(*args):
                        await original(*args)
                        raise RuntimeError("later chain failed")

                    context = patch.object(importer, "process_chain", fail_after)
                with context, pytest.raises(RuntimeError):
                    await importer.import_directory(day)
            assert await db._fetchval("SELECT count(*) FROM prices") == 0
            assert await db._fetchval("SELECT count(*) FROM chain_products") == 0
            assert await db._fetchval("SELECT count(*) FROM chains") == 0

    asyncio.run(run())


@pytest.mark.parametrize(
    "field", ["special_price", "unit_price", "best_price_30", "anchor_price"]
)
@pytest.mark.parametrize("value", ["NaN", "-0.01", "1.001", "Infinity", "bad"])
def test_invalid_optional(database, tmp_path, field, value):
    async def run():
        async with database() as db:
            await db.create_tables()
            day = write_day(tmp_path)
            path = day / "test" / "prices.csv"
            with path.open() as stream:
                rows = list(csv.DictReader(stream))
            rows[0][field] = value
            with path.open("w") as stream:
                writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
                writer.writeheader()
                writer.writerows(rows)
            with patch.object(importer, "db", db), pytest.raises(ValueError):
                await importer.import_directory(day, False)
            assert await db._fetchval("SELECT count(*) FROM chains") == 0

    asyncio.run(run())


def test_full_metadata_global_link_and_http(database, service_client, tmp_path):
    from secrets import token_hex
    from tests.service.import_fixtures import rewrite

    async def run():
        async with database() as db:
            async with service_client(db) as client:
                day = write_day(tmp_path)
                with patch.object(importer, "db", db):
                    await importer.import_directory(day)
                    old_id = await db._fetchval("SELECT id FROM chain_products")
                    rewrite(
                        day / "test" / "products.csv",
                        lambda rows: rows[0].update(
                            name="Changed",
                            barcode="test:001",
                            brand="New brand",
                            category="New category",
                            quantity="2",
                            unit="L",
                        ),
                    )
                    rewrite(
                        day / "test" / "prices.csv",
                        lambda rows: rows[0].update(price="5", special_price="4"),
                    )
                    await importer.import_directory(day)
                    await importer.import_directory(day)
                async with db._get_conn() as conn:
                    row = await conn.fetchrow(
                        "SELECT cp.*, p.ean FROM chain_products cp JOIN products p ON p.id=cp.product_id"
                    )
                    assert row["id"] == old_id
                    assert row["code"] == "001"
                    assert row["ean"] == "test:001"
                    assert [
                        row[key]
                        for key in ("name", "brand", "category", "quantity", "unit")
                    ] == ["Changed", "New brand", "New category", "2", "L"]
                    token = token_hex(32)
                    await conn.execute(
                        "INSERT INTO users(name,api_key) VALUES ('fixture',$1)", token
                    )
                response = await client.get(
                    "/v1/products/test:001/",
                    headers={"Authorization": f"Bearer {token}"},
                )
                assert response.status_code == 200
                assert "Changed" in response.text
                prices = await db.get_product_prices(
                    [row["product_id"]], __import__("datetime").date(2026, 11, 14)
                )
                assert prices[0]["min_price"] == 4

    asyncio.run(run())


def test_provenance_upgrade(database):
    async def run():
        async with database() as db:
            await db.create_tables()
            async with db._get_conn() as conn:
                await conn.execute(
                    "ALTER TABLE prices DROP COLUMN available, DROP COLUMN anchor_price_date, DROP COLUMN special_sale_type"
                )
            await db.create_tables()
            await db.create_tables()
            async with db._get_conn() as conn:
                columns = await conn.fetch(
                    "SELECT column_name FROM information_schema.columns WHERE table_name='prices'"
                )
                assert {"available", "anchor_price_date", "special_sale_type"} <= {
                    row["column_name"] for row in columns
                }

    asyncio.run(run())


@pytest.mark.parametrize("kind", ["products", "prices"])
@pytest.mark.parametrize("conflicting", [False, True])
@pytest.mark.parametrize("reverse", [False, True])
def test_bulk_duplicate_keys(database, tmp_path, kind, conflicting, reverse):
    from dataclasses import replace
    from datetime import date
    from decimal import Decimal

    from service.db.models import ChainProduct, Price

    async def run():
        async with database() as db:
            await db.create_tables()
            with patch.object(importer, "db", db):
                await importer.import_directory(write_day(tmp_path))
            async with db._get_conn() as conn:
                product = await conn.fetchrow("SELECT * FROM chain_products")
                store_id = await conn.fetchval("SELECT id FROM stores")
            if kind == "products":
                row = ChainProduct(
                    chain_id=product["chain_id"],
                    product_id=product["product_id"],
                    code=product["code"],
                    name="Updated",
                )
                other = replace(row, name="Conflicting") if conflicting else row
                operation = db.add_many_chain_products
                query = "SELECT name FROM chain_products"
                before, after = "Original", "Updated"
            else:
                row = Price(product["id"], store_id, date(2026, 11, 14), Decimal("4"))
                other = replace(row, regular_price=Decimal("5")) if conflicting else row
                operation = db.add_many_prices
                query = "SELECT regular_price FROM prices"
                before, after = Decimal("2"), Decimal("4")
            rows = [other, row] if reverse else [row, other]
            if conflicting:
                with pytest.raises(ValueError, match="Conflicting duplicate batch row"):
                    await operation(rows)
                assert await db._fetchval(query) == before
            else:
                assert await operation(rows) == 1
                assert await db._fetchval(query) == after

    asyncio.run(run())
