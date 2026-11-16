import asyncio
import shutil
from decimal import Decimal
from unittest.mock import patch

import pytest

from tests.service.import_fixtures import importer, write_day, rewrite


@pytest.mark.parametrize(
    "fault",
    [
        "header",
        "missing",
        "directory",
        "row",
        "reference",
        "date",
        "available",
        "late_chain",
    ],
)
def test_bad_input_does_not_mutate(database, tmp_path, fault):
    async def run():
        async with database() as db:
            await db.create_tables()
            day = write_day(tmp_path)
            prices = day / "test" / "prices.csv"
            if fault == "header":
                prices.write_text("wrong\nvalue\n")
            elif fault == "missing":
                prices.unlink()
            elif fault == "directory":
                prices.unlink()
                prices.mkdir()
            elif fault == "row":
                with prices.open("a") as stream:
                    stream.write("bad,row\n")
            elif fault == "reference":
                rewrite(prices, lambda rows: rows[0].update(product_id="missing"))
            elif fault == "date":
                rewrite(
                    prices, lambda rows: rows[0].update(anchor_price_date="2025-02-30")
                )
            elif fault == "available":
                rewrite(prices, lambda rows: rows[0].update(available="maybe"))
            else:
                shutil.copytree(day / "test", day / "zzz")
                (day / "zzz" / "products.csv").unlink()
            with patch.object(importer, "db", db), pytest.raises(ValueError):
                await importer.import_directory(day, False)
            assert await db._fetchval("SELECT count(*) FROM chains") == 0

    asyncio.run(run())


@pytest.mark.parametrize("conflict", [False, True])
@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("alias", [False, True])
def test_duplicate_permutations(database, tmp_path, conflict, reverse, alias):
    async def run():
        async with database() as db:
            await db.create_tables()
            day = write_day(tmp_path)
            if alias:

                def add_alias(rows):
                    rows.append(dict(rows[0], product_id="002", barcode="12345678"))

                rewrite(day / "test" / "products.csv", add_alias)

            def duplicate(rows):
                rows.append(
                    dict(
                        rows[0],
                        product_id="002" if alias else "001",
                        available="true" if conflict else "false",
                    )
                )
                if reverse:
                    rows.reverse()

            rewrite(day / "test" / "prices.csv", duplicate)
            with patch.object(importer, "db", db):
                await importer.import_directory(day, False)
            skipped = conflict and not alias
            assert await db._fetchval("SELECT count(*) FROM prices") == (
                2 if alias else 0 if skipped else 1
            )

    asyncio.run(run())


async def fetch_all(db, query):
    async with db._get_conn() as conn:
        return [tuple(row) for row in await conn.fetch(query)]


def test_rows_differing_only_in_unit_price_merge(database, tmp_path):
    async def run():
        async with database() as db:
            await db.create_tables()
            day = write_day(tmp_path)

            def unit_prices(rows):
                first = dict(rows[0], unit_price="0.12")
                rows[:] = [
                    first,
                    dict(first, unit_price="0.17"),
                    dict(first, unit_price="0.18"),
                ]
                rows.append(dict(first, product_id="002", unit_price="0.50"))
                rows.append(dict(first, product_id="002", unit_price="0.50"))

            rewrite(
                day / "test" / "products.csv",
                lambda rows: rows.append(
                    dict(rows[0], product_id="002", barcode="22345678")
                ),
            )
            rewrite(day / "test" / "prices.csv", unit_prices)
            with patch.object(importer, "db", db):
                await importer.import_directory(day, False)
            rows = await fetch_all(
                db,
                "SELECT cp.code,p.unit_price FROM prices p JOIN chain_products cp ON cp.id=p.chain_product_id ORDER BY cp.code",
            )
            assert rows == [("001", None), ("002", Decimal("0.50"))]

    asyncio.run(run())


