import csv
import importlib

importer = importlib.import_module("service.db.import")


def write_day(tmp_path, price="2.00", name="Original"):
    day = tmp_path / "2026-11-14"
    chain = day / "test"
    chain.mkdir(parents=True, exist_ok=True)
    tables = {
        "stores": [{"store_id": "01", "city": "City"}],
        "products": [
            {
                "product_id": "001",
                "barcode": "00012345678",
                "name": name,
                "brand": "Brand",
                "category": "Food",
                "quantity": "1",
                "unit": "kg",
            }
        ],
        "prices": [
            {
                "product_id": "001",
                "store_id": "01",
                "price": price,
                "special_price": "",
                "unit_price": "",
                "best_price_30": "",
                "anchor_price": "",
                "available": "false",
                "anchor_price_date": "2025-05-02",
                "special_sale_type": "promo",
            }
        ],
    }
    for table, rows in tables.items():
        with (chain / f"{table}.csv").open("w") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
    return day


def rewrite(path, change):
    with path.open() as stream:
        rows = list(csv.DictReader(stream))
    change(rows)
    with path.open("w") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
