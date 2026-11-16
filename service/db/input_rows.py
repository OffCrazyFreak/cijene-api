"""Disk-backed validation of normalized inputs before database mutation."""

import csv
import json
import logging
import re
import sqlite3
from contextlib import closing
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Iterator

from common.barcodes import normalize_barcode

logger = logging.getLogger(__name__)

MAX_FILE_BYTES = 2 * 1024**3
MAX_INPUT_BYTES = 8 * 1024**3
MAX_CHAINS = 100
MAX_FIELD_BYTES = 65536
MAX_COLUMNS = 64
MAX_LINE_CHARACTERS = 1024**2
BATCH_SIZE = 1000
REQUIRED = {
    "stores": {"store_id"},
    "products": {
        "product_id",
        "name",
        "barcode",
        "brand",
        "category",
        "quantity",
        "unit",
    },
    "prices": {
        "product_id",
        "store_id",
        "price",
        "unit_price",
        "best_price_30",
        "anchor_price",
    },
}
MONEY_FIELDS = ("price", "special_price", "unit_price", "best_price_30", "anchor_price")


def source_date(value: str) -> date:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("Expected calendar date YYYY-MM-DD")
    return date.fromisoformat(value)


def parse_money(value: str, required: bool = False) -> Decimal | None:
    try:
        if not value.strip() and not required:
            return None
        number = Decimal(value)
        if not number.is_finite() or number < 0 or number >= 100000000:
            raise ValueError("Price outside finite nonnegative DECIMAL(10,2) range")
        if number != number.quantize(Decimal("0.01")):
            raise ValueError("Price exceeds currency precision")
        return number if required or number else None
    except InvalidOperation as exc:
        raise ValueError("Invalid price") from exc


def barcode(row: dict[str, str], chain: str) -> str:
    value = row["barcode"].strip()
    if ":" in value:
        return value
    if len(value) >= 8 and value.isdigit():
        return normalize_barcode(value) or value
    return f"{chain}:{row['product_id']}"


def availability(value: str) -> bool | None:
    if value == "" or value.lower() == "unknown":
        return None
    if value.lower() not in ("true", "false", "0", "1"):
        raise ValueError("Availability must be true, false or unknown")
    return value.lower() in ("true", "1")


def bounded_lines(stream) -> Iterator[str]:
    total = 0
    while line := stream.readline(MAX_LINE_CHARACTERS + 1):
        total += len(line.encode("utf-8"))
        if len(line) > MAX_LINE_CHARACTERS or total > MAX_FILE_BYTES:
            raise ValueError("CSV exceeds streaming bounds")
        yield line


def iter_rows(path: Path) -> Iterator[dict[str, str]]:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
        raise ValueError(f"Invalid or oversized CSV: {path.name}")
    try:
        with path.open(encoding="utf-8", newline="") as stream:
            reader = csv.DictReader(bounded_lines(stream), strict=True)
            headers = reader.fieldnames
            if (
                not headers
                or len(headers) > MAX_COLUMNS
                or len(headers) != len(set(headers))
            ):
                raise ValueError("Missing or duplicate CSV headers")
            if not REQUIRED[path.stem].issubset(headers):
                raise ValueError(f"Missing required headers in {path.name}")
            for row in reader:
                if None in row or None in row.values():
                    raise ValueError("CSV row width differs from header")
                if any(len(value.encode()) > MAX_FIELD_BYTES for value in row.values()):
                    raise ValueError("CSV field exceeds limit")
                yield row
    except (OSError, UnicodeError, csv.Error) as exc:
        raise ValueError(f"Cannot read CSV: {path.name}") from exc


def remember(index: sqlite3.Connection, kind: str, key: str, row: dict) -> bool:
    """True for the first row of a key; raises when a repeated row differs."""
    payload = json.dumps(row, sort_keys=True)
    if index.execute(
        "INSERT OR IGNORE INTO entries VALUES (?,?,?)", (kind, key, payload)
    ).rowcount:
        return True
    previous = index.execute(
        "SELECT payload FROM entries WHERE kind=? AND key=?", (kind, key)
    ).fetchone()[0]
    if previous != payload:
        raise ValueError("Conflicting duplicate source row")
    return False


def remember_first(index: sqlite3.Connection, key: str, row: dict) -> bool:
    return bool(
        index.execute(
            "INSERT OR IGNORE INTO entries VALUES ('prices',?,?)",
            (key, json.dumps(row, sort_keys=True)),
        ).rowcount
    )


def merge_offer(kept: dict, row: dict) -> dict | None:
    """One offer from rows that differ at most in unit price, or None to skip."""
    if {k: v for k, v in kept.items() if k != "unit_price"} != {
        k: v for k, v in row.items() if k != "unit_price"
    }:
        return None
    if kept.get("unit_price", "") != row.get("unit_price", ""):
        return {**kept, "unit_price": ""}
    return kept