def test_conflicting_rows_skip_only_that_shop_and_day(database, tmp_path, caplog):
    async def run():
        async with database() as db:
            await db.create_tables()
            older = write_day(tmp_path, "2.00").rename(tmp_path / "2026-11-13")
            with patch.object(importer, "db", db):
                await importer.import_directory(older, False)

            day = write_day(tmp_path, "2.00")
            rewrite(
                day / "test" / "products.csv",
                lambda rows: rows.append(
                    dict(rows[0], product_id="002", barcode="22345678")
                ),
            )
            rewrite(
                day / "test" / "prices.csv",
                lambda rows: rows.extend(
                    [
                        dict(rows[0], price="1.49"),
                        dict(rows[0], product_id="002", price="3.00"),
                    ]
                ),
            )
            other = day / "other"
            shutil.copytree(day / "test", other)
            rewrite(
                other / "prices.csv",
                lambda rows: rows.append(
                    dict(rows[2], special_price="0.40", price="3.00")
                ),
            )
            with patch.object(importer, "db", db):
                await importer.import_directory(day, False)

            assert "other: skipped 2 product and shop pairs" in caplog.text
            assert "test: skipped 1 product and shop pairs" in caplog.text
            offers = await fetch_all(
                db,
                "SELECT c.code,cp.code,o.price_date::text,o.regular_price FROM latest_offers o JOIN chain_products cp ON cp.id=o.chain_product_id JOIN chains c ON c.id=cp.chain_id ORDER BY 1,2",
            )
            assert offers == [
                ("test", "001", "2026-11-13", Decimal("2.00")),
                ("test", "002", "2026-11-14", Decimal("3.00")),
            ]

    asyncio.run(run())


@pytest.mark.parametrize("available", ["", "0", "1", "true", "false"])
def test_zero_and_provenance(database, tmp_path, available):
    async def run():
        async with database() as db:
            await db.create_tables()
            day = write_day(tmp_path, "0")
            rewrite(
                day / "test" / "prices.csv",
                lambda rows: rows[0].update(
                    available=available,
                    special_price="0",
                    unit_price="0",
                    best_price_30="0",
                    anchor_price="0",
                ),
            )
            with patch.object(importer, "db", db):
                await importer.import_directory(day)
            async with db._get_conn() as conn:
                row = await conn.fetchrow("SELECT * FROM prices")
                assert row["regular_price"] == 0
                assert row["special_price"] is None
                assert row["anchor_price"] is None
                assert row["available"] is (
                    None if available == "" else available in ("1", "true")
                )
                assert row["price_date"].isoformat() == "2026-11-14"
                assert row["anchor_price_date"].isoformat() == "2025-05-02"
                assert row["special_sale_type"] == "promo"
            assert len(await db.list_latest_chain_stats()) == 1

    asyncio.run(run())


def test_failed_correction_keeps_previous_data(database, tmp_path):
    async def run():
        from unittest.mock import AsyncMock

        async with database() as db:
            await db.create_tables()
            day = write_day(tmp_path)
            with patch.object(importer, "db", db):
                await importer.import_directory(day)
                write_day(tmp_path, "9", "Should roll back")
                with (
                    patch.object(
                        db,
                        "compute_chain_stats",
                        AsyncMock(side_effect=RuntimeError("stats")),
                    ),
                    pytest.raises(RuntimeError),
                ):
                    await importer.import_directory(day)
            assert await db._fetchval("SELECT regular_price FROM prices") == 2
            assert await db._fetchval("SELECT name FROM chain_products") == "Original"
            assert await db._fetchval("SELECT min_price FROM chain_prices") == 2
            assert await db._fetchval("SELECT price_count FROM chain_stats") == 1

    asyncio.run(run())


