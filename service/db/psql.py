import asyncio
from dataclasses import dataclass, field
import logging
import os
from contextlib import asynccontextmanager
from contextvars import ContextVar
from pathlib import Path
from datetime import date
from time import monotonic
from typing import (
    Any,
    Callable,
    AsyncGenerator,
    AsyncIterator,
    List,
)

import asyncpg

from .base import Database
from .models import (
    Chain,
    ChainProduct,
    ChainProductWithId,
    ChainStats,
    ChainWithId,
    Price,
    Product,
    ProductWithId,
    Store,
    StorePrice,
    StoreWithId,
    User,
)


PRICE_FIELDS = (
    "chain_product_id",
    "store_id",
    "price_date",
    "regular_price",
    "special_price",
    "unit_price",
    "best_price_30",
    "anchor_price",
    "available",
    "anchor_price_date",
    "special_sale_type",
)
# Arrays instead of a temp table per batch: a dropped temp table keeps its lock until commit.
STAGED_PRICES = (
    """unnest(
    $1::int[], $2::int[], $3::date[], $4::numeric[], $5::numeric[], $6::numeric[],
    $7::numeric[], $8::numeric[], $9::boolean[], $10::date[], $11::text[]
) AS t("""
    + ",".join(PRICE_FIELDS)
    + ")"
)

DUPLICATE_CANONICAL_OFFERS = f"""
    SELECT DISTINCT cp.product_id, t.store_id
    FROM {STAGED_PRICES}
    JOIN chain_products cp ON cp.id = t.chain_product_id
    JOIN chain_products other ON other.product_id = cp.product_id
        AND other.chain_id = cp.chain_id AND other.id <> cp.id
    JOIN latest_offers o ON o.chain_product_id = other.id AND o.store_id = t.store_id
"""


async def _backfill_latest_offers(conn: asyncpg.Connection) -> None:
    """Caller holds the writer lock and startup transaction."""
    if await conn.fetchval("SELECT baseline_complete FROM import_state"):
        return

    await conn.execute("LOCK TABLE prices IN SHARE ROW EXCLUSIVE MODE")
    # Legacy imports stored a missing special price as 0, which would win every price sort.
    await conn.execute(
        """
        INSERT INTO latest_offers
        SELECT DISTINCT ON (chain_product_id, store_id)
            chain_product_id, store_id, price_date, regular_price,
            NULLIF(special_price, 0), unit_price, best_price_30, anchor_price, available, anchor_price_date,
            special_sale_type
        FROM prices
        ORDER BY chain_product_id, store_id, price_date DESC
        ON CONFLICT DO NOTHING
        """
    )
    await conn.execute(
        """
        INSERT INTO chain_import_dates
        SELECT chain_id, max(price_date) FROM prices
        JOIN chain_products cp ON cp.id = prices.chain_product_id
        GROUP BY chain_id
        ON CONFLICT DO NOTHING
        """
    )
    await conn.execute("UPDATE import_state SET baseline_complete = TRUE")


def _unique_batch[T](rows: list[T], key: Callable[[T], tuple]) -> list[T]:
    unique: dict[tuple, T] = {}
    for row in rows:
        identity = key(row)
        if identity in unique and unique[identity] != row:
            raise ValueError("Conflicting duplicate batch row")
        unique[identity] = row
    return list(unique.values())


@dataclass(frozen=True)
class _TransactionContext:
    owner: asyncio.Task | None
    connection: asyncpg.Connection
    mode: str
    duplicate_shops: set[tuple[int, int]] = field(default_factory=set)