class RepeatedOffers:
    """Resolution of price keys that one chain file lists more than once."""

    def __init__(self) -> None:
        self.resolved: dict[str, dict | None] = {}
        self.merged: set[str] = set()

    def add(self, index: sqlite3.Connection, key: str, row: dict) -> None:
        current = self.resolved.get(key)
        if key not in self.resolved:
            current = json.loads(
                index.execute(
                    "SELECT payload FROM entries WHERE kind='prices' AND key=?", (key,)
                ).fetchone()[0]
            )
        if current is None:
            return

        merged = merge_offer(current, row)
        if merged is current:
            return
        self.resolved[key] = merged
        if merged is None:
            self.merged.discard(key)
        else:
            self.merged.add(key)

    def apply(self, prices: Path, chain: str) -> None:
        """Rewrite the snapshot's first occurrences with each key's resolution."""
        if not self.resolved:
            return

        rewritten = prices.with_suffix(".resolved")
        with (
            prices.open(encoding="utf-8", newline="") as source,
            rewritten.open("w", encoding="utf-8", newline="") as output,
        ):
            reader = csv.DictReader(source)
            writer = csv.DictWriter(output, fieldnames=list(reader.fieldnames or []))
            writer.writeheader()
            for row in reader:
                key = offer_key(row)
                if key in self.resolved:
                    resolution = self.resolved[key]
                    if resolution is None:
                        continue
                    row = resolution
                writer.writerow(row)
        rewritten.replace(prices)

        skipped = sum(resolution is None for resolution in self.resolved.values())
        logger.warning(
            f"{chain}: merged {len(self.merged):,} and skipped {skipped:,} "
            "product and shop pairs with repeated rows for one date"
        )


def offer_key(row: dict) -> str:
    return json.dumps([row["product_id"], row["store_id"]])


def prepare_input(path: Path, destination: Path) -> None:
    chains = []
    for entry in sorted(path.iterdir()):
        if entry.name == "archive-info.txt":
            if entry.is_symlink() or not entry.is_file():
                raise ValueError("Archive notice must be a regular file")
            if entry.stat().st_size > MAX_FILE_BYTES:
                raise ValueError("Archive notice exceeds byte limit")
            continue
        chains.append(entry)
    if not chains or len(chains) > MAX_CHAINS:
        raise ValueError("Input must contain bounded chain directories")
    total = 0
    with closing(sqlite3.connect(destination / "validation.sqlite")) as index, index:
        # The index is private scratch data, so crash safety buys nothing here.
        index.execute("PRAGMA journal_mode=OFF")
        index.execute("PRAGMA synchronous=OFF")
        index.execute("PRAGMA cache_size=-131072")
        index.execute(
            "CREATE TABLE entries (kind TEXT, key TEXT, payload TEXT, PRIMARY KEY(kind,key))"
        )
        for chain in chains:
            if chain.is_symlink() or not chain.is_dir() or len(chain.name) > 50:
                raise ValueError("Invalid chain directory")
            target = destination / chain.name
            target.mkdir()
            index.execute("DELETE FROM entries")
            known: dict[str, set[str]] = {"stores": set(), "products": set()}
            repeated = RepeatedOffers()
            for kind in REQUIRED:
                source = chain / f"{kind}.csv"
                try:
                    total += source.stat().st_size
                except OSError as exc:
                    raise ValueError("Missing input CSV") from exc
                if total > MAX_INPUT_BYTES:
                    raise ValueError("Input exceeds byte limit")
                with (target / source.name).open(
                    "w", newline="", encoding="utf-8"
                ) as output:
                    writer = None
                    for row in iter_rows(source):
                        if writer is None:
                            writer = csv.DictWriter(output, fieldnames=list(row))
                            writer.writeheader()
                        if kind == "stores":
                            key = row["store_id"]
                            limits = {
                                "store_id": 100,
                                "type": 100,
                                "address": 255,
                                "city": 100,
                                "zipcode": 20,
                            }
                        elif kind == "products":
                            key = row["product_id"]
                            limits = {
                                "product_id": 100,
                                "name": 255,
                                "barcode": 50,
                                "brand": 255,
                                "category": 255,
                                "quantity": 50,
                                "unit": 50,
                            }
                            if len(barcode(row, chain.name)) > 50:
                                raise ValueError(
                                    "Normalized barcode exceeds database limit"
                                )
                            if not row["name"].strip():
                                raise ValueError("Missing product name")
                        else:
                            key = offer_key(row)
                            limits = {}
                            for field in MONEY_FIELDS:
                                parse_money(row.get(field, ""), field == "price")
                            if (
                                row["store_id"] not in known["stores"]
                                or row["product_id"] not in known["products"]
                            ):
                                raise ValueError("Missing source reference")
                            availability(row.get("available", ""))
                            if row.get("anchor_price_date"):
                                source_date(row["anchor_price_date"])
                            if len(row.get("special_sale_type", "")) > 255:
                                raise ValueError("Sale type exceeds limit")
                        if not key or any(
                            len(row.get(field, "")) > limit
                            for field, limit in limits.items()
                        ):
                            raise ValueError("Missing identity or oversized metadata")
                        if kind == "prices":
                            # Owner rule: repeated rows merge on unit price, else the pair is skipped.
                            if not remember_first(index, key, row):
                                repeated.add(index, key, row)
                                continue
                        else:
                            known[kind].add(key)
                            # Two product codes of one barcode in a shop are kept; reads pick one.
                            if not remember(index, kind, key, row):
                                continue
                        writer.writerow(row)
                    if writer is None:
                        with source.open(encoding="utf-8", newline="") as original:
                            headers = next(csv.reader(original))
                        csv.writer(output).writerow(headers)
            repeated.apply(target / "prices.csv", chain.name)
