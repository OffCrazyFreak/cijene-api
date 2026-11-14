#!/usr/bin/env python3
import argparse
import asyncio
import logging
from datetime import date
from itertools import batched
from pathlib import Path
from tempfile import TemporaryDirectory

from service.config import settings
from service.db.archive_input import safe_extract
from service.db.input_rows import (
    BATCH_SIZE,
    availability,
    barcode,
    iter_rows,
    parse_money,
    prepare_input,
    source_date,
)
from service.db.models import Chain, ChainProduct, Price, Store
from service.db.stats import compute_stats

logger = logging.getLogger("importer")
db = settings.get_db()


async def process_stores(stores_path: Path, chain_id: int) -> dict[str, int]:
    result = {}
    for row in iter_rows(stores_path):
        store = Store(
            chain_id=chain_id,
            code=row["store_id"],
            type=row.get("type"),
            address=row.get("address"),
            city=row.get("city"),
            zipcode=row.get("zipcode"),
        )
        result[store.code] = await db.add_store(store)
    return result


async def process_products(
    products_path: Path, chain_id: int, chain_code: str, barcodes: dict[str, int]
) -> dict[str, int]:
    for batch in batched(iter_rows(products_path), BATCH_SIZE):
        products = []
        for row in batch:
            ean = barcode(row, chain_code)
            if ean not in barcodes:
                barcodes[ean] = await db.add_ean(ean)
            products.append(
                ChainProduct(
                    chain_id=chain_id,
                    product_id=barcodes[ean],
                    code=row["product_id"],
                    name=row["name"],
                    brand=row["brand"].strip() or None,
                    category=row["category"].strip() or None,
                    unit=row["unit"].strip() or None,
                    quantity=row["quantity"].strip() or None,
                )
            )
        await db.add_many_chain_products(products)
    return await db.get_chain_product_map(chain_id)


async def process_prices(
    price_date: date,
    prices_path: Path,
    chain_id: int,
    store_map: dict[str, int],
    chain_product_map: dict[str, int],
) -> int:
    count = 0
    for batch in batched(iter_rows(prices_path), BATCH_SIZE):
        prices = []
        for row in batch:
            prices.append(
                Price(
                    chain_product_id=chain_product_map[row["product_id"]],
                    store_id=store_map[row["store_id"]],
                    price_date=price_date,
                    regular_price=parse_money(row["price"], True),
                    special_price=parse_money(row.get("special_price", "")),
                    unit_price=parse_money(row["unit_price"]),
                    best_price_30=parse_money(row["best_price_30"]),
                    anchor_price=parse_money(row["anchor_price"]),
                    available=availability(row.get("available", "")),
                    anchor_price_date=source_date(row["anchor_price_date"])
                    if row.get("anchor_price_date")
                    else None,
                    special_sale_type=row.get("special_sale_type") or None,
                )
            )
        count += await db.add_many_prices(prices)
    return count


async def process_chain(
    price_date: date, chain_dir: Path, barcodes: dict[str, int]
) -> None:
    chain_id = await db.add_chain(Chain(code=chain_dir.name))
    stores = await process_stores(chain_dir / "stores.csv", chain_id)
    products = await process_products(
        chain_dir / "products.csv", chain_id, chain_dir.name, barcodes
    )
    await process_prices(
        price_date, chain_dir / "prices.csv", chain_id, stores, products
    )


async def import_archive(path: Path, compute_stats_flag: bool = True) -> None:
    price_date = source_date(path.stem)
    with TemporaryDirectory() as directory:
        safe_extract(path, Path(directory))
        await _import(Path(directory), price_date, compute_stats_flag)


async def import_directory(path: Path, compute_stats_flag: bool = True) -> None:
    if path.is_symlink() or not path.is_dir():
        raise ValueError("Input directory is missing or unsafe")
    await _import(path, source_date(path.name), compute_stats_flag)


async def _import(
    path: Path, price_date: date, compute_stats_flag: bool = True
) -> None:
    with TemporaryDirectory() as directory:
        snapshot = Path(directory)
        prepare_input(path, snapshot)
        async with db.import_transaction(price_date):
            barcodes = await db.get_product_barcodes()
            for chain in sorted(snapshot.iterdir()):
                if chain.is_dir():
                    await process_chain(price_date, chain, barcodes)
            if compute_stats_flag:
                await compute_stats(price_date, database=db)


async def main():
    """
    Import price data from directories or zip archives.

    This script expects the directories to be named in the format YYYY-MM-DD,
    containing subdirectories for each retail chain. Each chain directory
    should contain CSV files named `stores.csv`, `products.csv`, and `prices.csv`.
    The CSV files should follow the structure documented in
    `crawler/store/output.py`.

    Zip archives should be named YYYY-MM-DD.zip and contain the same resources
    as directories described above.

    Database connection settings are loaded from the service configuration, see
    `service/config.py` for details.
    """
    parser = argparse.ArgumentParser(
        description=main.__doc__,
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser.add_argument(
        "paths",
        type=Path,
        help="One or more directories or zip archives containing price data",
        nargs="+",
    )
    parser.add_argument(
        "-s",
        "--skip-stats",
        action="store_true",
        help="Skip computing chain stats",
    )
    parser.add_argument(
        "-d",
        "--debug",
        action="store_true",
        help="Enable debug logging",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s:%(name)s:%(levelname)s:%(message)s",
    )

    compute_stats_flag = not args.skip_stats

    await db.connect()

    try:
        await db.create_tables()

        for path in args.paths:
            if path.is_dir():
                await import_directory(path, compute_stats_flag)
            elif path.suffix.lower() == ".zip":
                await import_archive(path, compute_stats_flag)
            else:
                logger.error(f"Path `{path}` is neither a directory nor a zip archive.")
    finally:
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())