class PostgresDatabase(Database):
    """PostgreSQL implementation of the database interface using asyncpg."""

    def __init__(self, dsn: str, min_size: int = 10, max_size: int = 30):
        """Initialize the PostgreSQL database connection pool.

        Args:
            dsn: Database connection string
            min_size: Minimum number of connections in the pool
            max_size: Maximum number of connections in the pool
        """
        self.dsn = dsn
        self.min_size = min_size
        self.max_size = max_size
        self.pool = None
        self._import_conn: ContextVar[_TransactionContext | None] = ContextVar(
            "import_connection", default=None
        )
        self.logger = logging.getLogger(__name__)

    async def connect(self) -> None:
        self.pool = await asyncpg.create_pool(
            dsn=self.dsn,
            min_size=self.min_size,
            max_size=self.max_size,
        )

    @asynccontextmanager
    async def _get_conn(self) -> AsyncGenerator[asyncpg.Connection]:
        """Context manager to acquire a connection from the pool."""
        active = self._import_conn.get()
        if active is not None:
            if active.owner is not asyncio.current_task():
                raise RuntimeError(
                    "Transaction connection owner differs from current task"
                )
            yield active.connection
            return
        if not self.pool:
            raise RuntimeError("Database pool is not initialized")
        async with self.pool.acquire() as conn:
            yield conn

    @asynccontextmanager
    async def _atomic(self) -> AsyncIterator[asyncpg.Connection]:
        """Context manager for atomic transactions."""
        async with self._get_conn() as conn:
            async with conn.transaction():
                yield conn

    @asynccontextmanager
    async def _writer_transaction(self, mode: str):
        if self._import_conn.get() is not None:
            raise RuntimeError("Nested writer transaction is unsupported")
        async with self._get_conn() as conn:
            async with conn.transaction(isolation="read_committed"):
                await conn.execute("SELECT pg_advisory_xact_lock(73198421)")
                token = self._import_conn.set(
                    _TransactionContext(asyncio.current_task(), conn, mode)
                )
                try:
                    yield conn
                finally:
                    self._import_conn.reset(token)

    @asynccontextmanager
    async def _cleanup_connection(self):
        if self._import_conn.get() is not None:
            raise RuntimeError("Nested writer transaction is unsupported")
        async with self._get_conn() as conn:
            token = None
            try:
                await conn.execute("SELECT pg_advisory_lock(73198421)")
                token = self._import_conn.set(
                    _TransactionContext(asyncio.current_task(), conn, "cleanup")
                )
                yield conn
            finally:
                if token is not None:
                    self._import_conn.reset(token)
                unlock = asyncio.create_task(
                    conn.execute("SELECT pg_advisory_unlock(73198421)")
                )
                try:
                    await asyncio.shield(unlock)
                except asyncio.CancelledError:
                    await unlock
                    raise

    @asynccontextmanager
    async def import_transaction(self, source_date: date | None = None):
        async with self._writer_transaction("import") as conn:
            preserved = {}
            fields = {
                "stores": "chain_id,code,type,address,city,zipcode,lat,lon,phone",
                "chain_products": "chain_id,product_id,code,brand,name,category,unit,quantity",
            }
            if source_date is not None:
                for table, columns in fields.items():
                    preserved[table] = await conn.fetch(
                        f"SELECT id,{columns} FROM {table} m WHERE metadata_source_date>$1",
                        source_date,
                    )
            yield
            for table, records in preserved.items():
                columns = fields[table].split(",")
                if records:
                    await conn.executemany(
                        f"UPDATE {table} SET "
                        + ",".join(
                            f"{column}=${i}" for i, column in enumerate(columns, 2)
                        )
                        + " WHERE id=$1",
                        [tuple(record) for record in records],
                    )
            context = self._import_conn.get()
            if context is not None:
                self._warn_duplicate_shops(context.duplicate_shops)

    def _warn_duplicate_shops(self, shops: set[tuple[int, int]]) -> None:
        if shops:
            self.logger.warning(
                f"{len(shops):,} shops list one barcode under several chain "
                "codes; reads use the newest offer"
            )

    async def record_metadata_source_date(
        self, table: str, ids: list[int], source: date
    ) -> None:
        if table not in {"stores", "chain_products"} or self._import_conn.get() is None:
            raise RuntimeError("Metadata provenance requires import_transaction")
        async with self._get_conn() as conn:
            await conn.execute(
                f"UPDATE {table} SET metadata_source_date=greatest(metadata_source_date,$2::date) WHERE id=ANY($1::bigint[])",
                ids,
                source,
            )

    async def close(self) -> None:
        """Close all database connections."""
        if self.pool:
            await self.pool.close()

    async def create_tables(self) -> None:
        schema_path = os.path.join(os.path.dirname(__file__), "psql.sql")

        try:
            with open(schema_path, "r") as f:
                schema_sql = f.read()

            async with self._get_conn() as conn:
                async with conn.transaction():
                    await conn.execute("SELECT pg_advisory_xact_lock(73198421)")
                    await conn.execute(schema_sql)
                    for migration in sorted(
                        (Path(__file__).parent / "migrations").glob("*.sql")
                    ):
                        await conn.execute(migration.read_text())
                    await _backfill_latest_offers(conn)
                self.logger.info("Database tables created successfully")
        except Exception as e:
            self.logger.error(f"Error creating tables: {e}")
            raise

    async def _fetchval(self, query: str, *args: Any) -> Any:
        async with self._get_conn() as conn:
            return await conn.fetchval(query, *args)

    async def get_product_barcodes(self) -> dict[str, int]:
        async with self._get_conn() as conn:
            rows = await conn.fetch("SELECT id, ean FROM products")
            return {row["ean"]: row["id"] for row in rows}

    async def get_chain_product_map(self, chain_id: int) -> dict[str, int]:
        async with self._get_conn() as conn:
            rows = await conn.fetch(
                """
                SELECT code, id FROM chain_products WHERE chain_id = $1
                """,
                chain_id,
            )
            return {row["code"]: row["id"] for row in rows}

    async def add_chain(self, chain: Chain) -> int:
        async with self._atomic() as conn:
            chain_id = await conn.fetchval(
                "SELECT id FROM chains WHERE code = $1",
                chain.code,
            )
            if chain_id is not None:
                return chain_id
            chain_id = await conn.fetchval(
                "INSERT INTO chains (code) VALUES ($1) RETURNING id",
                chain.code,
            )
            if chain_id is None:
                raise RuntimeError(f"Failed to insert chain {chain.code}")
            return chain_id

    async def list_chains(self) -> list[ChainWithId]:
        async with self._get_conn() as conn:
            rows = await conn.fetch("SELECT id, code FROM chains")
            return [ChainWithId(**row) for row in rows]

    async def list_latest_chain_stats(self) -> list[ChainStats]:
        async with self._get_conn() as conn:
            rows = await conn.fetch("""
                SELECT
                    c.code AS chain_code,
                    cs.price_date,
                    cs.price_count,
                    cs.store_count,
                    cs.created_at
                FROM chains c
                JOIN LATERAL (
                    SELECT *
                    FROM chain_stats
                    WHERE chain_id = c.id
                    ORDER BY price_date DESC
                    LIMIT 1
                ) cs ON true;
            """)
            return [ChainStats(**row) for row in rows]

    async def add_store(self, store: Store) -> int:
        # Skipping unchanged rows keeps an identical replay from expiring cursors.
        store_id = await self._fetchval(
            """
            INSERT INTO stores (chain_id, code, type, address, city, zipcode)
            VALUES ($1, $2, $3, $4, $5, $6)
            ON CONFLICT (chain_id, code) DO UPDATE SET
                type = COALESCE($3, stores.type),
                address = COALESCE($4, stores.address),
                city = COALESCE($5, stores.city),
                zipcode = COALESCE($6, stores.zipcode)
            WHERE (stores.type, stores.address, stores.city, stores.zipcode)
                IS DISTINCT FROM (
                    COALESCE($3, stores.type),
                    COALESCE($4, stores.address),
                    COALESCE($5, stores.city),
                    COALESCE($6, stores.zipcode)
                )
            RETURNING id
            """,
            store.chain_id,
            store.code,
            store.type,
            store.address or None,
            store.city or None,
            store.zipcode or None,
        )
        if store_id is not None:
            return store_id

        # A separate statement sees a row that a concurrent writer committed meanwhile.
        return await self._fetchval(
            "SELECT id FROM stores WHERE chain_id = $1 AND code = $2",
            store.chain_id,
            store.code,
        )

    @asynccontextmanager
    async def _metadata_connection(self):
        if self._import_conn.get() is not None:
            async with self._get_conn() as conn:
                yield conn
            return
        async with self._writer_transaction("metadata") as conn:
            yield conn

    async def update_store(
        self,
        chain_id: int,
        store_code: str,
        *,
        address: str | None = None,
        city: str | None = None,
        zipcode: str | None = None,
        lat: float | None = None,
        lon: float | None = None,
        phone: str | None = None,
    ) -> bool:
        """
        Update store information by chain_id and store code.
        Returns True if the store was updated, False if not found.
        """
        async with self._metadata_connection() as conn:
            result = await conn.execute(
                """
                UPDATE stores
                SET
                    address = COALESCE($3, stores.address),
                    city = COALESCE($4, stores.city),
                    zipcode = COALESCE($5, stores.zipcode),
                    lat = COALESCE($6, stores.lat),
                    lon = COALESCE($7, stores.lon),
                    phone = COALESCE($8, stores.phone)
                WHERE chain_id = $1 AND code = $2
                """,
                chain_id,
                store_code,
                address or None,
                city or None,
                zipcode or None,
                lat or None,
                lon or None,
                phone or None,
            )
            _, rowcount = result.split(" ")
            return int(rowcount) == 1

    async def list_stores(self, chain_code: str) -> list[StoreWithId]:
        async with self._get_conn() as conn:
            rows = await conn.fetch(
                """
                SELECT
                    s.id, s.chain_id, s.code, s.type, s.address, s.city, s.zipcode,
                    s.lat, s.lon, s.phone
                FROM stores s
                JOIN chains c ON s.chain_id = c.id
                WHERE c.code = $1
                """,
                chain_code,
            )

            return [StoreWithId(**row) for row in rows]

    async def filter_stores(
        self,
        chain_codes: list[str] | None = None,
        city: str | None = None,
        address: str | None = None,
        lat: float | None = None,
        lon: float | None = None,
        d: float = 10.0,
    ) -> list[StoreWithId]:
        # Validate lat/lon parameters
        if (lat is None) != (lon is None):
            raise ValueError(
                "Both lat and lon must be provided together, or both must be None"
            )

        async with self._get_conn() as conn:
            # Build the query dynamically based on provided filters
            where_conditions = []
            params = []
            param_counter = 1

            # Chain codes filter
            if chain_codes:
                where_conditions.append(f"c.code = ANY(${param_counter})")
                params.append(chain_codes)
                param_counter += 1

            # City filter (case-insensitive substring match)
            if city:
                where_conditions.append(f"s.city ILIKE ${param_counter}")
                params.append(f"%{city}%")
                param_counter += 1

            # Address filter (case-insensitive substring match)
            if address:
                where_conditions.append(f"s.address ILIKE ${param_counter}")
                params.append(f"%{address}%")
                param_counter += 1

            # Geolocation filter using computed earth_point column
            if lat is not None and lon is not None:
                where_conditions.append(
                    f"s.earth_point IS NOT NULL AND "
                    f"earth_distance(s.earth_point, ll_to_earth(${param_counter}, ${param_counter + 1})) <= ${param_counter + 2}"
                )
                params.extend([lat, lon, d * 1000])  # Convert km to meters
                param_counter += 3

            # Build the complete query
            base_query = """
                SELECT
                    s.id, s.chain_id, s.code, s.type, s.address, s.city, s.zipcode,
                    s.lat, s.lon, s.phone
                FROM stores s
                JOIN chains c ON s.chain_id = c.id
            """

            if where_conditions:
                query = base_query + " WHERE " + " AND ".join(where_conditions)
            else:
                query = base_query

            query += " ORDER BY c.code, s.code"
            rows = await conn.fetch(query, *params)
            return [StoreWithId(**row) for row in rows]

    async def add_ean(self, ean: str) -> int:
        """
        Add an empty product with only EAN barcode info.

        Args:
            ean: The EAN code to add.

        Returns:
            The database ID of the created product.
        """
        return await self._fetchval(
            "INSERT INTO products (ean) VALUES ($1) RETURNING id",
            ean,
        )

    async def get_products_by_ean(self, ean: list[str]) -> list[ProductWithId]:
        async with self._get_conn() as conn:
            rows = await conn.fetch(
                """
                SELECT id, ean, brand, name, quantity, unit
                FROM products WHERE ean = ANY($1)
                """,
                ean,
            )
            return [ProductWithId(**row) for row in rows]

    async def get_product_store_prices(
        self,
        product_ids: list[int],
        store_ids: list[int] | None = None,
    ) -> list[StorePrice]:
        async with self._get_conn() as conn:
            query = """
                WITH chains_dates AS (
                  -- Find the latest loaded data per chain
                    SELECT DISTINCT ON (chain_id) chain_id, price_date AS last_price_date
                    FROM chain_stats
                    ORDER BY chain_id, price_date DESC
                )
                SELECT
                    chains.id AS chain_id,
                    chains.code AS chain_code,
                    products.ean,
                    prices.price_date,
                    prices.regular_price,
                    prices.special_price,
                    prices.best_price_30,
                    prices.unit_price,
                    prices.anchor_price,
                    stores.code AS store_code,
                    stores.type,
                    stores.address,
                    stores.city,
                    stores.zipcode,
                    stores.lat,
                    stores.lon,
                    stores.phone
                FROM chains_dates
                JOIN chains ON chains.id = chains_dates.chain_id
                JOIN chain_products ON chain_products.chain_id = chains.id
                JOIN products ON products.id = chain_products.product_id
                JOIN prices ON prices.chain_product_id = chain_products.id
                           AND prices.price_date = chains_dates.last_price_date
                JOIN stores ON stores.id = prices.store_id
                WHERE products.id = ANY($1)
            """

            params = [product_ids]
            param_idx = 2

            if store_ids is not None:
                query += f" AND stores.id = ANY(${param_idx})"
                params.append(store_ids)
                param_idx += 1

            rows = await conn.fetch(query, *params)

            return [
                StorePrice(
                    chain=row["chain_code"],
                    ean=row["ean"],
                    price_date=row["price_date"],
                    regular_price=row["regular_price"],
                    special_price=row["special_price"],
                    unit_price=row["unit_price"],
                    best_price_30=row["best_price_30"],
                    anchor_price=row["anchor_price"],
                    store=Store(
                        chain_id=row["chain_id"],
                        code=row["store_code"],
                        type=row["type"],
                        address=row["address"],
                        city=row["city"],
                        zipcode=row["zipcode"],
                        lat=row["lat"],
                        lon=row["lon"],
                        phone=row["phone"],
                    ),
                )
                for row in rows
            ]

    async def update_product(self, product: Product) -> bool:
        """
        Update product information by EAN code.

        Args:
            product: Product object containing the EAN and fields to update.
                    Only non-None fields will be updated in the database.

        Returns:
            True if the product was updated, False if not found.
        """
        async with self._metadata_connection() as conn:
            result = await conn.execute(
                """
                UPDATE products
                SET
                    brand = COALESCE($2, products.brand),
                    name = COALESCE($3, products.name),
                    quantity = COALESCE($4, products.quantity),
                    unit = COALESCE($5, products.unit)
                WHERE ean = $1
                """,
                product.ean,
                product.brand,
                product.name,
                product.quantity,
                product.unit,
            )
            _, rowcount = result.split(" ")
            return int(rowcount) == 1

    async def get_chain_products_for_product(
        self,
        product_ids: list[int],
        chain_ids: list[int] | None = None,
    ) -> list[ChainProductWithId]:
        async with self._get_conn() as conn:
            if chain_ids:
                # Use ANY for filtering by chain IDs
                query = """
                    SELECT
                        id, chain_id, product_id, code, name, brand,
                        category, unit, quantity
                    FROM chain_products
                    WHERE product_id = ANY($1) AND chain_id = ANY($2)
                """
                rows = await conn.fetch(query, product_ids, chain_ids)
            else:
                # Original query when no chain filtering
                query = """
                    SELECT
                        id, chain_id, product_id, code, name, brand,
                        category, unit, quantity
                    FROM chain_products
                    WHERE product_id = ANY($1)
                """
                rows = await conn.fetch(query, product_ids)
            return [ChainProductWithId(**row) for row in rows]

    async def search_products(
        self,
        query: str,
        limit: int = 20,
        chain_ids: list[int] | None = None,
        date: date | None = None,
    ) -> list[ProductWithId]:
        # Strip LIKE wildcards so user input can't inject match-anything patterns
        words = [
            word.strip().lower().replace("%", "").replace("_", "")
            for word in query.split()
        ]
        words = [word for word in words if word]
        if not words:
            return []

        where_conditions = []
        params: list[Any] = []

        # Match against the same lower+unaccent expression that
        # idx_chain_products_name_trgm indexes (see psql.sql), so the GIN
        # trigram index is used instead of a sequential scan. Words shorter
        # than 3 characters yield no trigrams and fall back to a scan.
        for word in words:
            params.append(word)
            where_conditions.append(
                "lower(immutable_unaccent(cp.name || ' ' || COALESCE(cp.brand, '')))"
                f" LIKE '%' || immutable_unaccent(${len(params)}) || '%'"
            )

        if chain_ids is not None:
            params.append(chain_ids)
            where_conditions.append(f"cp.chain_id = ANY(${len(params)})")

        where_clause = " AND ".join(where_conditions)
        params.append(date)
        date_param_idx = len(params)
        params.append(limit)

        # Only match chain products that have a price on their chain's
        # effective date (latest load on or before the requested date) —
        # the same criterion get_product_prices() uses — so discontinued
        # products don't consume result slots and cause the response to
        # contain fewer than `limit` products even when more matches exist.
        #
        # ean is unique per product, so grouping by product_id is equivalent
        # to grouping by ean but lets us join products after the limit,
        # for the returned rows only.
        query_sql = f"""
            WITH chains_dates AS (
                SELECT DISTINCT ON (chain_id) chain_id, price_date AS last_price_date
                FROM chain_stats
                WHERE price_date <= COALESCE(${date_param_idx}::date, CURRENT_DATE)
                ORDER BY chain_id, price_date DESC
            )
            SELECT p.id, p.ean, p.brand, p.name, p.quantity, p.unit
            FROM (
                SELECT cp.product_id, COUNT(*) AS match_count
                FROM chain_products cp
                JOIN chains_dates cd ON cd.chain_id = cp.chain_id
                WHERE cp.product_id IS NOT NULL AND {where_clause}
                  AND EXISTS (
                    SELECT 1 FROM chain_prices cpr
                    WHERE cpr.chain_product_id = cp.id
                      AND cpr.price_date = cd.last_price_date
                  )
                GROUP BY cp.product_id
                ORDER BY match_count DESC
                LIMIT ${len(params)}
            ) matches
            JOIN products p ON p.id = matches.product_id
            ORDER BY matches.match_count DESC
        """
        async with self._get_conn() as conn:
            rows = await conn.fetch(query_sql, *params)
            return [ProductWithId(**row) for row in rows]

    async def fuzzy_search_products(
        self,
        query: str,
        limit: int = 20,
        chain_ids: list[int] | None = None,
        date: date | None = None,
    ) -> list[ProductWithId]:
        if not query.strip():
            return []

        # Normalize query: strip diacritics and convert to lowercase
        normalized_query = query.strip().lower()

        params: list[Any] = [normalized_query]
        chain_filter = ""
        if chain_ids is not None:
            params.append(chain_ids)
            chain_filter = f"AND cp.chain_id = ANY(${len(params)})"
        params.append(date)
        date_param_idx = len(params)
        params.append(limit)

        # Trigram fuzzy search using PostgreSQL pg_trgm extension:
        # - % operator: checks similarity > 0.3 threshold, uses GIN trigram index for fast filtering
        # - <-> operator: trigram distance (0=identical, 1=completely different), also index-accelerated
        # - immutable_unaccent(): strips diacritics ("čaša" -> "casa") in index-compatible way
        # - 1 - distance = similarity score for ranking (1=perfect match, 0=no similarity)
        # Requires matching index and function (see idx_chain_products_name_trgm and immutable_unaccent in psql.sql)
        # The EXISTS clause excludes discontinued products (no price on the
        # chain's effective date); see the comment in search_products().
        query_sql = f"""
            WITH chains_dates AS (
                SELECT DISTINCT ON (chain_id) chain_id, price_date AS last_price_date
                FROM chain_stats
                WHERE price_date <= COALESCE(${date_param_idx}::date, CURRENT_DATE)
                ORDER BY chain_id, price_date DESC
            )
            SELECT p.id, p.ean, p.brand, p.name, p.quantity, p.unit
            FROM (
                SELECT
                    cp.product_id,
                    MAX(1 - (lower(immutable_unaccent(cp.name || ' ' || COALESCE(cp.brand, ''))) <-> immutable_unaccent($1)))
                    AS similarity_score
                FROM chain_products cp
                JOIN chains_dates cd ON cd.chain_id = cp.chain_id
                WHERE lower(immutable_unaccent(cp.name || ' ' || COALESCE(cp.brand, ''))) % immutable_unaccent($1)
                  AND cp.product_id IS NOT NULL
                  {chain_filter}
                  AND EXISTS (
                    SELECT 1 FROM chain_prices cpr
                    WHERE cpr.chain_product_id = cp.id
                      AND cpr.price_date = cd.last_price_date
                  )
                GROUP BY cp.product_id
                ORDER BY similarity_score DESC
                LIMIT ${len(params)}
            ) matches
            JOIN products p ON p.id = matches.product_id
            ORDER BY matches.similarity_score DESC
        """
        async with self._get_conn() as conn:
            rows = await conn.fetch(query_sql, *params)
            return [ProductWithId(**row) for row in rows]

    async def get_product_prices(
        self, product_ids: list[int], date: date
    ) -> list[dict[str, Any]]:
        async with self._get_conn() as conn:
            return await conn.fetch(
                """
                WITH chains_dates AS (
                    -- Find the latest loaded data per chain
                   SELECT DISTINCT ON (chain_id) chain_id, price_date AS last_price_date
                   FROM chain_stats
                   WHERE price_date <= $2
                   ORDER BY chain_id, price_date DESC
                )
                SELECT chains.code AS chain,
                       chain_products.product_id,
                       chain_prices.min_price,
                       chain_prices.max_price,
                       chain_prices.avg_price,
                       chain_prices.price_date
                FROM chains_dates
                JOIN chains ON chains.id = chains_dates.chain_id
                JOIN chain_products ON chain_products.chain_id = chains.id
                JOIN chain_prices ON chain_prices.chain_product_id = chain_products.id
                                 AND chain_prices.price_date = chains_dates.last_price_date
                WHERE chain_products.product_id = ANY($1)
                """,
                product_ids,
                date,
            )

    async def add_many_prices(self, prices: list[Price]) -> int:
        prices = _unique_batch(
            prices, lambda row: (row.chain_product_id, row.store_id, row.price_date)
        )
        columns = [
            [getattr(price, field) for price in prices] for field in PRICE_FIELDS
        ]
        async with self._atomic() as conn:
            result = await conn.execute(
                f"""
                INSERT INTO prices({",".join(PRICE_FIELDS)})
                SELECT * FROM {STAGED_PRICES}
                ON CONFLICT (chain_product_id, store_id, price_date) DO UPDATE SET
                    regular_price = EXCLUDED.regular_price,
                    special_price = EXCLUDED.special_price,
                    unit_price = EXCLUDED.unit_price,
                    best_price_30 = EXCLUDED.best_price_30,
                    anchor_price = EXCLUDED.anchor_price,
                    available = EXCLUDED.available,
                    anchor_price_date = EXCLUDED.anchor_price_date,
                    special_sale_type = EXCLUDED.special_sale_type
                """,
                *columns,
            )
            await conn.execute(
                f"""
                INSERT INTO latest_offers
                SELECT DISTINCT ON (chain_product_id, store_id) * FROM {STAGED_PRICES}
                ORDER BY chain_product_id, store_id, price_date DESC
                ON CONFLICT (chain_product_id, store_id) DO UPDATE SET
                    price_date = EXCLUDED.price_date,
                    regular_price = EXCLUDED.regular_price,
                    special_price = EXCLUDED.special_price,
                    unit_price = EXCLUDED.unit_price,
                    best_price_30 = EXCLUDED.best_price_30,
                    anchor_price = EXCLUDED.anchor_price,
                    available = EXCLUDED.available,
                    anchor_price_date = EXCLUDED.anchor_price_date,
                    special_sale_type = EXCLUDED.special_sale_type
                WHERE EXCLUDED.price_date >= latest_offers.price_date
                    AND ROW(latest_offers.*) IS DISTINCT FROM ROW(EXCLUDED.*)
                """,
                *columns,
            )
            await conn.execute(
                f"""
                INSERT INTO chain_import_dates
                SELECT cp.chain_id, max(t.price_date) FROM {STAGED_PRICES}
                JOIN chain_products cp ON cp.id = t.chain_product_id
                GROUP BY cp.chain_id
                ON CONFLICT (chain_id) DO UPDATE SET price_date = EXCLUDED.price_date
                WHERE EXCLUDED.price_date > chain_import_dates.price_date
                """,
                *columns,
            )
            duplicates = {
                (row["product_id"], row["store_id"])
                for row in await conn.fetch(DUPLICATE_CANONICAL_OFFERS, *columns)
            }
            context = self._import_conn.get()
            if context is None:
                self._warn_duplicate_shops(duplicates)
            else:
                context.duplicate_shops.update(duplicates)
            _, _, rowcount = result.split(" ")
            return int(rowcount)

    async def add_many_chain_products(
        self,
        chain_products: List[ChainProduct],
    ) -> int:
        chain_products = _unique_batch(
            chain_products, lambda row: (row.chain_id, row.code)
        )
        fields = (
            "chain_id",
            "product_id",
            "code",
            "name",
            "brand",
            "category",
            "unit",
            "quantity",
        )
        async with self._atomic() as conn:
            result = await conn.execute(
                f"""
                INSERT INTO chain_products({",".join(fields)})
                SELECT * FROM unnest(
                    $1::int[], $2::int[], $3::text[], $4::text[],
                    $5::text[], $6::text[], $7::text[], $8::text[]
                )
                ON CONFLICT (chain_id, code) DO UPDATE SET
                    product_id = EXCLUDED.product_id,
                    name = EXCLUDED.name,
                    brand = EXCLUDED.brand,
                    category = EXCLUDED.category,
                    unit = EXCLUDED.unit,
                    quantity = EXCLUDED.quantity
                WHERE (
                    chain_products.product_id,
                    chain_products.name,
                    chain_products.brand,
                    chain_products.category,
                    chain_products.unit,
                    chain_products.quantity
                ) IS DISTINCT FROM (
                    EXCLUDED.product_id,
                    EXCLUDED.name,
                    EXCLUDED.brand,
                    EXCLUDED.category,
                    EXCLUDED.unit,
                    EXCLUDED.quantity
                )
                """,
                *([getattr(row, field) for row in chain_products] for field in fields),
            )
            _, _, rowcount = result.split(" ")
            return int(rowcount)

    async def compute_chain_prices(self, date: date) -> None:
        async with self._get_conn() as conn:
            await conn.execute(
                """
                INSERT INTO chain_prices (
                    chain_product_id,
                    price_date,
                    min_price,
                    max_price,
                    avg_price
                )
                SELECT
                    chain_product_id,
                    price_date,
                    MIN(
                        LEAST(
                            COALESCE(regular_price, special_price),
                            COALESCE(special_price, regular_price)
                        )
                    ) AS min_price,
                    MAX(
                        LEAST(
                            COALESCE(regular_price, special_price),
                            COALESCE(special_price, regular_price)
                        )
                    ) AS max_price,
                    ROUND(
                        AVG(
                            LEAST(
                                COALESCE(regular_price, special_price),
                                COALESCE(special_price, regular_price)
                            )
                        ),
                        2
                    ) AS avg_price
                FROM prices
                WHERE price_date = $1
                GROUP BY chain_product_id, price_date
                ON CONFLICT (chain_product_id, price_date)
                DO UPDATE SET
                    min_price = EXCLUDED.min_price,
                    max_price = EXCLUDED.max_price,
                    avg_price = EXCLUDED.avg_price;

                """,
                date,
            )

    async def compute_chain_stats(self, date: date) -> None:
        async with self._atomic() as conn:
            # Not doing insert in the same query because that caused deadlocks
            # for reasons which I don't understand.
            stats = await conn.fetch(
                """
                SELECT
                    cp.chain_id,
                    COUNT(*) AS price_count,
                    COUNT(DISTINCT p.store_id) AS store_count
                FROM prices p
                JOIN chain_products cp ON cp.id = p.chain_product_id
                WHERE p.price_date = $1
                GROUP BY cp.chain_id
                """,
                date,
            )

            for record in stats:
                await conn.execute(
                    """
                    INSERT INTO chain_stats(chain_id, price_date, price_count, store_count)
                    VALUES ($1, $2, $3, $4)
                    ON CONFLICT (chain_id, price_date)
                    DO UPDATE SET
                        price_count = EXCLUDED.price_count,
                        store_count = EXCLUDED.store_count;
                    """,
                    record["chain_id"],
                    date,
                    record["price_count"],
                    record["store_count"],
                )

    async def _get_kept_chains(self, before: date) -> dict[date, list[int]]:
        """
        Find raw-price and stats-backed latest dates before the cutoff.

        Returns:
            Chain IDs per latest data date. The rows of these chains on
            that date stay.
        """
        async with self._get_conn() as conn:
            rows = await conn.fetch(
                """
                SELECT chain_id, latest FROM (
                    SELECT cp.chain_id, max(p.price_date) AS latest
                    FROM prices p
                    JOIN chain_products cp ON cp.id = p.chain_product_id
                    GROUP BY cp.chain_id
                    UNION
                    SELECT chain_id, max(price_date) AS latest
                    FROM chain_stats
                    GROUP BY chain_id
                ) protected
                WHERE latest < $1
                """,
                before,
            )
        kept: dict[date, list[int]] = {}
        for row in rows:
            kept.setdefault(row["latest"], []).append(row["chain_id"])
        return kept

    async def _price_dates_before(self, before: date) -> AsyncIterator[date]:
        """
        Yield the price dates before the given date that have rows, oldest first.

        Each step reads one entry of the price date index, so the cost depends
        on the number of dates with rows, not on the number of days between
        the oldest date and the given date. The next date is read after the
        caller has processed the current one.
        """
        price_date = await self._fetchval(
            "SELECT min(price_date) FROM prices WHERE price_date < $1",
            before,
        )
        while price_date is not None:
            yield price_date
            price_date = await self._fetchval(
                """
                SELECT min(price_date) FROM prices
                WHERE price_date > $1 AND price_date < $2
                """,
                price_date,
                before,
            )

    @staticmethod
    def _old_prices_filter(
        price_date: date, kept: dict[date, list[int]]
    ) -> tuple[str, list[Any]]:
        """
        Build the WHERE clause and arguments for the old rows of one date.

        Returns:
            The condition and its query arguments.
        """
        chain_ids = kept.get(price_date)
        if not chain_ids:
            return "price_date = $1", [price_date]
        condition = """
            price_date = $1
            AND NOT EXISTS (
                SELECT 1 FROM chain_products cp
                WHERE cp.id = prices.chain_product_id
                  AND cp.chain_id = ANY($2::int[])
            )
        """
        return condition, [price_date, chain_ids]

    async def count_prices_before(self, before: date) -> dict[date, int]:
        async with self._writer_transaction("cleanup"):
            return await self._count_prices_before(before)

    async def _count_prices_before(self, before: date) -> dict[date, int]:
        kept = await self._get_kept_chains(before)
        counts: dict[date, int] = {}
        async for price_date in self._price_dates_before(before):
            condition, args = self._old_prices_filter(price_date, kept)
            count = await self._fetchval(
                f"SELECT count(*) FROM prices WHERE {condition}", *args
            )
            if count:
                counts[price_date] = count
            else:
                self.logger.debug(f"{price_date}: no rows to delete")
        return counts

    async def delete_prices_before(self, before: date) -> dict[date, int]:
        async with self._cleanup_connection():
            deleted = await self._delete_prices_before(before)
        if deleted:
            await self._vacuum_prices(sum(deleted.values()))
        return deleted

    async def _delete_prices_before(self, before: date) -> dict[date, int]:
        kept = await self._get_kept_chains(before)
        deleted: dict[date, int] = {}
        async for price_date in self._price_dates_before(before):
            condition, args = self._old_prices_filter(price_date, kept)
            t0 = monotonic()
            async with self._atomic() as conn:
                status = await conn.execute(
                    f"DELETE FROM prices WHERE {condition}", *args
                )
            count = int(status.split()[-1])
            if count:
                deleted[price_date] = count
                self.logger.info(
                    f"{price_date}: deleted {count:,} rows in {monotonic() - t0:.0f} s"
                )
            else:
                self.logger.debug(f"{price_date}: no rows to delete")

        return deleted

    async def _vacuum_prices(self, deleted: int) -> None:
        """
        Run VACUUM and ANALYZE on the prices table after a delete.

        VACUUM records the space of the deleted rows in the free space map,
        so that imports use it again instead of making the table larger.
        Server messages (the VERBOSE report, a warning if VACUUM skips the
        table) go to the log.

        Args:
            deleted: Number of rows deleted before this call.
        """

        def log_server_message(
            _conn: asyncpg.Connection, msg: asyncpg.PostgresLogMessage
        ) -> None:
            fields = msg.as_dict()
            severity = fields.get("severity_en", "")
            level = logging.WARNING if severity == "WARNING" else logging.INFO
            self.logger.log(level, f"server {severity}: {fields.get('message')}")

        # The pool resets the session settings when the connection goes back.
        async with self._get_conn() as conn:
            conn.add_log_listener(log_server_message)
            try:
                # Enough memory to remove ~170M rows in one pass over the
                # indexes (PostgreSQL 15/16 use 6 bytes per row). Allocated
                # as needed.
                await conn.execute("SET maintenance_work_mem = '1GB'")

                # VACUUM must not run inside a transaction block, so it is a
                # separate statement. PARALLEL 0: a parallel VACUUM needs
                # shared memory, which fails in containers with a small
                # /dev/shm.
                self.logger.info("Running VACUUM on prices")
                t0 = monotonic()
                await conn.execute("VACUUM (VERBOSE, PARALLEL 0) prices")
                self.logger.info(f"VACUUM done in {monotonic() - t0:.0f} s")

                # After VACUUM, n_dead_tup is the number of dead rows it could
                # not remove. Read it before ANALYZE replaces it with an
                # estimate.
                dead = await conn.fetchval(
                    """
                    SELECT n_dead_tup FROM pg_stat_user_tables
                    WHERE relid = 'prices'::regclass
                    """
                )
                dead = dead or 0
                if dead > deleted // 100:
                    self.logger.warning(
                        f"{dead:,} dead rows are still in prices after VACUUM. "
                        "An open transaction older than the delete keeps them "
                        "until the next run; see also the server messages above."
                    )
                else:
                    self.logger.info(f"{dead:,} dead rows are still in prices")

                t0 = monotonic()
                await conn.execute("ANALYZE prices")
                self.logger.info(f"ANALYZE done in {monotonic() - t0:.0f} s")
            finally:
                conn.remove_log_listener(log_server_message)

    async def get_user_by_api_key(self, api_key: str) -> User | None:
        async with self._get_conn() as conn:
            row = await conn.fetchrow(
                """
                SELECT id, name, api_key, is_active, created_at
                FROM users
                WHERE
                    api_key = $1 AND
                    is_active = TRUE
                """,
                api_key,
            )

            if row:
                return User(**row)
            return None