def test_second_chain_failure_is_atomic(database, tmp_path):
    async def run():
        async with database() as db:
            await db.create_tables()
            day = write_day(tmp_path)
            shutil.copytree(day / "test", day / "zzz")
            original = importer.process_chain

            async def fail_later(price_date, chain, barcodes):
                await original(price_date, chain, barcodes)
                if chain.name == "zzz":
                    raise RuntimeError("second chain")

            with (
                patch.object(importer, "db", db),
                patch.object(importer, "process_chain", fail_later),
                pytest.raises(RuntimeError),
            ):
                await importer.import_directory(day)
            assert await db._fetchval("SELECT count(*) FROM chains") == 0

    asyncio.run(run())


def test_import_transaction_is_invisible_until_commit(database):
    async def run():
        async with database() as db:
            await db.create_tables()
            async with db.import_transaction():
                from service.db.models import Chain

                await db.add_chain(Chain(code="pending"))
                async with db.pool.acquire() as reader:
                    assert await reader.fetchval("SELECT count(*) FROM chains") == 0
            assert await db._fetchval("SELECT count(*) FROM chains") == 1

    asyncio.run(run())


@pytest.mark.parametrize(
    "first,second,expected",
    [
        ("00012345678", "12345678", 1),
        ("test:001", "test:002", 2),
        ("2012345678901", "02012345678901", 2),
    ],
)
def test_barcode_identity(database, tmp_path, first, second, expected):
    async def run():
        async with database() as db:
            await db.create_tables()
            day = write_day(tmp_path)

            def products(rows):
                rows[0]["barcode"] = first
                rows.append(dict(rows[0], product_id="002", barcode=second))

            rewrite(day / "test" / "products.csv", products)
            with patch.object(importer, "db", db):
                await importer.import_directory(day, False)
            assert (
                await db._fetchval(
                    "SELECT count(DISTINCT product_id) FROM chain_products"
                )
                == expected
            )

    asyncio.run(run())


@pytest.mark.parametrize("bound", ["MAX_FILE_BYTES", "MAX_INPUT_BYTES", "MAX_CHAINS"])
def test_input_bounds(database, tmp_path, bound):
    async def run():
        from service.db import input_rows

        async with database() as db:
            await db.create_tables()
            day = write_day(tmp_path)
            with (
                patch.object(importer, "db", db),
                patch.object(input_rows, bound, 0),
                pytest.raises(ValueError),
            ):
                await importer.import_directory(day, False)
            assert await db._fetchval("SELECT count(*) FROM chains") == 0

    asyncio.run(run())


def test_unreadable_input(database, tmp_path):
    async def run():
        async with database() as db:
            await db.create_tables()
            day = write_day(tmp_path)
            original = type(day).open

            def open_file(path, *args, **kwargs):
                if path == day / "test" / "prices.csv":
                    raise PermissionError("fixture read failure")
                return original(path, *args, **kwargs)

            with (
                patch.object(importer, "db", db),
                patch.object(type(day), "open", open_file),
                pytest.raises(ValueError, match="Cannot read CSV"),
            ):
                await importer.import_directory(day, False)
            assert await db._fetchval("SELECT count(*) FROM chains") == 0

    asyncio.run(run())


@pytest.mark.parametrize("kind", ["bad_quote", "oversized_line", "barcode"])
def test_parser_bounds_before_writes(database, tmp_path, kind):
    async def run():
        from service.db import input_rows

        async with database() as db:
            await db.create_tables()
            day = write_day(tmp_path)
            if kind == "barcode":
                rewrite(
                    day / "test" / "products.csv",
                    lambda rows: rows[0].update(barcode="", product_id="x" * 100),
                )
            elif kind == "bad_quote":
                with (day / "test" / "prices.csv").open("a") as stream:
                    stream.write('"unterminated')
            with (
                patch.object(importer, "db", db),
                patch.object(
                    input_rows,
                    "MAX_LINE_CHARACTERS",
                    4 if kind == "oversized_line" else 1024**2,
                ),
                pytest.raises(ValueError),
            ):
                await importer.import_directory(day, False)
            assert await db._fetchval("SELECT count(*) FROM chains") == 0

    asyncio.run(run())
